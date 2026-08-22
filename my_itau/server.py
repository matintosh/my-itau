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

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastmcp import FastMCP
from pydantic import BaseModel

from .client import ItauAuthError, ItauClient, ItauNoHistoric, ItauSessionExpired
from .config import any_api_keys_configured, validate_api_key
from .config import credentials as cfg_credentials
from .normalizers import (
    account_to_ob,
    account_transaction,
    account_transaction_signed,
    card_to_ob,
    cc_transaction,
    currency_code,
    is_payment,
    pending_authorization,
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
        raise api_error("not_configured", "No credentials. Run: my-itau config", status.HTTP_503_SERVICE_UNAVAILABLE)
    client = ItauClient()
    try:
        client.login(doc, pwd)
    except ItauAuthError as e:
        if getattr(e, "code", None) == "10030":
            raise api_error("account_blocked", str(e), status.HTTP_429_TOO_MANY_REQUESTS)
        raise api_error("auth_failed", str(e), status.HTTP_401_UNAUTHORIZED)
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
        raise api_error("invalid_api_key", "Invalid or expired API key", status.HTTP_401_UNAUTHORIZED)


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
    """Fetch CC moves. card_hash=None returns all cards on the account."""
    client = get_auto_client()
    if not client.credit_cards:
        return []
    try:
        return client.get_credit_card_moves(card_hash, month, year)
    except ItauSessionExpired:
        # Transient expiry — re-login once and retry. A persistent expiry
        # propagates (surfaces as 401) rather than masquerading as "no moves".
        return refresh_auto_client().get_credit_card_moves(card_hash, month, year)


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
        card_hash: resourceId from get_cards(). Omit for all cards on the account.
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
        card_hash: resourceId from get_cards(). Omit for all cards on the account.
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
def get_authorizations(card_hash: Optional[str] = None) -> list[dict]:
    """
    Approved pending credit card authorizations in Open Banking format.
    These are charges authorized but not yet settled as booked transactions.
    importe may be 0 for pre-authorizations without a confirmed amount.

    Args:
        card_hash: resourceId from get_cards() to filter to one card.
                   Omit to get all cards' pending authorizations.
    """
    client = get_auto_client()
    try:
        auths = client.get_pending_authorizations(card_hash)
    except ItauSessionExpired:
        auths = refresh_auto_client().get_pending_authorizations(card_hash)
    return [pending_authorization(a) for a in auths]


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
# Builders shared by the new per-card / account-flow surfaces
# ---------------------------------------------------------------------------


def _spend_totals(moves: list[dict]) -> list[dict]:
    """Per-currency spend totals (absolute debits) for a list of raw CC moves."""
    totals: dict[str, float] = {}
    for m in moves:
        if is_payment(m):
            continue
        cur = currency_code(m.get("moneda") or "")
        totals[cur] = round(totals.get(cur, 0.0) + abs(float(m.get("importe") or 0)), 2)
    return [{"currency": cur, "amount": f"{amt:.2f}"} for cur, amt in totals.items()]


def _build_cards_grouped(
    client: ItauClient, month: Optional[int], year: Optional[int]
) -> list[dict]:
    """Every card with its own booked moves nested under it.

    One upstream fetch (the CC endpoint is account-wide), grouped client-side by
    ``tarjeta.hash`` — avoids hammering Itaú with one request per card.
    """
    moves = client.get_credit_card_moves(None, month, year)
    groups: dict[str, list[dict]] = {}
    for m in moves:
        if is_payment(m):
            continue
        h = (m.get("tarjeta") or {}).get("hash") or ""
        groups.setdefault(h, []).append(m)

    result = []
    for c in client.credit_cards:
        h = c.get("hash") or ""
        card_moves = groups.get(h, [])
        result.append({
            "card": card_to_ob(c),
            "transactions": {"booked": [cc_transaction(m) for m in card_moves]},
            "summary": {"transactionCount": len(card_moves), "totals": _spend_totals(card_moves)},
        })
    return result


def _build_card_bundle(
    client: ItauClient, card_hash: str, month: Optional[int], year: Optional[int]
) -> Optional[dict]:
    """One card's metadata + booked moves + pending auths + spend summary."""
    card = next((c for c in client.credit_cards if c.get("hash") == card_hash), None)
    if card is None:
        return None
    moves = client.get_credit_card_moves(card_hash, month, year)
    booked = [m for m in moves if not is_payment(m)]
    auths = client.get_pending_authorizations(card_hash)
    return {
        "card": card_to_ob(card),
        "transactions": {
            "booked": [cc_transaction(m) for m in booked],
            "pending": [pending_authorization(a) for a in auths],
        },
        "summary": {"transactionCount": len(booked), "totals": _spend_totals(booked)},
    }


# ---------------------------------------------------------------------------
# New MCP tools: independent per-card views + account money-flow (in/out)
# ---------------------------------------------------------------------------


@mcp.tool()
def get_cards_moves(
    month: Optional[int] = None,
    year: Optional[int] = None,
) -> list[dict]:
    """
    Every credit card with its own transactions nested under it (one call).
    Each entry: {card, transactions.booked, summary}. Amounts negative (debit).

    Args:
        month: Month 1-12. Defaults to current month.
        year:  Full year e.g. 2026. Defaults to current year.
    """
    client = get_auto_client()
    try:
        return _build_cards_grouped(client, month, year)
    except ItauSessionExpired:
        return _build_cards_grouped(refresh_auto_client(), month, year)


@mcp.tool()
def get_card(
    card_hash: str,
    month: Optional[int] = None,
    year: Optional[int] = None,
) -> dict:
    """
    A single card's full bundle: metadata, booked transactions, pending
    authorizations, and a per-currency spend summary.

    Args:
        card_hash: resourceId from get_cards().
        month:     Month 1-12. Defaults to current month.
        year:      Full year e.g. 2026. Defaults to current year.
    """
    client = get_auto_client()
    try:
        bundle = _build_card_bundle(client, card_hash, month, year)
    except ItauSessionExpired:
        bundle = _build_card_bundle(refresh_auto_client(), card_hash, month, year)
    if bundle is None:
        raise ValueError(f"No card with hash {card_hash}")
    return bundle


@mcp.tool()
def get_account_flow(
    account_hash: str,
    month: Optional[int] = None,
    year: Optional[int] = None,
) -> list[dict]:
    """
    Bank account transactions with signed amounts (money in vs out).
    Debits (out) are negative with creditDebitIndicator 'DBIT'; credits (in)
    are positive with 'CRDT'. Berlin Group shape.

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
    return [account_transaction_signed(m, cur) for m in moves]


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


# ---------------------------------------------------------------------------
# Structured errors — every failure is JSON: {"error": {"code", "message"}}
# so callers can branch (retry / back off / reconnect) on the machine `code`
# instead of parsing a human string.
#
#   invalid_api_key  (401)  bad/missing x-api-key
#   not_configured   (503)  server has no Itaú credentials
#   auth_failed      (401)  bad document/password
#   account_blocked  (429)  Itaú blocked the account (too many attempts) — back off
#   session_expired  (401)  session died and couldn't recover
#   not_found        (404)
#   upstream_error   (502)  Itaú returned something unexpected — usually retryable
# ---------------------------------------------------------------------------


def api_error(code: str, message: str, http_status: int) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"code": code, "message": message})


@app.exception_handler(StarletteHTTPException)
async def _structured_http_error(request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        body = {"error": detail}
    else:
        body = {"error": {"code": "error", "message": detail}}
    return JSONResponse(status_code=exc.status_code, content=body, headers=getattr(exc, "headers", None))


@app.exception_handler(ItauAuthError)
async def _handle_auth_error(request: Request, exc: ItauAuthError):
    if getattr(exc, "code", None) == "10030":
        return JSONResponse(status_code=429, content={"error": {"code": "account_blocked", "message": str(exc)}})
    return JSONResponse(status_code=401, content={"error": {"code": "auth_failed", "message": str(exc)}})


@app.exception_handler(ItauSessionExpired)
async def _handle_session_expired(request: Request, exc: ItauSessionExpired):
    return JSONResponse(status_code=401, content={"error": {"code": "session_expired", "message": str(exc)}})


@app.exception_handler(ItauNoHistoric)
async def _handle_no_historic(request: Request, exc: ItauNoHistoric):
    # Defensive net: the historic fetcher already converts this to an empty
    # result at the client, so this only fires if a future path lets it escape.
    # It's "no data here", never a hard failure — surface an empty booked list.
    return JSONResponse(status_code=200, content={"transactions": {"booked": []}})


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
def rest_get_moves(
    month: Optional[int] = None,
    year: Optional[int] = None,
    card_hash: Optional[str] = None,
):
    """CC transactions (OB format). Defaults to all cards; pass card_hash to filter to one."""
    client = get_auto_client()
    if not client.credit_cards:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "No credit cards found."})
    try:
        moves = client.get_credit_card_moves(card_hash, month, year)
    except ItauSessionExpired:
        moves = refresh_auto_client().get_credit_card_moves(card_hash, month, year)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": "upstream_error", "message": str(e)})
    return {"transactions": {"booked": [cc_transaction(m) for m in moves if not is_payment(m)]}}


@app.get("/moves/{card_hash}", dependencies=[Depends(require_api_key)])
def rest_get_moves_for_card(card_hash: str, month: Optional[int] = None, year: Optional[int] = None):
    client = get_auto_client()
    try:
        moves = client.get_credit_card_moves(card_hash, month, year)
    except ItauSessionExpired:
        moves = refresh_auto_client().get_credit_card_moves(card_hash, month, year)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": "upstream_error", "message": str(e)})
    return {"transactions": {"booked": [cc_transaction(m) for m in moves if not is_payment(m)]}}


@app.get("/cards", dependencies=[Depends(require_api_key)])
def rest_get_cards():
    return {"paymentAccounts": [card_to_ob(c) for c in get_auto_client().credit_cards]}


# NOTE: declared before /cards/{card_hash} so the static path wins the match.
@app.get("/cards/moves", dependencies=[Depends(require_api_key)])
def rest_get_cards_moves(month: Optional[int] = None, year: Optional[int] = None):
    """Every card with its own moves nested under it (one upstream fetch, grouped)."""
    client = get_auto_client()
    if not client.credit_cards:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "No credit cards found."})
    try:
        cards = _build_cards_grouped(client, month, year)
    except ItauSessionExpired:
        cards = _build_cards_grouped(refresh_auto_client(), month, year)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": "upstream_error", "message": str(e)})
    return {"cards": cards}


@app.get("/cards/{card_hash}", dependencies=[Depends(require_api_key)])
def rest_get_card_bundle(card_hash: str, month: Optional[int] = None, year: Optional[int] = None):
    """One card's metadata + booked moves + pending auths + spend summary."""
    client = get_auto_client()
    try:
        bundle = _build_card_bundle(client, card_hash, month, year)
    except ItauSessionExpired:
        bundle = _build_card_bundle(refresh_auto_client(), card_hash, month, year)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": "upstream_error", "message": str(e)})
    if bundle is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"No card with hash {card_hash}"})
    return bundle


@app.get("/accounts/{account_hash}/moves", dependencies=[Depends(require_api_key)])
def rest_get_account_flow(account_hash: str, month: Optional[int] = None, year: Optional[int] = None):
    """Account transactions with signed amounts (in/out). Debit negative, credit positive."""
    client = get_auto_client()
    cur = _account_currency(client, account_hash)
    try:
        moves = client.get_account_moves(account_hash, month, year)
    except ItauSessionExpired:
        # Transient expiry — re-login once and retry. Accounts that genuinely
        # expose no historic view already return [] at the client (ItauNoHistoric),
        # so a *persistent* expiry here is a real session problem and now surfaces
        # as 401 instead of being masked as "no movements".
        client = refresh_auto_client()
        cur = _account_currency(client, account_hash)
        moves = client.get_account_moves(account_hash, month, year)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": "upstream_error", "message": str(e)})
    return {"transactions": {"booked": [account_transaction_signed(m, cur) for m in moves]}}


@app.get("/authorizations", dependencies=[Depends(require_api_key)])
def rest_get_authorizations():
    """Pending authorizations for all cards (OB format)."""
    client = get_auto_client()
    try:
        auths = client.get_pending_authorizations()
    except ItauSessionExpired:
        auths = refresh_auto_client().get_pending_authorizations()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": "upstream_error", "message": str(e)})
    return {"transactions": {"pending": [pending_authorization(a) for a in auths]}}


@app.get("/authorizations/{card_hash}", dependencies=[Depends(require_api_key)])
def rest_get_authorizations_for_card(card_hash: str):
    """Pending authorizations filtered to a specific card."""
    client = get_auto_client()
    try:
        auths = client.get_pending_authorizations(card_hash)
    except ItauSessionExpired:
        auths = refresh_auto_client().get_pending_authorizations(card_hash)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": "upstream_error", "message": str(e)})
    return {"transactions": {"pending": [pending_authorization(a) for a in auths]}}


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
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": "upstream_error", "message": str(e)})
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
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": "upstream_error", "message": str(e)})
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
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": "upstream_error", "message": str(e)})
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
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": "upstream_error", "message": str(e)})
    return {
        "transactions": {
            "booked": [cc_transaction(m) for m in moves if not is_payment(m)]
        }
    }


@v1.get("/cards/{card_id}/authorizations")
def v1_card_authorizations(card_id: str):
    """Berlin Group: pending authorizations for a card."""
    client = get_auto_client()
    try:
        auths = client.get_pending_authorizations(card_id)
    except ItauSessionExpired:
        auths = refresh_auto_client().get_pending_authorizations(card_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": "upstream_error", "message": str(e)})
    return {"transactions": {"pending": [pending_authorization(a) for a in auths]}}


app.include_router(v1)
