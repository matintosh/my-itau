---
name: itau-uy-api
description: >-
  Interact with the local Itaú Uruguay scraper API to fetch credit card
  transactions. Use when importing Itaú transactions into the app, querying
  recent CC moves, checking API status, or integrating itaulink.com.uy data.
  Triggers: Itaú, itaulink, credit card import, CC moves,
  Itaú transactions, fetch bank data.
---

# Itaú UY API

CLI + REST API + MCP server for Itaú Uruguay banking data.
Source: clone of `https://github.com/matintosh/my-itau`

## Start the server (REST + HTTP MCP)

Only needed for LAN access or HTTP MCP. Claude Code uses stdio — no server required.

```bash
cd /path/to/itau-uy-api
.venv/bin/my-itau serve              # binds 0.0.0.0:8787
```

Credentials from system keyring (macOS Keychain). API keys managed via:
```bash
my-itau api-key add home             # generate + store key
my-itau api-key list                 # all keys + expiry status
my-itau api-key remove <alias>       # revoke
```

## Endpoints

Base: `http://localhost:8787`
All requests (except `/health`) require `X-Api-Key: <key>`.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness — no auth |
| GET | `/status` | Session + cache state |
| GET | `/cards` | Credit cards with hashes |
| GET | `/accounts` | Bank accounts with hashes |
| GET | `/moves` | CC moves, default card, current month |
| GET | `/moves?month=4&year=2026` | Specific month |
| GET | `/moves/{hash}` | Specific card |
| GET | `/v1/cards/{id}/transactions` | Berlin Group format |
| GET | `/v1/accounts/{id}/transactions` | Berlin Group format |

Swagger UI: `http://localhost:8787/docs`

## Typical response — `/moves` (Berlin Group format)

```json
{
  "transactions": {
    "booked": [
      {
        "transactionId": "12345678",
        "bookingDate": "2026-04-07",
        "transactionAmount": { "amount": "-162.00", "currency": "UYU" },
        "creditorName": "TELEPEAJE",
        "remittanceInformationUnstructured": ""
      }
    ]
  }
}
```

## Feeding moves into the mobile app

The `/moves` endpoint returns Berlin Group format. For the mobile app's
`parseItauJson()` parser, use `my-itau moves --json` which returns raw
Itaú payload, or adapt the parser to consume the OB format.

## Key field mapping (raw Itaú move → ParsedItauTransaction)

| Itaú field | App field | Notes |
|---|---|---|
| `nombreComercio` | `payee` | Trim whitespace |
| `fecha.{year,monthOfYear,dayOfMonth}` | `transactionDate` | YYYY-MM-DD |
| `importe` | `originalAmountCents` | `Math.round(abs * 100)` |
| `moneda` | `originalCurrency` | "Pesos" → `UYU`, else `USD` |
| `idCupon` | `clientId` | via `couponToUuid()` in parser |
| `nroCuota` / `cantCuotas` | `memo` | "Cuota 1/3" style |

## Session & cache behaviour

- First request auto-logs in (~3 s); session is reused for ~20 min.
- On session expiry the server re-authenticates automatically.
- Historic months (`?month=X&year=Y`) always hit Itaú live; no cache.

## Run integration test

```bash
cd /path/to/itau-uy-api
.venv/bin/python test_login.py   # requires credentials in keyring or .env
```
