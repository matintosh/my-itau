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
from datetime import datetime
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
    Returns moves for every credit card, grouped by card.
    Use ?month=3&year=2026 to fetch a specific month.
    Omit query params for the current month.
    """
    client = get_auto_client()

    results = []
    for card in client.credit_cards:
        card_hash = card.get("hash")
        if not card_hash:
            continue
        try:
            moves = client.get_credit_card_moves(card_hash, month, year)
        except ItauSessionExpired:
            client = refresh_auto_client()
            moves = client.get_credit_card_moves(card_hash, month, year)
        except Exception as e:
            logger.exception("Error fetching moves for card %s", card_hash[:12])
            moves = []

        results.append({
            "card": {
                "hash": card_hash,
                "brand": card.get("brand"),
                "masked_number": card.get("masked_number"),
                "holder": card.get("holder"),
            },
            "month": month,
            "year": year,
            "count": len(moves),
            "moves": moves,
        })

    return {"cards": results}


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
    """Fetch moves for a single card hash. Auto-refreshes session on expiry."""
    client = get_auto_client()
    try:
        moves = client.get_credit_card_moves(card_hash, month, year)
    except ItauSessionExpired:
        client = refresh_auto_client()
        moves = client.get_credit_card_moves(card_hash, month, year)
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
