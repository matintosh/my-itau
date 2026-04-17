"""
Itaú Uruguay API server.

Simple usage (credentials in .env):
  GET  /moves               — current-month CC moves for all cards
  GET  /moves?month=3&year=2026  — specific month
  GET  /cards               — list credit cards with hashes

Advanced (manage your own session):
  POST /login               — returns session_token
  GET  /credit-cards/{hash}/moves  — moves for one card (requires X-Session-Token)
  GET  /accounts/{hash}/moves      — bank-account moves (requires X-Session-Token)
  DELETE /logout

All endpoints require X-Api-Key header when API_KEY env var is set.
"""

import logging
import os
import secrets
import threading
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from itau_client import ItauAuthError, ItauClient, ItauSessionExpired

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("main")

API_KEY = os.getenv("API_KEY", "")
ITAU_DOC = os.getenv("ITAU_DOCUMENT_NUMBER", "")
ITAU_PWD = os.getenv("ITAU_PASSWORD", "")

app = FastAPI(
    title="Itaú UY API",
    description="Thin wrapper around itaulink.com.uy for personal use.",
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# Auto-session: single shared session backed by .env credentials
# ---------------------------------------------------------------------------

_auto_client: Optional[ItauClient] = None
_auto_lock = threading.Lock()

_MOVES_TTL = timedelta(minutes=10)
_moves_cache: Optional[dict] = None
_moves_cache_expires: Optional[datetime] = None


def _auto_login() -> ItauClient:
    """Create a fresh ItauClient and log in with .env credentials."""
    if not ITAU_DOC or not ITAU_PWD:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ITAU_DOCUMENT_NUMBER / ITAU_PASSWORD not set in .env",
        )
    client = ItauClient()
    try:
        client.login(ITAU_DOC, ITAU_PWD)
    except ItauAuthError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    return client


def get_auto_client() -> ItauClient:
    """Return (or create) the shared auto-session client."""
    global _auto_client
    with _auto_lock:
        if _auto_client is None:
            logger.info("Auto-session: logging in")
            _auto_client = _auto_login()
        return _auto_client


def refresh_auto_client() -> ItauClient:
    """Force a fresh login for the auto-session (called on session expiry)."""
    global _auto_client
    with _auto_lock:
        logger.info("Auto-session: refreshing (session expired)")
        _auto_client = _auto_login()
        return _auto_client


# ---------------------------------------------------------------------------
# Manual session store: session_token → ItauClient
# ---------------------------------------------------------------------------

_sessions: dict[str, ItauClient] = {}

# ---------------------------------------------------------------------------
# Auth guards
# ---------------------------------------------------------------------------


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


def get_manual_session(
    x_session_token: Optional[str] = Header(default=None),
    _: None = Depends(require_api_key),
) -> ItauClient:
    if not x_session_token or x_session_token not in _sessions:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-Session-Token. POST /login first.",
        )
    return _sessions[x_session_token]


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    document_number: str
    password: str


class LoginResponse(BaseModel):
    session_token: str
    accounts: list[dict]
    credit_cards: list[dict]


# ---------------------------------------------------------------------------
# Simple endpoints (auto-session, no token management needed)
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    return {"status": "ok", "utc": datetime.utcnow().isoformat()}


@app.get("/status", dependencies=[Depends(require_api_key)])
def status():
    now = datetime.utcnow()
    session_ready = _auto_client is not None
    cache_valid = (
        _moves_cache is not None
        and _moves_cache_expires is not None
        and now < _moves_cache_expires
    )
    return {
        "session": {
            "ready": session_ready,
            "accounts": len(_auto_client.accounts) if session_ready else 0,
            "credit_cards": len(_auto_client.credit_cards) if session_ready else 0,
        },
        "cache": {
            "valid": cache_valid,
            "expires": _moves_cache_expires.isoformat() if _moves_cache_expires else None,
            "expires_in_seconds": (
                int((_moves_cache_expires - now).total_seconds())
                if cache_valid else None
            ),
            "move_count": len((_moves_cache or {}).get("data", {}).get("datos", {}).get("datosMovimientos", {}).get("movimientos", [])) if cache_valid else None,
        },
    }


@app.get(
    "/moves",
    summary="CC moves (auto-auth)",
    description=(
        "Returns credit-card transactions for all cards using credentials from .env. "
        "The session is cached and refreshed automatically on expiry."
    ),
    dependencies=[Depends(require_api_key)],
)
def get_moves(month: Optional[int] = None, year: Optional[int] = None):
    """
    Returns first credit card moves. Current month is cached for 10 minutes.
    Use ?month=3&year=2026 for a specific month (not cached).
    """
    global _moves_cache, _moves_cache_expires

    now = datetime.utcnow()
    is_current_month = month is None and year is None
    if is_current_month and _moves_cache and _moves_cache_expires and now < _moves_cache_expires:
        logger.info("Serving /moves from cache (expires %s)", _moves_cache_expires.strftime("%H:%M:%S"))
        return _moves_cache

    client = get_auto_client()
    card = client.credit_cards[0] if client.credit_cards else None
    if not card:
        raise HTTPException(status_code=404, detail="No credit cards found on this account")

    card_hash = card.get("hash")
    try:
        payload = client.get_credit_card_payload(card_hash, month, year)
    except ItauSessionExpired:
        client = refresh_auto_client()
        payload = client.get_credit_card_payload(card_hash, month, year)

    if is_current_month:
        _moves_cache = payload
        _moves_cache_expires = now + _MOVES_TTL
        logger.info("Cached /moves until %s", _moves_cache_expires.strftime("%H:%M:%S"))

    return payload


@app.get(
    "/moves/{card_hash}",
    summary="CC moves for one card (auto-auth)",
    dependencies=[Depends(require_api_key)],
)
def get_moves_for_card(
    card_hash: str,
    month: Optional[int] = None,
    year: Optional[int] = None,
):
    """Raw itaulink_msg payload for a specific card. Same shape as itau-example.json."""
    client = get_auto_client()
    try:
        payload = client.get_credit_card_payload(card_hash, month, year)
    except ItauSessionExpired:
        client = refresh_auto_client()
        payload = client.get_credit_card_payload(card_hash, month, year)
    except Exception as e:
        logger.exception("Error fetching CC moves")
        raise HTTPException(status_code=502, detail=str(e))

    return payload


@app.get(
    "/cards",
    summary="List credit cards (auto-auth)",
    dependencies=[Depends(require_api_key)],
)
def get_cards():
    """Return all credit cards for the .env account."""
    client = get_auto_client()
    return {
        "credit_cards": [
            {
                "hash": c.get("hash"),
                "brand": c.get("brand"),
                "masked_number": c.get("masked_number"),
                "holder": c.get("holder"),
                "expiry": c.get("expiry"),
                "currency": c.get("currency"),
                "limit": c.get("limit"),
                "account_number": c.get("account_number"),
                "status": c.get("status"),
            }
            for c in client.credit_cards
        ]
    }


# ---------------------------------------------------------------------------
# Advanced endpoints (caller manages session token)
# ---------------------------------------------------------------------------


@app.post(
    "/login",
    response_model=LoginResponse,
    summary="Login with custom credentials",
    dependencies=[Depends(require_api_key)],
)
def login(body: LoginRequest):
    client = ItauClient()
    try:
        client.login(body.document_number, body.password)
    except ItauAuthError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))

    token = secrets.token_urlsafe(32)
    _sessions[token] = client
    return LoginResponse(
        session_token=token,
        accounts=client.accounts,
        credit_cards=client.credit_cards,
    )


@app.get("/accounts", summary="List bank accounts (session token required)")
def list_accounts(client: ItauClient = Depends(get_manual_session)):
    return {"accounts": client.accounts}


@app.get("/credit-cards", summary="List credit cards (session token required)")
def list_credit_cards(client: ItauClient = Depends(get_manual_session)):
    return {"credit_cards": client.credit_cards}


@app.get("/credit-cards/{card_hash}/moves", summary="CC moves (session token required)")
def credit_card_moves(
    card_hash: str,
    month: Optional[int] = None,
    year: Optional[int] = None,
    client: ItauClient = Depends(get_manual_session),
):
    try:
        moves = client.get_credit_card_moves(card_hash, month, year)
    except ItauSessionExpired:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Itaú session expired. POST /login again.",
        )
    except Exception as e:
        logger.exception("Error fetching CC moves")
        raise HTTPException(status_code=502, detail=str(e))

    return {
        "card_hash": card_hash,
        "month": month,
        "year": year,
        "count": len(moves),
        "moves": moves,
    }


@app.get("/accounts/{account_hash}/moves", summary="Account moves (session token required)")
def account_moves(
    account_hash: str,
    month: Optional[int] = None,
    year: Optional[int] = None,
    client: ItauClient = Depends(get_manual_session),
):
    try:
        moves = client.get_account_moves(account_hash, month, year)
    except ItauSessionExpired:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Itaú session expired. POST /login again.",
        )
    except Exception as e:
        logger.exception("Error fetching account moves")
        raise HTTPException(status_code=502, detail=str(e))

    return {
        "account_hash": account_hash,
        "month": month,
        "year": year,
        "count": len(moves),
        "moves": moves,
    }


@app.delete("/logout", summary="Invalidate a manual session token")
def logout(
    x_session_token: Optional[str] = Header(default=None),
    _: None = Depends(require_api_key),
):
    if x_session_token and x_session_token in _sessions:
        del _sessions[x_session_token]
    return {"status": "logged out"}
