"""
my-itau combined server: REST API + MCP on the same process.

REST endpoints: http://localhost:8787/...
MCP (SSE):      http://localhost:8787/mcp/sse
MCP (HTTP):     http://localhost:8787/mcp/

Claude Desktop config:
  {
    "mcpServers": {
      "my-itau": {
        "command": "my-itau",
        "args": ["mcp"]
      }
    }
  }

Or point at the running server:
  {
    "mcpServers": {
      "my-itau": {
        "url": "http://localhost:8787/mcp/sse"
      }
    }
  }
"""

import logging
import secrets
import threading
from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastmcp import FastMCP
from pydantic import BaseModel

from .client import ItauAuthError, ItauClient, ItauSessionExpired
from .config import any_api_keys_configured, validate_api_key
from .config import credentials as cfg_credentials
from .normalizers import (
    account_to_ob,
    account_transaction,
    card_to_ob,
    cc_transaction,
    is_payment,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("my_itau.server")

# ---------------------------------------------------------------------------
# Auto-session (backed by ~/.my-itau/config.json or env vars)
# ---------------------------------------------------------------------------

_auto_client: Optional[ItauClient] = None
_auto_lock = threading.Lock()



def _auto_login() -> ItauClient:
    doc, pwd = cfg_credentials()
    if not doc or not pwd:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No credentials. Run: my-itau config",
        )
    client = ItauClient()
    try:
        client.login(doc, pwd)
    except ItauAuthError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    return client


def _account_currency(client: ItauClient, account_hash: str) -> str:
    """Return ISO 4217 currency for an account hash, or '' if unknown."""
    acct = next((a for a in client.accounts if a.get("hash") == account_hash), None)
    return acct.get("currency", "") if acct else ""


def get_auto_client() -> ItauClient:
    global _auto_client
    with _auto_lock:
        if _auto_client is None:
            logger.info("Auto-session: logging in")
            _auto_client = _auto_login()
        return _auto_client


def refresh_auto_client() -> ItauClient:
    global _auto_client
    with _auto_lock:
        logger.info("Auto-session: refreshing (session expired)")
        _auto_client = _auto_login()
        return _auto_client


# ---------------------------------------------------------------------------
# Manual session store
# ---------------------------------------------------------------------------

_sessions: dict[str, ItauClient] = {}

# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    if not any_api_keys_configured():
        return  # server is open — no keys defined
    if not x_api_key or validate_api_key(x_api_key) is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired API key")


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
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "my-itau",
    instructions=(
        "Access Itaú Uruguay banking data (Berlin Group / Open Banking format). "
        "Use get_cards to list cards (resourceId field = card identifier). "
        "Use get_moves for transactions, get_summary for totals. "
        "Use get_accounts / get_account_moves for bank account data."
    ),
)


def _fetch_cc_moves(card_hash: Optional[str], month: Optional[int], year: Optional[int]) -> list[dict]:
    from .config import default_card
    client = get_auto_client()
    hash_ = card_hash or default_card() or (client.credit_cards[0]["hash"] if client.credit_cards else None)
    if not hash_:
        return []
    try:
        return client.get_credit_card_moves(hash_, month, year)
    except ItauSessionExpired:
        return refresh_auto_client().get_credit_card_moves(hash_, month, year)


@mcp.tool()
def get_cards() -> list[dict]:
    """List all credit cards. Use resourceId as card identifier in other calls."""
    return [card_to_ob(c) for c in get_auto_client().credit_cards]


@mcp.tool()
def get_moves(
    month: Optional[int] = None,
    year: Optional[int] = None,
    card_hash: Optional[str] = None,
) -> list[dict]:
    """
    Credit card transactions in Open Banking format (Berlin Group).
    Amounts are negative (debit convention). Excludes payment entries.

    Args:
        month:     Month 1-12. Defaults to current month.
        year:      Full year e.g. 2026. Defaults to current year.
        card_hash: resourceId from get_cards(). Defaults to stored default card.
    """
    moves = _fetch_cc_moves(card_hash, month, year)
    return [cc_transaction(m) for m in moves if not is_payment(m)]


@mcp.tool()
def get_summary(
    month: Optional[int] = None,
    year: Optional[int] = None,
    card_hash: Optional[str] = None,
) -> dict:
    """
    Total spent per currency. Use for 'what's my total / balance / spending'.
    Amounts are positive totals (absolute value of debits).

    Args:
        month:     Month 1-12. Defaults to current month.
        year:      Full year e.g. 2026. Defaults to current year.
        card_hash: resourceId from get_cards(). Defaults to stored default card.
    """
    moves = _fetch_cc_moves(card_hash, month, year)
    totals: dict[str, float] = {}
    count = 0
    for m in moves:
        if is_payment(m):
            continue
        from .normalizers import currency_code
        cur = currency_code(m.get("moneda") or "")
        totals[cur] = round(totals.get(cur, 0.0) + abs(float(m.get("importe") or 0)), 2)
        count += 1

    return {
        "period": f"{month or 'current'}/{year or 'current'}",
        "transactionCount": count,
        "totals": [{"currency": cur, "amount": f"{amt:.2f}"} for cur, amt in totals.items()],
    }


@mcp.tool()
def get_accounts() -> list[dict]:
    """List bank accounts in Open Banking format (Berlin Group)."""
    return [account_to_ob(a) for a in get_auto_client().accounts]


@mcp.tool()
def get_account_moves(
    account_hash: str,
    month: Optional[int] = None,
    year: Optional[int] = None,
) -> list[dict]:
    """
    Bank account transactions in Open Banking format (Berlin Group).

    Args:
        account_hash: resourceId from get_accounts().
        month:        Month 1-12. Defaults to current month.
        year:         Full year e.g. 2026. Defaults to current year.
    """
    client = get_auto_client()
    cur = _account_currency(client, account_hash)
    try:
        moves = client.get_account_moves(account_hash, month, year)
    except ItauSessionExpired:
        client = refresh_auto_client()
        cur = _account_currency(client, account_hash)
        moves = client.get_account_moves(account_hash, month, year)
    return [account_transaction(m, cur) for m in moves]


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    document_number: str
    password: str


class LoginResponse(BaseModel):
    session_token: str
    accounts: list[dict]
    credit_cards: list[dict]


app = FastAPI(
    title="my-itau",
    description="Itaú Uruguay banking API + MCP server.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount MCP — SSE transport at /mcp/sse, streamable-HTTP at /mcp/
app.mount("/mcp", mcp.http_app())


# ---------------------------------------------------------------------------
# Simple endpoints (auto-session)
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    return {"status": "ok", "utc": datetime.utcnow().isoformat()}


@app.get("/status", dependencies=[Depends(require_api_key)])
def api_status():
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
        },
    }


@app.get("/moves", dependencies=[Depends(require_api_key)])
def rest_get_moves(month: Optional[int] = None, year: Optional[int] = None):
    """CC transactions for default card (OB format)."""
    from .config import default_card
    client = get_auto_client()
    default = default_card()
    card = (
        next((c for c in client.credit_cards if c["hash"] == default), None)
        if default else (client.credit_cards[0] if client.credit_cards else None)
    )
    if not card:
        raise HTTPException(status_code=404, detail="No credit cards found. Run: my-itau set-card")
    try:
        moves = client.get_credit_card_moves(card["hash"], month, year)
    except ItauSessionExpired:
        moves = refresh_auto_client().get_credit_card_moves(card["hash"], month, year)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"transactions": {"booked": [cc_transaction(m) for m in moves if not is_payment(m)]}}


@app.get("/moves/{card_hash}", dependencies=[Depends(require_api_key)])
def rest_get_moves_for_card(card_hash: str, month: Optional[int] = None, year: Optional[int] = None):
    client = get_auto_client()
    try:
        moves = client.get_credit_card_moves(card_hash, month, year)
    except ItauSessionExpired:
        moves = refresh_auto_client().get_credit_card_moves(card_hash, month, year)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"transactions": {"booked": [cc_transaction(m) for m in moves if not is_payment(m)]}}


@app.get("/cards", dependencies=[Depends(require_api_key)])
def rest_get_cards():
    return {"paymentAccounts": [card_to_ob(c) for c in get_auto_client().credit_cards]}


@app.get("/accounts", dependencies=[Depends(require_api_key)])
def rest_get_accounts():
    return {"accounts": [account_to_ob(a) for a in get_auto_client().accounts]}


# ---------------------------------------------------------------------------
# Advanced endpoints (manual session token)
# ---------------------------------------------------------------------------


@app.post("/login", response_model=LoginResponse, dependencies=[Depends(require_api_key)])
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


@app.get("/credit-cards", dependencies=[Depends(require_api_key)])
def list_credit_cards(client: ItauClient = Depends(get_manual_session)):
    return {"paymentAccounts": [card_to_ob(c) for c in client.credit_cards]}


@app.get("/credit-cards/{card_hash}/moves")
def credit_card_moves(
    card_hash: str,
    month: Optional[int] = None,
    year: Optional[int] = None,
    client: ItauClient = Depends(get_manual_session),
):
    try:
        moves = client.get_credit_card_moves(card_hash, month, year)
    except ItauSessionExpired:
        raise HTTPException(status_code=401, detail="Session expired. POST /login again.")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"transactions": {"booked": [cc_transaction(m) for m in moves if not is_payment(m)]}}


@app.get("/account-moves/{account_hash}")
def account_moves(
    account_hash: str,
    month: Optional[int] = None,
    year: Optional[int] = None,
    client: ItauClient = Depends(get_manual_session),
):
    cur = _account_currency(client, account_hash)
    try:
        moves = client.get_account_moves(account_hash, month, year)
    except ItauSessionExpired:
        raise HTTPException(status_code=401, detail="Session expired. POST /login again.")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"transactions": {"booked": [account_transaction(m, cur) for m in moves]}}


@app.delete("/logout", dependencies=[Depends(require_api_key)])
def logout(x_session_token: Optional[str] = Header(default=None)):
    if x_session_token and x_session_token in _sessions:
        del _sessions[x_session_token]
    return {"status": "logged out"}


# ---------------------------------------------------------------------------
# /v1  Open Banking (Berlin Group) endpoints
# ---------------------------------------------------------------------------

from fastapi import APIRouter

v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


@v1.get("/accounts")
def v1_accounts():
    """Berlin Group: account list."""
    return {"accounts": [account_to_ob(a) for a in get_auto_client().accounts]}


@v1.get("/accounts/{account_id}/transactions")
def v1_account_transactions(
    account_id: str,
    month: Optional[int] = None,
    year: Optional[int] = None,
):
    """Berlin Group: account transaction list."""
    client = get_auto_client()
    cur = _account_currency(client, account_id)
    try:
        moves = client.get_account_moves(account_id, month, year)
    except ItauSessionExpired:
        client = refresh_auto_client()
        cur = _account_currency(client, account_id)
        moves = client.get_account_moves(account_id, month, year)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"transactions": {"booked": [account_transaction(m, cur) for m in moves]}}


@v1.get("/cards")
def v1_cards():
    """Berlin Group: payment instrument (card) list."""
    return {"paymentAccounts": [card_to_ob(c) for c in get_auto_client().credit_cards]}


@v1.get("/cards/{card_id}/transactions")
def v1_card_transactions(
    card_id: str,
    month: Optional[int] = None,
    year: Optional[int] = None,
):
    """Berlin Group: card transaction list. Excludes payment entries."""
    client = get_auto_client()
    try:
        moves = client.get_credit_card_moves(card_id, month, year)
    except ItauSessionExpired:
        moves = refresh_auto_client().get_credit_card_moves(card_id, month, year)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {
        "transactions": {
            "booked": [cc_transaction(m) for m in moves if not is_payment(m)]
        }
    }


app.include_router(v1)
