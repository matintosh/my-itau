"""
Itaú Uruguay API server.

Endpoints:
  POST /login                          — authenticate, returns session token
  GET  /accounts                       — list bank accounts
  GET  /credit-cards                   — list credit cards
  GET  /credit-cards/{hash}/moves      — credit-card transactions
  GET  /accounts/{hash}/moves          — bank-account transactions

All endpoints except /health require X-API-Key header matching the API_KEY env var.
Sessions are kept in-memory; on restart you must POST /login again.
"""

import logging
import os
import secrets
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from itau_client import ItauAuthError, ItauClient, ItauSessionExpired

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("main")

API_KEY = os.getenv("API_KEY", "")

app = FastAPI(
    title="Itaú UY API",
    description="Thin wrapper around itaulink.com.uy for personal use.",
    version="1.0.0",
)

# In-memory session store: session_token → ItauClient
_sessions: dict[str, ItauClient] = {}


# ------------------------------------------------------------------
# Auth helpers
# ------------------------------------------------------------------

def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    if not API_KEY:
        return  # dev mode — no key required
    if x_api_key != API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


def get_session(
    x_session_token: Optional[str] = Header(default=None),
    _: None = Depends(require_api_key),
) -> ItauClient:
    if not x_session_token or x_session_token not in _sessions:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-Session-Token. POST /login first.",
        )
    return _sessions[x_session_token]


# ------------------------------------------------------------------
# Request/response models
# ------------------------------------------------------------------

class LoginRequest(BaseModel):
    document_number: str
    password: str


class LoginResponse(BaseModel):
    session_token: str
    accounts: list[dict]
    credit_cards: list[dict]


class MovesQuery(BaseModel):
    month: Optional[int] = None
    year: Optional[int] = None


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "utc": datetime.utcnow().isoformat()}


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


@app.get("/accounts")
def list_accounts(client: ItauClient = Depends(get_session)):
    return {"accounts": client.accounts}


@app.get("/credit-cards")
def list_credit_cards(client: ItauClient = Depends(get_session)):
    return {"credit_cards": client.credit_cards}


@app.get("/credit-cards/{card_hash}/moves")
def credit_card_moves(
    card_hash: str,
    month: Optional[int] = None,
    year: Optional[int] = None,
    client: ItauClient = Depends(get_session),
):
    try:
        raw_moves = client.get_credit_card_moves(card_hash, month, year)
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
        "count": len(raw_moves),
        "moves": raw_moves,
        # Wrapped in the same envelope the mobile app's itau-parser.ts expects:
        # data.datos.datosMovimientos.movimientos
        "data": {
            "datos": {
                "datosMovimientos": {
                    "movimientos": raw_moves
                }
            }
        },
    }


@app.get("/accounts/{account_hash}/moves")
def account_moves(
    account_hash: str,
    month: Optional[int] = None,
    year: Optional[int] = None,
    client: ItauClient = Depends(get_session),
):
    try:
        raw_moves = client.get_account_moves(account_hash, month, year)
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
        "count": len(raw_moves),
        "moves": raw_moves,
    }


@app.delete("/logout")
def logout(
    x_session_token: Optional[str] = Header(default=None),
    _: None = Depends(require_api_key),
):
    if x_session_token and x_session_token in _sessions:
        del _sessions[x_session_token]
    return {"status": "logged out"}
