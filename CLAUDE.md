# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
my-itau doctor        # verify credentials + connectivity
```

Credentials are stored in `~/.my-itau/config.json` (document number) and system keyring (password). Env vars `ITAU_DOCUMENT_NUMBER` / `ITAU_PASSWORD` / `API_KEY` override both.

## Commands

```bash
my-itau config                        # save credentials interactively
my-itau moves [--month M --year Y]   # CC transactions (default card)
my-itau moves --all                   # all cards in one call
my-itau cards                         # list credit cards + hashes
my-itau cards-moves                   # every card with its own moves, grouped (1 fetch)
my-itau card <hash>                   # single-card bundle: moves + pending auths + summary
my-itau accounts                      # list bank accounts + hashes
my-itau account-moves <hash>          # bank account transactions (unsigned)
my-itau account-flow <hash>           # bank account transactions, signed in/out
my-itau authorizations                # pending CC authorizations, all cards
my-itau set-card                      # change default card
my-itau serve [--port 8787]          # start REST + MCP server
my-itau mcp                           # stdio MCP (for Claude Desktop)
my-itau doctor                        # connectivity + config health check
my-itau api-key add <alias>           # add named API key
my-itau request get /trx/             # raw authenticated request (escape hatch)
```

All data commands accept `--json` for machine-readable output.

Contributor scripts (repo root):
- `python probe.py` — endpoint reachability check, no credentials needed
- `python test_login.py` — full login + fetch smoke test, needs `.env` with `ITAU_DOCUMENT_NUMBER` / `ITAU_PASSWORD`

## Architecture

Three entry points, one `ItauClient`:

```
my_itau/
  client.py      — stateful HTTP scraper for itaulink.com.uy
  normalizers.py — pure functions: raw Itaú → Berlin Group / Open Banking format
  config.py      — credential + API key storage (keyring > file > env)
  cli.py         — Typer CLI (my-itau entrypoint)
  server.py      — FastAPI REST + FastMCP server (mounted at /mcp)
```

**`ItauClient` login flow** (reverse-engineered, must stay in order):
1. `GET /trx/` — prime session cookies
2. `POST /trx/doLogin` — form auth, expect redirect
3. `GET /trx/` — parse dashboard HTML for CSRF token + account list
4. `POST /trx/tarjetas/credito` — AJAX, returns card list with hashes
5. Move fetches use card/account `hash` as URL path segment

All AJAX calls require `X-CSRF-TOKEN` and `X-Requested-With: XMLHttpRequest` headers. CSRF token is parsed from `<meta name="_csrf">` in the dashboard HTML.

**Session management in `server.py`:**
- *Auto-session*: singleton `_auto_client` (lazy-login, thread-safe lock). All MCP tools and simple REST endpoints use this.
- *Manual session*: `POST /login` returns a session token; advanced endpoints use `X-Session-Token` header.
- Session expiry is detected via redirect to `expiredSession` URL; callers catch `ItauSessionExpired` and call `refresh_auto_client()`.

**API key guard**: if any key is configured, all non-`/health` endpoints require `X-Api-Key`. If no keys are configured, the server is open (useful for local dev).

**MCP server** (`FastMCP`) is mounted at `/mcp` on the same FastAPI app. Tools: `get_cards`, `get_moves`, `get_summary`, `get_accounts`, `get_account_moves`, `get_authorizations`, `get_cards_moves` (all cards grouped), `get_card` (single-card bundle), `get_account_flow` (account moves with signed in/out amounts). All use the auto-session.

**Per-card / account-flow surfaces** (added on top of the originals, nothing removed): `get_cards_moves` / `GET /cards/moves` fetch the account-wide CC endpoint **once** and group by `tarjeta.hash` — cheaper than one request per card (Itaú resets the connection on rapid multi-card requests). `GET /cards/moves` is declared **before** `GET /cards/{card_hash}` so the static path wins FastAPI's match. Account "in vs out" uses `normalizers.account_transaction_signed` (raw `tipo` `D`→negative/`DBIT`, `C`→positive/`CRDT`); the original `account_transaction` (always positive) is untouched.

**Normalizers** (`normalizers.py`) are pure functions with no I/O. They convert Itaú's internal field names (Spanish, Joda-Time date objects) to Berlin Group / NextGenPSD2 shape. CC amounts are negated (debit convention). `is_payment()` filters out `RECIBO DE PAGO` entries.

**Pending authorizations**: `ItauClient.get_pending_authorizations()` fetches account-wide pending CC auths (not per-card — Itaú returns them flat). `_fetch_cc_moves` auto-merges pending auths into the result when the requested month equals the current month. CLI/MCP/REST default to all-cards.

**Layer purity** (enforced by convention, not lint):
- `client.py` — pure HTTP, no FastAPI/CLI imports
- `normalizers.py` — pure functions, no I/O or network
- New REST goes in `server.py`; new CLI in `cli.py`

## REST API

Base URL: `http://localhost:8787`

| Endpoint | Auth | Notes |
|---|---|---|
| `GET /health` | none | liveness |
| `GET /cards` | API key | OB format |
| `GET /cards/moves` | API key | every card with its own moves nested + per-card summary (1 upstream fetch, grouped by `tarjeta.hash`) |
| `GET /cards/{card_hash}` | API key | single-card bundle: `card` + `transactions.booked` + `transactions.pending` + `summary` |
| `GET /moves[?card_hash=X]` | API key | all cards by default; `card_hash` query filters to one |
| `GET /moves/{card_hash}` | API key | path-style single-card filter (backwards compat) |
| `GET /accounts` | API key | OB format |
| `GET /accounts/{account_hash}/moves` | API key | account transactions, **signed** amounts (debit `-`/`DBIT`, credit `+`/`CRDT`) |
| `POST /login` | API key | returns session token |
| `GET /v1/accounts` | API key | Berlin Group prefix |
| `GET /v1/cards/{id}/transactions` | API key | Berlin Group |
| `GET /authorizations` | API key | pending CC auths (all cards) |
| `GET /mcp/sse` | — | MCP SSE transport |

## Key invariants

- `hash` is Itaú's opaque identifier for both cards and accounts — it changes between sessions; don't cache it long-term.
- `year_2d` in URL paths: Itaú uses 2-digit years (e.g. `/26` for 2026). Conversion: `year - 2000`. Used for **bank-account** historic endpoint (`/cuentas/1/{hash}/{m}/{yy}/consultaHistorica`).
- **CC moves** use a unified period endpoint: `/tarjetas/credito/{hash}/movimientos_actuales/{YYYY}{MM:02d}00`. The path's `{hash}` is just a session anchor — the endpoint returns **account-wide** data, so `_fetch_cc_moves` filters by `tarjeta.hash` to keep per-card semantics. The legacy `/movimientos_mes/{m}/{yy}` path is dead (404 as of 2026-06).
- `main.py` and `itau_client.py` in the repo root are backward-compat shims; the real code lives in `my_itau/`.

## If Itaú breaks the login flow

1. `python probe.py` — confirm which endpoints still exist
2. Capture a fresh browser login (devtools Network tab, filter `/trx/`)
3. Diff form fields + redirect URLs against `client.py:_do_login`
4. CSRF parsing lives in `client.py:_parse_dashboard` (`<meta name="_csrf">`)
5. `ERROR_CODES` in `client.py` maps 3 known login codes; unknown codes surface as `"Login failed (code XXXXX)"`
