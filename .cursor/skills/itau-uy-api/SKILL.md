---

## name: itau-uy-api

description: >-
  Interact with the local Itaú Uruguay scraper API to fetch credit card
  transactions. Use when importing Itaú transactions into the app, querying
  recent CC moves, checking API status, or integrating itaulink.com.uy data
  into project-f. Triggers: Itaú, itaulink, credit card import, CC moves,
  Itaú transactions, fetch bank data.

# Itaú UY API

Local Python/FastAPI service that scrapes itaulink.com.uy and exposes CC
transactions as JSON. Lives at `/Users/matintosh/dev/itau-uy-api`.

## Start the server

```bash
cd /Users/matintosh/dev/itau-uy-api
.venv/bin/uvicorn main:app --port 8787
```

Credentials and API key are read from `.env` (never committed).

## Endpoints

Base: `http://localhost:8787`  
All requests require `x-api-key: <API_KEY>` from `.env`.


| Method | Path                       | Description                                            |
| ------ | -------------------------- | ------------------------------------------------------ |
| GET    | `/health`                  | Liveness check — no auth required                      |
| GET    | `/status`                  | Session + cache state                                  |
| GET    | `/cards`                   | List credit cards with hashes                          |
| GET    | `/moves`                   | Current-month moves for first card — **cached 10 min** |
| GET    | `/moves?month=4&year=2026` | Specific month (not cached)                            |
| GET    | `/moves/{hash}`            | Moves for a specific card hash                         |


## Typical response — `/moves`

```json
{
  "card": { "brand": "VISA", "masked_number": "**** 4207", "holder": "MARTINEZ AGUERR, MATIAS NAH", "hash": "ecffb81c..." },
  "month": null,
  "year": null,
  "count": 56,
  "moves": [
    {
      "nombreComercio": "TELEPEAJE",
      "importe": 162,
      "moneda": "Pesos",
      "idCupon": "58317032",
      "nroCuota": 1,
      "cantCuotas": 1,
      "fecha": { "year": 2026, "monthOfYear": 4, "dayOfMonth": 7 },
      "tarjeta": { "sello": "VISA", "nroTitularTarjetaWithMask": "**** 4094" }
    }
  ]
}
```

## Feeding moves into the mobile app

The move objects are in the shape `parseItauJson()` in
`apps/mobile/src/lib/itau-parser.ts` expects. Wrap any array of moves like:

```ts
const raw = { data: { datos: { datosMovimientos: { movimientos: moves } } } }
const parsed = parseItauJson(raw)
```

Then pass `parsed` to `upsertTransactions()` from `transaction.queries.ts`.

## Key field mapping (move → ParsedItauTransaction)


| Itaú field                            | App field             | Notes                          |
| ------------------------------------- | --------------------- | ------------------------------ |
| `nombreComercio`                      | `payee`               | Trim whitespace                |
| `fecha.{year,monthOfYear,dayOfMonth}` | `transactionDate`     | YYYY-MM-DD                     |
| `importe`                             | `originalAmountCents` | `Math.round(abs * 100)`        |
| `moneda`                              | `originalCurrency`    | "Pesos" → `UYU`, else `USD`    |
| `idCupon`                             | `clientId`            | via `couponToUuid()` in parser |
| `nroCuota` / `cantCuotas`             | `memo`                | "Cuota 1/3" style              |


## Session & cache behaviour

- First request auto-logs in (~3 s); session is reused for ~20 min.
- `/moves` (current month, no query params) is cached 10 min in-process.
- On session expiry the client re-logs automatically — callers never see it.
- Historic months (`?month=X&year=Y`) always hit Itaú live; no cache.

## Run integration test

```bash
cd /Users/matintosh/dev/itau-uy-api
.venv/bin/python test_login.py   # requires .env with real credentials
```

