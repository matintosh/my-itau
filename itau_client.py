"""
Itaú Uruguay scraper client.

Login flow (reverse-engineered from itaulink.com.uy):
  1. GET  /trx/                        — grab cookies + _csrf token from HTML
  2. POST /trx/doLogin                 — form-based login
     Redirects to /trx/ on success, or /trx/?message_code=XXXXX on failure.
  3. GET  /trx/                        — download the JS bundle that contains
     the user's accounts as a JSON blob embedded in `var mensajeUsuario`.

Credit-card moves:
  POST /trx/tarjetas/1/{hash}/mesActual               — current month
  POST /trx/tarjetas/1/{hash}/{month}/{year:02d}/consultaHistorica  — past months
"""

import logging
import re
import json
from datetime import date
from typing import Any, Optional

import httpx

logger = logging.getLogger("itau_client")

BASE_URL = "https://www.itaulink.com.uy/trx"

ERROR_CODES: dict[str, str] = {
    "10010": "Bad document / login",
    "10020": "Bad password",
    "10030": "Account blocked",
}


class ItauAuthError(Exception):
    pass


class ItauSessionExpired(Exception):
    pass


class ItauClient:
    """
    Stateful client for itaulink.com.uy.
    Maintains a cookie jar across calls so callers can re-use the session.
    """

    def __init__(self) -> None:
        self._http = httpx.Client(
            base_url=BASE_URL,
            follow_redirects=False,
            timeout=30,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "es-UY,es;q=0.9",
            },
        )
        self.accounts: list[dict] = []
        self.credit_cards: list[dict] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def login(self, document_number: str, password: str) -> None:
        """
        Authenticate against itaulink.com.uy.
        Raises ItauAuthError on bad credentials.
        """
        logger.info("Logging in as %s", document_number)
        self._prime_session()
        redirect = self._do_login(document_number, password)
        logger.debug("Login redirect → %s", redirect)

        # Success: redirect to the dashboard (various possible URLs)
        # Failure: redirect contains message_code= query param
        if "message_code=" not in redirect:
            self._load_dashboard()
            return

        # Error path
        match = re.search(r"message_code=(\d+)", redirect)
        code = match.group(1) if match else "unknown"
        raise ItauAuthError(ERROR_CODES.get(code, f"Login failed (code {code})"))

    def get_credit_card_moves(
        self, card_hash: str, month: Optional[int] = None, year: Optional[int] = None
    ) -> list[dict]:
        """
        Returns credit-card movements for the given card hash.
        Defaults to the current month when month/year are omitted.
        """
        today = date.today()
        month = month or today.month
        year = year or today.year

        year_2d = year - 2000 if year > 2000 else year

        try:
            if month == today.month and year == today.year:
                return self._fetch_cc_current(card_hash)
            return self._fetch_cc_historic(card_hash, month, year_2d)
        except ItauSessionExpired:
            logger.warning("Session expired — re-authenticating is the caller's responsibility")
            raise

    def get_account_moves(
        self, account_hash: str, month: Optional[int] = None, year: Optional[int] = None
    ) -> list[dict]:
        """
        Returns bank-account movements for the given account hash.
        Mirrors credit-card API but uses /cuentas/ path.
        """
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
    # Internal helpers
    # ------------------------------------------------------------------

    def _prime_session(self) -> None:
        """GET the main page so the server sets initial cookies."""
        try:
            r = self._http.get("/")
            logger.debug("Prime session status %s", r.status_code)
        except httpx.HTTPError as e:
            logger.warning("Could not prime session: %s", e)

    def _do_login(self, document_number: str, password: str) -> str:
        """POST credentials. Returns the Location header of the redirect."""
        r = self._http.post(
            "/doLogin",
            data={
                "tipo_documento": "1",   # Cédula de identidad
                "tipo_usuario": "R",      # Retail / personal
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
        GET /trx/ after login — parse `var mensajeUsuario = JSON.parse(...)` 
        to extract accounts and credit cards.
        """
        r = self._http.get("/")
        r.raise_for_status()

        lines = r.text.splitlines()
        target_line = next(
            (l for l in lines if "var mensajeUsuario = JSON.parse" in l), None
        )
        if target_line is None:
            # Try alternative: some portal versions embed it differently
            target_line = next(
                (l for l in lines if "mensajeUsuario" in l and "JSON.parse" in l), None
            )

        if target_line is None:
            logger.warning(
                "Could not find mensajeUsuario in dashboard. "
                "Account/card list will be empty."
            )
            return

        m = re.search(r"JSON\.parse\((['\"])(.*?)\1\)", target_line)
        if not m:
            logger.warning("Could not extract JSON from mensajeUsuario line.")
            return

        raw = m.group(2).replace("\\'", "'").replace('\\"', '"')
        try:
            user_data: dict = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse mensajeUsuario JSON: %s", e)
            return

        cuentas = user_data.get("cuentas", {})
        self.accounts = [
            self._normalise_account(a)
            for bucket in (
                cuentas.get("caja_de_ahorro", []),
                cuentas.get("cuenta_corriente", []),
                cuentas.get("cuenta_recaudadora", []),
                cuentas.get("cuenta_de_ahorro_junior", []),
            )
            for a in bucket
        ]

        tarjetas_raw = user_data.get("tarjetas", [])
        if isinstance(tarjetas_raw, list):
            self.credit_cards = [self._normalise_card(t) for t in tarjetas_raw]
        elif isinstance(tarjetas_raw, dict):
            # Sometimes it's keyed by type
            self.credit_cards = [
                self._normalise_card(t)
                for cards in tarjetas_raw.values()
                for t in (cards if isinstance(cards, list) else [cards])
            ]

        logger.info(
            "Dashboard loaded: %d accounts, %d credit cards",
            len(self.accounts),
            len(self.credit_cards),
        )

    @staticmethod
    def _normalise_account(raw: dict) -> dict:
        return {
            "type": raw.get("tipoCuenta"),
            "id": raw.get("idCuenta"),
            "user": raw.get("nombreTitular"),
            "currency": raw.get("moneda"),
            "balance": raw.get("saldo"),
            "hash": raw.get("hash"),
            "customer_hash": raw.get("hashCustomer"),
        }

    @staticmethod
    def _normalise_card(raw: dict) -> dict:
        return {
            "type": "credit_card",
            "brand": raw.get("sello") or raw.get("tipoTarjeta"),
            "last_digits": raw.get("nroTarjeta") or raw.get("ultimosDigitos"),
            "hash": raw.get("hash"),
            "customer_hash": raw.get("hashCustomer"),
            "currency": raw.get("moneda"),
            "limit": raw.get("limite"),
            "balance": raw.get("saldo"),
        }

    # ---------- Credit-card endpoints -----------------------------------

    def _fetch_cc_current(self, card_hash: str) -> list[dict]:
        r = self._http.post(
            f"/tarjetas/1/{card_hash}/mesActual",
            headers={"Accept": "application/json"},
        )
        self._check_session(r)
        r.raise_for_status()
        data: Any = r.json()
        return self._extract_cc_moves(data)

    def _fetch_cc_historic(self, card_hash: str, month: int, year_2d: int) -> list[dict]:
        r = self._http.post(
            f"/tarjetas/1/{card_hash}/{month}/{year_2d:02d}/consultaHistorica",
            headers={"Accept": "application/json"},
        )
        self._check_session(r)
        r.raise_for_status()
        data: Any = r.json()
        return self._extract_cc_moves_historic(data)

    @staticmethod
    def _extract_cc_moves(data: Any) -> list[dict]:
        """Unwrap current-month credit card response."""
        # Shape A: data.itaulink_msg.data.datos.datosMovimientos.movimientos
        try:
            return (
                data["itaulink_msg"]["data"]["datos"]["datosMovimientos"]["movimientos"]
            )
        except (KeyError, TypeError):
            pass
        # Shape B: data.datos.datosMovimientos.movimientos  (used in older JS client)
        try:
            return data["datos"]["datosMovimientos"]["movimientos"]
        except (KeyError, TypeError):
            pass
        # Shape C: already a list
        if isinstance(data, list):
            return data
        logger.warning("Unknown credit-card response shape: %s", list(data.keys()) if isinstance(data, dict) else type(data))
        return []

    @staticmethod
    def _extract_cc_moves_historic(data: Any) -> list[dict]:
        """Unwrap historic credit card response."""
        try:
            return (
                data["itaulink_msg"]["data"]["mapaHistoricos"]
                ["movimientosHistoricos"]["movimientos"]
            )
        except (KeyError, TypeError):
            pass
        try:
            return data["mapaHistoricos"]["movimientosHistoricos"]["movimientos"]
        except (KeyError, TypeError):
            pass
        if isinstance(data, list):
            return data
        logger.warning("Unknown historic CC response shape: %s", list(data.keys()) if isinstance(data, dict) else type(data))
        return []

    # ---------- Bank-account endpoints ----------------------------------

    def _fetch_account_current(self, account_hash: str) -> list[dict]:
        r = self._http.post(
            f"/cuentas/1/{account_hash}/mesActual",
            headers={"Accept": "application/json"},
        )
        self._check_session(r)
        r.raise_for_status()
        data = r.json()
        try:
            return data["itaulink_msg"]["data"]["movimientosMesActual"]["movimientos"]
        except (KeyError, TypeError):
            return []

    def _fetch_account_historic(self, account_hash: str, month: int, year_2d: int) -> list[dict]:
        r = self._http.post(
            f"/cuentas/1/{account_hash}/{month}/{year_2d:02d}/consultaHistorica",
            headers={"Accept": "application/json"},
        )
        self._check_session(r)
        r.raise_for_status()
        data = r.json()
        try:
            return data["itaulink_msg"]["data"]["mapaHistoricos"]["movimientosHistoricos"]["movimientos"]
        except (KeyError, TypeError):
            return []

    # ---------- Session check ------------------------------------------

    @staticmethod
    def _check_session(response: httpx.Response) -> None:
        location = response.headers.get("location", "")
        if "expiredSession" in location or response.status_code in (301, 302, 303):
            raise ItauSessionExpired("Session expired")
