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
my-itau cards                         # list credit cards + hashes
my-itau accounts                      # list bank accounts + hashes
my-itau account-moves <hash>          # bank account transactions
my-itau set-card                      # change default card
my-itau serve [--port 8787]          # start REST + MCP server
my-itau mcp                           # stdio MCP (for Claude Desktop)
my-itau doctor                        # connectivity + config health check
my-itau api-key add <alias>           # add named API key
```

All data commands accept `--json` for machine-readable output.

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

**MCP server** (`FastMCP`) is mounted at `/mcp` on the same FastAPI app. Tools: `get_cards`, `get_moves`, `get_summary`, `get_accounts`, `get_account_moves`. All use the auto-session.

**Normalizers** (`normalizers.py`) are pure functions with no I/O. They convert Itaú's internal field names (Spanish, Joda-Time date objects) to Berlin Group / NextGenPSD2 shape. CC amounts are negated (debit convention). `is_payment()` filters out `RECIBO DE PAGO` entries.

## REST API

Base URL: `http://localhost:8787`

| Endpoint | Auth | Notes |
|---|---|---|
| `GET /health` | none | liveness |
| `GET /cards` | API key | OB format |
| `GET /moves[/{card_hash}]` | API key | default card or specific |
| `GET /accounts` | API key | OB format |
| `POST /login` | API key | returns session token |
| `GET /v1/accounts` | API key | Berlin Group prefix |
| `GET /v1/cards/{id}/transactions` | API key | Berlin Group |
| `GET /mcp/sse` | — | MCP SSE transport |

## Key invariants

- `hash` is Itaú's opaque identifier for both cards and accounts — it changes between sessions; don't cache it long-term.
- `year_2d` in URL paths: Itaú uses 2-digit years (e.g. `/26` for 2026). Conversion: `year - 2000`.
- Current-month vs historic move fetches hit different endpoints — the client branches on `month == today.month and year == today.year`.
- `main.py` and `itau_client.py` in the repo root are backward-compat shims; the real code lives in `my_itau/`.
