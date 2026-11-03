"""
Itaú Uruguay scraper client.

Correct login + data-fetch flow (reverse-engineered, 2026):

  1. GET  /trx/                       → prime session cookies
  2. POST /trx/doLogin                → form-based auth; redirects to /trx/ on success
  3. GET  /trx/                       → dashboard HTML; parse accounts + CSRF token
     ...or a 302 to /trx/inicio-seguro when the device isn't recognized. That
     page embeds `opeID` + the account's token types; then:
       POST /trx/tokens/operations/sms          {operationId, tokenType=SMS_TOKEN}  (SMS only)
       POST /trx/inicio-seguro/validar-token    {token, tipoToken, operationID}
     → data.operacion_aprobada on success, after which step 3 succeeds.
     Itaú sets no persistent "trusted device" cookie, so this repeats on every
     fresh login from an unrecognized device.
  4. POST /trx/tarjetas/credito       → AJAX; returns credit-card list with hashes
  5. POST /trx/tarjetas/credito/{hash}/movimientos_actuales/{YYYYMM}00 → CC moves for statement period
     (use 00000000 as suffix for the current open period)
  6. POST /trx/cuentas/1/{hash}/mesActual                              → current-month account moves
     POST /trx/cuentas/1/{hash}/{month}/{year:02d}/consultaHistorica   → historic account moves

All AJAX calls require: X-CSRF-TOKEN, X-Requested-With: XMLHttpRequest.
"""

import json
import logging
import re
from datetime import date
from typing import Any, Optional

import httpx

logger = logging.getLogger("my_itau.client")

BASE_URL = "https://www.itaulink.com.uy"
TRX = "/trx"

ERROR_CODES: dict[str, str] = {
    "10010": "Bad document number / login",
    "10020": "Bad password",
    "10030": "Account blocked",
}

class ItauAuthError(Exception):
    """Login/authentication failure. `code` is Itaú's numeric message_code
    (see ERROR_CODES) when available, so callers can distinguish e.g. a
    blocked account (10030) from bad credentials."""

    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(message)
        self.code = code


class ItauSessionExpired(Exception):
    """The Itaú session is genuinely gone — the redirect carried the
    `expiredSession` marker. Callers should re-login and retry."""
    pass


class ItauNoHistoric(Exception):
    """The historic endpoint redirected WITHOUT an `expiredSession` marker.
    That is not a session problem: the account simply doesn't expose a
    historic-movements view (e.g. business "empresa" accounts). Callers should
    treat it as "no movements for this period", NOT as an error."""
    pass


class ItauTwoFactorRequired(Exception):
    """Login credentials were correct, but Itaú doesn't recognize this device
    and demands a second factor before granting dashboard access (it redirects
    to /trx/inicio-seguro).

    The client that raised this is left mid-login: call
    `submit_second_factor(code, token_type)` on it (or on a client rebuilt via
    `ItauClient.from_session(client.export_session())`) to finish.

    `token_types`: what the account supports, e.g. ["SMS_TOKEN", "ITOKEN"].
    `phone_masked`: where an SMS code would go, e.g. "****1234" (or None)."""

    def __init__(self, message: str, token_types: list[str], phone_masked: Optional[str]):
        super().__init__(message)
        self.token_types = token_types
        self.phone_masked = phone_masked


TOKEN_TYPES = ("ITOKEN", "SMS_TOKEN", "PHYSICAL_TOKEN")


def _parse_second_factor_page(html: str) -> dict:
    """Pull what the inicio-seguro page's own JS uses to drive the token step."""
    ope = re.search(r"var opeID\s*=\s*'([^']+)'", html)
    types = re.search(r"var twofactorsAuth\s*=\s*'\[([^\]]*)\]'", html)
    phone = re.search(r'id="phone-number-token-to-send"[^>]*>([^<]+)<', html)
    csrf = re.search(r'<meta name="_csrf"\s+content="([^"]+)"', html)
    return {
        "operation_id": ope.group(1) if ope else None,
        "token_types": [t.strip() for t in types.group(1).split(",") if t.strip()] if types else [],
        "phone_masked": phone.group(1).strip() if phone else None,
        "csrf": csrf.group(1) if csrf else "",
    }


class ItauClient:
    """Stateful HTTP client for itaulink.com.uy. One instance = one login session."""

    def __init__(self) -> None:
        self._http = httpx.Client(
            base_url=BASE_URL,
            follow_redirects=False,
            timeout=30,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/146.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "es-UY,es;q=0.9",
            },
        )
        self._csrf: str = ""
        self.accounts: list[dict] = []
        self.credit_cards: list[dict] = []
        # Card product + art (from /actualizar) never change → cache per session.
        self._card_art_cache: dict[str, dict] = {}
        # Set while a login is parked on Itaú's second-factor step.
        self._pending_2fa: Optional[dict] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def login(self, document_number: str, password: str) -> None:
        """Authenticate. Raises ItauAuthError on bad credentials, or
        ItauTwoFactorRequired when Itaú doesn't recognize the device — finish
        that with submit_second_factor() (after request_sms() for SMS codes)."""
        logger.info("Logging in as ••%s", document_number[-3:])
        self._prime_session()
        redirect = self._do_login(document_number, password)
        logger.debug("Login redirect → %s", redirect)

        if "message_code=" in redirect:
            m = re.search(r"message_code=(\d+)", redirect)
            code = m.group(1) if m else "unknown"
            raise ItauAuthError(ERROR_CODES.get(code, f"Login failed (code {code})"), code=code)

        self._load_dashboard()
        self._load_credit_cards()

    # -- Second factor (inicio-seguro) ------------------------------------

    def request_sms(self) -> None:
        """Ask Itaú to text a one-time code to the account's phone."""
        p = self._require_pending()
        if "SMS_TOKEN" not in p["token_types"]:
            raise ItauAuthError("This account doesn't support SMS codes.")
        r = self._ajax_post(
            f"{TRX}/tokens/operations/sms",
            str(httpx.QueryParams({"operationId": p["operation_id"], "tokenType": "SMS_TOKEN"})).encode(),
        )
        r.raise_for_status()
        err = (r.json().get("itaulink_msg", {}).get("data", {}) or {}).get("errorMsg")
        if err:
            raise ItauAuthError(f"Itaú couldn't send the SMS: {err}")

    def submit_second_factor(self, code: str, token_type: str = "ITOKEN") -> None:
        """Finish a login parked on the second-factor step. On success the
        client is fully logged in (accounts + cards loaded), same as login()."""
        p = self._require_pending()
        if token_type not in TOKEN_TYPES:
            raise ValueError(f"token_type must be one of {TOKEN_TYPES}")
        r = self._ajax_post(
            f"{TRX}/inicio-seguro/validar-token",
            str(httpx.QueryParams({"token": code.strip(), "tipoToken": token_type, "operationID": p["operation_id"]})).encode(),
        )
        if r.is_redirect:
            raise ItauSessionExpired("The second-factor step expired — start the login again.")
        r.raise_for_status()
        data = (r.json().get("itaulink_msg", {}) or {}).get("data", {}) or {}
        if "operacion_aprobada" not in data:
            msg = data.get("errorAlert") or data.get("error") or "Itaú rejected the code."
            raise ItauAuthError(re.sub(r"<[^>]+>", "", str(msg)).strip(), code="second_factor_rejected")
        self._pending_2fa = None
        self._load_dashboard()
        self._load_credit_cards()

    def export_session(self) -> dict:
        """Serializable snapshot of the live session (cookies, CSRF, any parked
        second-factor step). Treat as a credential — it IS one, for ~20 min."""
        return {
            "cookies": [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                        for c in self._http.cookies.jar],
            "csrf": self._csrf,
            "pending_2fa": self._pending_2fa,
        }

    @classmethod
    def from_session(cls, state: dict) -> "ItauClient":
        """Rebuild a client from export_session() — e.g. in a second stateless
        request that submits the code the user typed."""
        c = cls()
        for ck in state.get("cookies", []):
            c._http.cookies.set(ck["name"], ck["value"], domain=ck["domain"], path=ck["path"])
        c._csrf = state.get("csrf", "")
        c._pending_2fa = state.get("pending_2fa")
        return c

    def _require_pending(self) -> dict:
        if not self._pending_2fa:
            raise ItauAuthError("No login is waiting on a second factor.")
        return self._pending_2fa

    def get_credit_card_payload(
        self,
        card_hash: Optional[str] = None,
        month: Optional[int] = None,
        year: Optional[int] = None,
    ) -> dict:
        """
        Returns the raw itaulink_msg content exactly as Itaú sends it:
          { header: {...}, data: { datos: { tarjetaId, datosMovimientos: { movimientos: [...] } } } }
        """
        today = date.today()
        month = month or today.month
        year = year or today.year
        url_hash = card_hash or (self.credit_cards[0]["hash"] if self.credit_cards else "")

        r = self._ajax_post(
            f"{TRX}/tarjetas/credito/{url_hash}/movimientos_actuales/{year:04d}{month:02d}00",
            b"{}",
        )
        self._check_session(r)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "itaulink_msg" in data:
            return data["itaulink_msg"]
        return data

    def get_credit_card_moves(
        self,
        card_hash: Optional[str] = None,
        month: Optional[int] = None,
        year: Optional[int] = None,
    ) -> list[dict]:
        """Credit-card movements. Defaults to current statement period, all cards.

        card_hash: filter to one card. If None, returns moves for all cards on the account.
        """
        today = date.today()
        month = month or today.month
        year = year or today.year
        return self._fetch_cc_moves(card_hash, year, month)

    def get_pending_authorizations(self, card_hash: Optional[str] = None) -> list[dict]:
        """Approved pending authorizations (current period).

        card_hash: if provided, filters to that card only. If None, returns all
        cards on the account (the endpoint always returns account-wide data).
        Any card hash on the account can be used as the URL key.
        """
        url_hash = card_hash or (self.credit_cards[0]["hash"] if self.credit_cards else "")
        if not url_hash:
            return []
        url = f"{TRX}/tarjetas/credito/{url_hash}/autorizaciones_pendientes"
        r = self._ajax_post(url, b"{}")
        self._check_session(r)
        r.raise_for_status()
        data = r.json()
        auths = (
            data.get("itaulink_msg", data)
                .get("data", {})
                .get("datos", {})
                .get("datosAutorizaciones", {})
                .get("autorizaciones", [])
        )
        approved = [a for a in auths if a.get("aprobada")]
        if card_hash:
            approved = [a for a in approved if a.get("tarjeta", {}).get("hash") == card_hash]
        return approved

    def get_account_moves(
        self,
        account_hash: str,
        month: Optional[int] = None,
        year: Optional[int] = None,
    ) -> list[dict]:
        """Bank-account movements. Defaults to current month."""
        today = date.today()
        month = month or today.month
        year = year or today.year
        year_2d = year - 2000 if year > 2000 else year

        if month == today.month and year == today.year:
            return self._fetch_account_current(account_hash)
        return self._fetch_account_historic(account_hash, month, year_2d)

    # ------------------------------------------------------------------
    # Internal: session bootstrap
    # ------------------------------------------------------------------

    def _prime_session(self) -> None:
        try:
            self._http.get(TRX + "/")
        except httpx.HTTPError as e:
            logger.warning("Could not prime session: %s", e)

    def _do_login(self, document_number: str, password: str) -> str:
        r = self._http.post(
            TRX + "/doLogin",
            data={
                "tipo_documento": "1",
                "tipo_usuario": "R",
                "nro_documento": document_number,
                "pass": password,
            },
        )
        if r.status_code not in (301, 302, 303, 307, 308):
            raise ItauAuthError(
                f"Unexpected status {r.status_code} from /doLogin. "
                "The login endpoint may have changed."
            )
        return r.headers.get("location", "")

    def _load_dashboard(self) -> None:
        r = self._http.get(TRX + "/")
        if r.is_redirect and "inicio-seguro" in r.headers.get("location", ""):
            page = self._http.get(r.headers["location"])
            info = _parse_second_factor_page(page.text) if page.status_code == 200 else {}
            if not info.get("operation_id"):
                raise ItauAuthError(
                    f"Itaú asked for a second factor but the token page couldn't be read "
                    f"(HTTP {page.status_code}). Log out of other Itaú sessions and retry."
                )
            self._csrf = info["csrf"] or self._csrf
            self._pending_2fa = {k: info[k] for k in ("operation_id", "token_types", "phone_masked")}
            raise ItauTwoFactorRequired(
                "Itaú requires a second factor (iToken/SMS) for this device.",
                token_types=info["token_types"],
                phone_masked=info["phone_masked"],
            )
        r.raise_for_status()
        html = r.text

        m = re.search(r'<meta name="_csrf"\s+content="([^"]+)"', html)
        self._csrf = m.group(1) if m else ""
        logger.debug("CSRF: %s", self._csrf)

        target_line = next(
            (l for l in html.splitlines() if "var mensajeUsuario = JSON.parse" in l),
            None,
        )
        if not target_line:
            logger.warning("mensajeUsuario not found — accounts list will be empty")
            return

        start = target_line.find("{")
        end = target_line.rfind("}")
        if start == -1 or end == -1:
            logger.warning("Could not find JSON braces in mensajeUsuario line")
            return

        try:
            user_data: dict = json.loads(target_line[start : end + 1])
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse mensajeUsuario: %s", e)
            return

        cuentas = user_data.get("cuentas", {})
        self.accounts = [
            self._normalise_account(a)
            for key in (
                "CAJA_DE_AHORRO",
                "CUENTA_CORRIENTE",
                "CUENTA_RECAUDADORA",
                "CUENTA_DE_AHORRO_JUNIOR",
                "CUENTA_DE_ALIMENTACION",
            )
            for a in cuentas.get(key, [])
        ]
        logger.info("Loaded %d bank accounts", len(self.accounts))

    def _load_credit_cards(self) -> None:
        r = self._ajax_post(TRX + "/tarjetas/credito", b"{}")
        if r.status_code != 200:
            logger.warning("Credit-card list failed: HTTP %d", r.status_code)
            return

        try:
            payload = r.json()
        except Exception as e:
            logger.warning("Could not parse CC list response: %s", e)
            return

        tarjeta_data = (
            payload.get("itaulink_msg", {})
                   .get("data", {})
                   .get("objetosTarjetaCredito", {})
        )
        tarjeta_imagen = tarjeta_data.get("tarjetaImagen", [])

        cards = []
        for group in tarjeta_imagen:
            if isinstance(group, list) and group:
                primary = group[0]
            elif isinstance(group, dict):
                primary = group
            else:
                continue
            cards.append(self._normalise_card(primary))

        self.credit_cards = cards
        logger.info("Loaded %d credit cards", len(self.credit_cards))

    def card_art(self, card_hash: str) -> dict:
        """Product name + card-art image URL for a card, from the per-card
        `/actualizar` view. Cached per session (these never change). Returns
        {"product": str|None, "imageUrl": str|None} — empty on any failure."""
        if card_hash in self._card_art_cache:
            return self._card_art_cache[card_hash]
        result: dict = {"product": None, "imageUrl": None}
        try:
            r = self._http.get(
                f"{TRX}/tarjetas/credito/{card_hash}/actualizar",
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-CSRF-TOKEN": self._csrf,
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{BASE_URL}{TRX}/",
                },
            )
            if r.status_code == 200:
                datos = (
                    r.json().get("itaulink_msg", {}).get("data", {}).get("datos", {})
                )
                imagen = datos.get("imagen")
                product = (datos.get("datosAdicionales") or {}).get("tipo")
                if not product:  # fall back to the card's own descripcion
                    for t in (datos.get("allTarjetas") or []):
                        if t.get("hash") == card_hash and t.get("descripcion"):
                            product = t["descripcion"]
                            break
                result["product"] = product or None
                if imagen:
                    result["imageUrl"] = f"{BASE_URL}/static/img/dynamic/tarjetas_credito/{imagen}"
        except Exception as e:  # best-effort — never break the cards list
            logger.warning("card_art(%s) failed: %s", card_hash[:8], e)
        self._card_art_cache[card_hash] = result
        return result

    # ------------------------------------------------------------------
    # Internal: normalise raw objects
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_account(raw: dict) -> dict:
        return {
            "type": raw.get("tipoCuenta"),
            "id": raw.get("idCuenta"),
            "holder": raw.get("nombreTitular"),
            "currency": raw.get("moneda"),
            "balance": raw.get("saldo"),
            "hash": raw.get("hash"),
            "customer_hash": raw.get("hashCustomer"),
        }

    @staticmethod
    def _normalise_card(raw: dict) -> dict:
        expiry = raw.get("fechaVencimiento") or {}
        expiry_str = None
        if expiry.get("monthOfYear") and expiry.get("year"):
            expiry_str = f"{expiry['monthOfYear']:02d}/{expiry['year']}"

        return {
            "type": "credit_card",
            "brand": raw.get("sello"),
            "description": raw.get("descripcion"),
            "masked_number": raw.get("nroTitularTarjetaWithMask"),
            "holder": raw.get("nombreTitular"),
            "expiry": expiry_str,
            "hash": raw.get("hash"),
            "currency": raw.get("monedaLimite"),
            "limit": raw.get("limiteDeCredito"),
            "account_number": raw.get("nroCuenta"),
            "status": raw.get("estado"),
            "raw": raw,
        }

    # ------------------------------------------------------------------
    # Internal: credit-card move fetchers
    # ------------------------------------------------------------------

    def _fetch_cc_moves(self, card_hash: Optional[str], year: int, month: int) -> list[dict]:
        url_hash = card_hash or (self.credit_cards[0]["hash"] if self.credit_cards else "")
        if not url_hash:
            return []
        url = f"{TRX}/tarjetas/credito/{url_hash}/movimientos_actuales/{year:04d}{month:02d}00"
        r = self._ajax_post(url, b"{}")
        self._check_session(r)
        r.raise_for_status()
        moves = self._unwrap_cc_moves(r.json(), historic=False)
        # Endpoint returns account-wide data regardless of URL hash; filter only if caller specified one.
        if card_hash:
            return [m for m in moves if m.get("tarjeta", {}).get("hash") == card_hash]
        return moves

    @staticmethod
    def _unwrap_cc_moves(data: Any, *, historic: bool) -> list[dict]:
        base = data
        if isinstance(data, dict) and "itaulink_msg" in data:
            base = data["itaulink_msg"].get("data", {})

        paths = (
            [
                ["datos", "datosMovimientos", "movimientos"],
                ["movimientosMesActual", "movimientos"],
            ]
            if not historic
            else [
                ["mapaHistoricos", "movimientosHistoricos", "movimientos"],
                ["movimientosHistoricos", "movimientos"],
            ]
        )

        for path in paths:
            node = base
            for key in path:
                if not isinstance(node, dict):
                    node = None
                    break
                node = node.get(key)
            if isinstance(node, list):
                return node

        if isinstance(data, list):
            return data

        logger.warning(
            "Unknown CC move response shape (historic=%s): %s",
            historic,
            list(base.keys()) if isinstance(base, dict) else type(base),
        )
        return []

    # ------------------------------------------------------------------
    # Internal: bank-account move fetchers
    # ------------------------------------------------------------------

    def _fetch_account_current(self, account_hash: str) -> list[dict]:
        url = f"{TRX}/cuentas/1/{account_hash}/mesActual"
        r = self._ajax_post(url, b"{}")
        self._check_session(r)
        r.raise_for_status()
        data = r.json()
        try:
            return data["itaulink_msg"]["data"]["movimientosMesActual"]["movimientos"]
        except (KeyError, TypeError):
            return []

    def _fetch_account_historic(self, account_hash: str, month: int, year_2d: int) -> list[dict]:
        url = f"{TRX}/cuentas/1/{account_hash}/{month}/{year_2d:02d}/consultaHistorica"
        r = self._ajax_post(url, b"{}")
        try:
            self._check_session(r, allow_no_historic=True)
        except ItauNoHistoric:
            # Account genuinely exposes no historic view — not an error, and a
            # re-login wouldn't help. Report it as an empty period. A real
            # session expiry (marker present) still raises ItauSessionExpired.
            return []
        r.raise_for_status()
        data = r.json()
        try:
            return data["itaulink_msg"]["data"]["mapaHistoricos"]["movimientosHistoricos"]["movimientos"]
        except (KeyError, TypeError):
            return []

    # ------------------------------------------------------------------
    # Internal: shared helpers
    # ------------------------------------------------------------------

    def _ajax_post(self, path: str, body: bytes) -> httpx.Response:
        return self._http.post(
            path,
            content=body,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-CSRF-TOKEN": self._csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Origin": BASE_URL,
                "Referer": f"{BASE_URL}{TRX}/",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            },
        )

    @staticmethod
    def _check_session(response: httpx.Response, *, allow_no_historic: bool = False) -> None:
        """Raise if the data endpoint redirected instead of returning JSON.

        A redirect carrying the `expiredSession` marker is always a real session
        loss (ItauSessionExpired). On the historic endpoint, a *bare* redirect
        (no marker) instead means the account has no historic view — that is not
        a session problem, so raise ItauNoHistoric and let the caller treat it as
        "no movements". Everywhere else, any redirect is treated as expiry.
        """
        loc = response.headers.get("location", "")
        is_redirect = response.status_code in (301, 302, 303)
        expired = "expiredSession" in loc
        if not is_redirect and not expired:
            return
        if allow_no_historic and is_redirect and not expired:
            logger.info("Historic endpoint redirected without expiredSession marker (loc=%r) — treating as no historic view", loc[:120])
            raise ItauNoHistoric("Account has no historic-movements view")
        raise ItauSessionExpired("Itaú session expired — please login again")
