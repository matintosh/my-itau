---
name: itau-uy-api
description: >-
  Interact with the local Itaú Uruguay scraper API to fetch credit card
  transactions. Use when importing Itaú transactions into the app, querying
  recent CC moves, checking API status, or integrating itaulink.com.uy data
  into project-f. Triggers: Itaú, itaulink, credit card import, CC moves,
  Itaú transactions, fetch bank data.
---

# Itaú UY API

Local Python/FastAPI + MCP service that scrapes itaulink.com.uy and exposes
CC and account transactions as JSON. Lives at `/Users/matintosh/dev/itau-uy-api`.

## Start the server

```bash
cd /Users/matintosh/dev/itau-uy-api
.venv/bin/my-itau serve              # binds 0.0.0.0:8787
.venv/bin/my-itau serve --port 9000  # custom port
```

Credentials are read from the system keyring (macOS Keychain) or `.env`.
API key is required — set via `my-itau config` or `API_KEY` env var.

## Endpoints

Base: `http://localhost:8787`
All requests (except `/health`) require `X-Api-Key: <API_KEY>`.

### Auto-session (uses stored credentials)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness — no auth |
| GET | `/status` | Session + cache state |
| GET | `/cards` | Credit cards with hashes |
| GET | `/accounts` | Bank accounts with hashes |
| GET | `/moves` | CC moves, default card, current month |
| GET | `/moves?month=4&year=2026` | Specific month |
| GET | `/moves/{card_hash}` | Specific card |

### Open Banking (Berlin Group) — `/v1`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/v1/cards` | Cards (OB format) |
| GET | `/v1/cards/{id}/transactions` | CC transactions |
| GET | `/v1/accounts` | Accounts (OB format) |
| GET | `/v1/accounts/{id}/transactions` | Account transactions |

All `/v1` responses use Berlin Group shapes: ISO 8601 dates, ISO 4217 currency
codes (`UYU`, `USD`), amounts as strings with two decimal places, negative for debits.

### Manual session (multi-user / scripted)

```
POST   /login                         → { session_token, accounts, credit_cards }
GET    /credit-cards/{hash}/moves     X-Session-Token required
GET    /account-moves/{hash}          X-Session-Token required
DELETE /logout
```

## Typical response — `/moves`

```json
{
  "transactions": {
    "booked": [
      {
        "transactionId": "58317032",
        "bookingDate": "2026-04-07",
        "transactionAmount": { "amount": "-162.00", "currency": "UYU" },
        "creditorName": "TELEPEAJE",
        "remittanceInformationUnstructured": "",
        "proprietaryBankTransactionCode": "compra"
      }
    ]
  }
}
```

Payment entries (card bill payments) are excluded automatically.
Installment info lives in `cardTransaction.installments.{current,total}`.

## Feeding moves into the mobile app

The `/moves` response is normalized (Berlin Group format). The mobile app's
`parseItauJson()` in `apps/mobile/src/lib/itau-parser.ts` expects the raw
Itaú shape — use the raw `moves` array from `my-itau moves --json` instead,
or adapt the parser to consume the OB format.

Key field mapping (raw Itaú move → ParsedItauTransaction):

| Itaú field | App field | Notes |
|---|---|---|
| `nombreComercio` | `payee` | Trim whitespace |
| `fecha.{year,monthOfYear,dayOfMonth}` | `transactionDate` | YYYY-MM-DD |
| `importe` | `originalAmountCents` | `Math.round(abs * 100)` |
| `moneda` | `originalCurrency` | "Pesos" → `UYU`, else `USD` |
| `idCupon` | `clientId` | via `couponToUuid()` in parser |
| `nroCuota` / `cantCuotas` | `memo` | "Cuota 1/3" style |

## CLI (quick reference)

```bash
.venv/bin/my-itau moves                        # default card, current month
.venv/bin/my-itau moves --month 3 --year 2026
.venv/bin/my-itau moves --json                 # raw JSON to stdout
.venv/bin/my-itau cards
.venv/bin/my-itau accounts
.venv/bin/my-itau account-moves <hash>
.venv/bin/my-itau doctor                       # health check
```

## Session & cache behaviour

- First request auto-logs in (~3 s); session reused for ~20 min.
- On session expiry the server re-authenticates automatically.
- Historic months (`?month=X&year=Y`) always hit Itaú live; no cache.

## Run integration test

```bash
cd /Users/matintosh/dev/itau-uy-api
.venv/bin/python test_login.py   # requires credentials in keyring or .env
```
