"""
Itaú Uruguay scraper client.

Correct login + data-fetch flow (reverse-engineered, 2026):

  1. GET  /trx/                       → prime session cookies
  2. POST /trx/doLogin                → form-based auth; redirects to /trx/ on success
  3. GET  /trx/                       → dashboard HTML; parse accounts + CSRF token
  4. POST /trx/tarjetas/credito       → AJAX; returns credit-card list with hashes
  5. POST /trx/tarjetas/credito/{hash}/movimientos_actuales/00000000   → current-month CC moves
     POST /trx/tarjetas/credito/{hash}/movimientos_mes/{month}/{year:02d} → historic CC moves
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

logger = logging.getLogger("itau_client")

BASE_URL = "https://www.itaulink.com.uy"
TRX = "/trx"

ERROR_CODES: dict[str, str] = {
    "10010": "Bad document number / login",
    "10020": "Bad password",
    "10030": "Account blocked",
}

# Segment at the end of the movimientos URL — appears to be always 00000000
_CC_MOVES_SUFFIX = "00000000"


class ItauAuthError(Exception):
    pass


class ItauSessionExpired(Exception):
    pass


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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def login(self, document_number: str, password: str) -> None:
        """Authenticate. Raises ItauAuthError on bad credentials."""
        logger.info("Logging in as %s", document_number)
        self._prime_session()
        redirect = self._do_login(document_number, password)
        logger.debug("Login redirect → %s", redirect)

        if "message_code=" in redirect:
            m = re.search(r"message_code=(\d+)", redirect)
            code = m.group(1) if m else "unknown"
            raise ItauAuthError(ERROR_CODES.get(code, f"Login failed (code {code})"))

        self._load_dashboard()
        self._load_credit_cards()

    def get_credit_card_moves(
        self,
        card_hash: str,
        month: Optional[int] = None,
        year: Optional[int] = None,
    ) -> list[dict]:
        """Credit-card movements. Defaults to current month."""
        today = date.today()
        month = month or today.month
        year = year or today.year
        year_2d = year - 2000 if year > 2000 else year

        try:
            if month == today.month and year == today.year:
                return self._fetch_cc_current(card_hash)
            return self._fetch_cc_historic(card_hash, month, year_2d)
        except ItauSessionExpired:
            raise

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

        try:
            if month == today.month and year == today.year:
                return self._fetch_account_current(account_hash)
            return self._fetch_account_historic(account_hash, month, year_2d)
        except ItauSessionExpired:
            raise

    # ------------------------------------------------------------------
    # Internal: session bootstrap
    # ------------------------------------------------------------------

    def _prime_session(self) -> None:
        """GET /trx/ so the server sets JSESSIONID before we POST credentials."""
        try:
            self._http.get(TRX + "/")
        except httpx.HTTPError as e:
            logger.warning("Could not prime session: %s", e)

    def _do_login(self, document_number: str, password: str) -> str:
        """POST credentials. Returns the Location header of the redirect."""
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
        """
        GET /trx/ after login.
        Extracts CSRF token and bank accounts from the embedded JS blob.
        """
        r = self._http.get(TRX + "/")
        r.raise_for_status()
        html = r.text

        # CSRF token
        m = re.search(r'<meta name="_csrf"\s+content="([^"]+)"', html)
        self._csrf = m.group(1) if m else ""
        logger.debug("CSRF: %s", self._csrf)

        # Bank accounts are embedded in `var mensajeUsuario = JSON.parse('...')`
        # Extract from first '{' to last '}' on that line (avoids regex issues with
        # escaped quotes inside the JSON string).
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
            for key in ("CAJA_DE_AHORRO", "CUENTA_CORRIENTE", "CUENTA_RECAUDADORA", "CUENTA_DE_AHORRO_JUNIOR", "CUENTA_DE_ALIMENTACION")
            for a in cuentas.get(key, [])
        ]
        logger.info("Loaded %d bank accounts", len(self.accounts))

    def _load_credit_cards(self) -> None:
        """
        POST /trx/tarjetas/credito → JSON with objetosTarjetaCredito.tarjetaImagen.
        Each element of tarjetaImagen is an array of card face objects; we take [0].hash.
        """
        r = self._ajax_post(TRX + "/tarjetas/credito", b"{}")
        if r.status_code != 200:
            loc = r.headers.get("location", "")
            logger.warning("Credit-card list failed: HTTP %d loc=%s", r.status_code, loc)
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
        tarjeta_imagen = tarjeta_data.get("tarjetaImagen", [])  # list of lists

        cards = []
        for group in tarjeta_imagen:
            # Each group is a list of card face objects for the same card
            if isinstance(group, list) and group:
                primary = group[0]
            elif isinstance(group, dict):
                primary = group
            else:
                continue
            cards.append(self._normalise_card(primary))

        self.credit_cards = cards
        logger.info("Loaded %d credit cards", len(self.credit_cards))

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

    def _fetch_cc_current(self, card_hash: str) -> list[dict]:
        url = f"{TRX}/tarjetas/credito/{card_hash}/movimientos_actuales/{_CC_MOVES_SUFFIX}"
        r = self._ajax_post(url, b"{}")
        self._check_session(r)
        r.raise_for_status()
        return self._unwrap_cc_moves(r.json(), historic=False)

    def _fetch_cc_historic(self, card_hash: str, month: int, year_2d: int) -> list[dict]:
        url = f"{TRX}/tarjetas/credito/{card_hash}/movimientos_mes/{month}/{year_2d:02d}"
        r = self._ajax_post(url, b"{}")
        self._check_session(r)
        r.raise_for_status()
        return self._unwrap_cc_moves(r.json(), historic=True)

    @staticmethod
    def _unwrap_cc_moves(data: Any, *, historic: bool) -> list[dict]:
        """
        Try known response shapes and return a flat list of movement dicts.
        The response is wrapped as:
          itaulink_msg.data.datos.datosMovimientos.movimientos   (current)
          itaulink_msg.data.mapaHistoricos.movimientosHistoricos.movimientos (historic)
        """
        base = data
        if isinstance(data, dict) and "itaulink_msg" in data:
            base = data["itaulink_msg"].get("data", {})

        if not historic:
            paths = [
                ["datos", "datosMovimientos", "movimientos"],
                ["movimientosMesActual", "movimientos"],
            ]
        else:
            paths = [
                ["mapaHistoricos", "movimientosHistoricos", "movimientos"],
                ["movimientosHistoricos", "movimientos"],
            ]

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
        self._check_session(r)
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
        """POST with the headers every AJAX call on itaulink requires."""
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
    def _check_session(response: httpx.Response) -> None:
        loc = response.headers.get("location", "")
        if response.status_code in (301, 302, 303) or "expiredSession" in loc:
            raise ItauSessionExpired("Itaú session expired — please login again")
