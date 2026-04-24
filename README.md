# my-itau

Unofficial Itaú Uruguay banking CLI, REST API, and MCP server.  
Reverse-engineered from live traffic against [itaulink.com.uy](https://www.itaulink.com.uy).

> **Not affiliated with Banco Itaú.** This project scrapes a private web portal.
> It may break if Itaú changes their frontend. Use at your own risk.

---

## Contents

- [Requirements](#requirements)
- [Install](#install)
- [Configure](#configure)
- [CLI](#cli)
- [REST API](#rest-api)
- [MCP — AI agent integration](#mcp--ai-agent-integration)
- [Security implications](#security-implications)
- [Architecture](#architecture)
- [Contributing](#contributing)

---

## Requirements

- Python 3.9+
- An active [itaulink.com.uy](https://www.itaulink.com.uy) account

---

## Install

```bash
git clone https://github.com/matintosh/my-itau
cd itau-uy-api
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Verify:

```bash
my-itau doctor
```

---

## Configure

### Interactive (recommended)

```bash
my-itau config
# prompts for document number and password
```

Passwords go to the **system keyring** (macOS Keychain, Linux Secret Service) when available.  
The document number and non-sensitive settings land in `~/.my-itau/config.json` (chmod 600).  
On headless Linux without a Secret Service daemon, everything falls back to the config file.

### API keys

Multiple named API keys are supported, each with an optional expiry:

```bash
my-itau api-key add home                        # auto-generate key
my-itau api-key add ci --expires 2026-12-31     # with expiry
my-itau api-key add mobile --key <your-key>     # bring your own

my-itau api-key list                            # show all keys + status
my-itau api-key remove ci                       # revoke by alias
```

Key values are shown once at creation and stored in the system keyring (or config file fallback). Expired keys are rejected automatically.

### Environment variables

Env vars take priority over keyring and config file — useful for CI or Docker:

```bash
export ITAU_DOCUMENT_NUMBER=12345678
export ITAU_PASSWORD=your-password
export API_KEY=random-secret          # single key override (always valid)
```

### Verify setup

```bash
my-itau doctor
```

Output shows credential status, keyring availability, and Itaú reachability:

```
  ✓ document_number set
  ✓ password set
  ✓ default_card ecffb81c…
  ✓ keyring system keyring
  ✓ api_keys 2 active (home, mobile)
  ✓ itaulink.com.uy reachable (HTTP 302)

Ready.
```

---

## CLI

### Credit cards

```bash
my-itau cards                        # list all cards with hashes
my-itau set-card                     # change default card (interactive)
```

### Transactions

```bash
my-itau moves                        # default card, current month
my-itau moves --month 3 --year 2026  # specific month
my-itau moves --card <hash>          # specific card
my-itau moves --all                  # all cards
my-itau moves --json                 # machine-readable output
```

Amounts follow **debit convention** (negative = you spent money).  
Payment entries (card bill payments) are excluded automatically.

### Bank accounts

```bash
my-itau accounts                     # list accounts with hashes
my-itau account-moves                # pick account interactively
my-itau account-moves <hash>         # current month
my-itau account-moves <hash> --month 3 --year 2026
my-itau account-moves <hash> --json
```

### Utilities

```bash
my-itau doctor                       # health check
my-itau doctor --json                # machine-readable health check

my-itau api-key add home             # add API key
my-itau api-key list                 # list all keys
my-itau api-key remove home          # revoke key

my-itau request get /trx/            # raw authenticated GET (escape hatch)

my-itau reset                        # delete ~/.my-itau/ and keyring entries
my-itau reset --yes                  # skip confirmation
```

---

## REST API

Start the server:

```bash
my-itau serve                        # binds 0.0.0.0:8787
my-itau serve --port 9000
my-itau serve --host 127.0.0.1       # local-only
```

```
REST → http://localhost:8787/
Docs → http://localhost:8787/docs   (Swagger UI)
MCP  → http://localhost:8787/mcp/sse
```

When any API key is configured, all endpoints require the header `X-Api-Key: <key>`. If no keys exist, the server is open.

### Auto-session endpoints

The server logs in automatically using stored credentials and re-authenticates when the Itaú session expires (~20 min).

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Server liveness (no auth required) |
| `GET` | `/status` | Session + cache status |
| `GET` | `/moves` | CC transactions, default card, current month |
| `GET` | `/moves?month=3&year=2026` | Specific month |
| `GET` | `/moves/{card_hash}` | Specific card |
| `GET` | `/cards` | List credit cards |
| `GET` | `/accounts` | List bank accounts |

### Open Banking (Berlin Group) endpoints

```
GET /v1/accounts
GET /v1/accounts/{account_id}/transactions
GET /v1/accounts/{account_id}/transactions?month=3&year=2026
GET /v1/cards
GET /v1/cards/{card_id}/transactions
```

Response shapes follow [NextGenPSD2 / Berlin Group](https://www.berlin-group.org/nextgenpsd2-downloads).  
Amounts are strings with two decimal places. Currency codes are ISO 4217 (`UYU`, `USD`, `EUR`, `BRL`).

### Manual session endpoints

For multi-user or scripted use. POST `/login` returns a `session_token`; pass it as `X-Session-Token` on subsequent calls.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/login` | Authenticate, returns session token + account list |
| `GET` | `/credit-cards` | Cards for manual session |
| `GET` | `/credit-cards/{hash}/moves` | CC moves for manual session |
| `GET` | `/account-moves/{hash}` | Account moves for manual session |
| `DELETE` | `/logout` | Invalidate session token |

`POST /login` body:
```json
{
  "document_number": "12345678",
  "password": "your-password"
}
```

Sessions are **in-memory** — a server restart clears all tokens.

---

## MCP — AI agent integration

my-itau exposes an [MCP](https://modelcontextprotocol.io) server so AI agents can query your banking data directly. Two transports: **stdio** (no server needed, Claude Code spawns the process) and **HTTP/SSE** (requires `my-itau serve` running).

### Claude Code (stdio — recommended)

```bash
claude mcp add my-itau /path/to/.venv/bin/my-itau -- mcp
claude mcp list   # verify: ✓ Connected
```

No server needed. Claude Code spawns `my-itau mcp` on demand. Credentials come from keyring automatically.

### Claude Desktop (stdio)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "my-itau": {
      "command": "/path/to/.venv/bin/my-itau",
      "args": ["mcp"]
    }
  }
}
```

Restart Claude Desktop.

### Cursor / Windsurf / other editors (stdio)

Add to your editor's MCP config file (check editor docs for exact path):

```json
{
  "mcpServers": {
    "my-itau": {
      "command": "/path/to/.venv/bin/my-itau",
      "args": ["mcp"]
    }
  }
}
```

### Any MCP client (HTTP/SSE)

Run `my-itau serve` first, then point any SSE-capable MCP client at:

```
http://localhost:8787/mcp/sse
```

Or in JSON config format:

```json
{
  "mcpServers": {
    "my-itau": {
      "url": "http://localhost:8787/mcp/sse"
    }
  }
}
```

### Claude Code skill (optional)

If you want Claude Code to understand this project's context and CLI across sessions, install the companion skill:

```bash
# Already included in this repo — install globally:
mkdir -p ~/.claude/skills/itau-uy-api
cp .claude/skills/itau-uy-api/SKILL.md ~/.claude/skills/itau-uy-api/SKILL.md
```

Claude Code will then automatically load context about the CLI, endpoints, and field mappings when you ask about Itaú transactions.

### Example prompts once connected

- *"How much did I spend this month?"*
- *"List my transactions at Tienda Inglesa in March."*
- *"What's my current USD account balance?"*
- *"Show me all installment purchases this month."*

### Available MCP tools

| Tool | Description |
|------|-------------|
| `get_cards` | List credit cards. `resourceId` = identifier for other calls. |
| `get_moves` | CC transactions (optional `month`, `year`, `card_hash`) |
| `get_summary` | Spending totals per currency for a period |
| `get_accounts` | List bank accounts |
| `get_account_moves` | Account transactions (`account_hash`, optional `month`/`year`) |

---

## Security implications

Read this before running the server on a shared or internet-facing machine.

### Credential storage

| Storage | What's stored there |
|---------|---------------------|
| System keyring | `password`, API key values (when keyring is available) |
| `~/.my-itau/config.json` (chmod 600) | `document_number`, `default_card`, API key metadata (alias, expiry, created); key values if keyring unavailable |
| Environment variables | Override everything; never persisted by this tool |

- **Keyring** — macOS Keychain or Linux Secret Service (GNOME Keyring, KWallet). Encrypted at rest, unlocked by your login session.
- **File fallback** — `chmod 600` prevents other users from reading it, but the file is plaintext. Use full-disk encryption (FileVault / LUKS) for defense in depth.
- **Never commit** `.env` files containing your credentials.
- **API key values** are shown once at creation and never again — save them somewhere safe.

### Network

- Your password is sent to `itaulink.com.uy` over HTTPS — the same as logging in through a browser.
- The local REST server does **not** use HTTPS. Do not expose it on a public network without a reverse proxy (nginx, Caddy) that terminates TLS.
- Add at least one API key (`my-itau api-key add home`) even on a LAN — it stops other devices from reading your transactions.
- Use per-client aliases (e.g. `home`, `mobile`, `ci`) so you can revoke individual access without rotating all keys.

### Sessions

- Itaú sessions last ~20 minutes. The server re-authenticates automatically.
- Manual session tokens (`POST /login`) are random 32-byte URL-safe strings. They live in memory and vanish on restart.
- No session tokens or credentials are logged.

### What this tool cannot do

- It is **read-only** by design — no transfers, no payments, no account changes.
- It only calls endpoints that the official Itaú web portal calls.

---

## Architecture

```
my_itau/
├── client.py       HTTP scraper — login flow, CSRF, AJAX calls
├── normalizers.py  Pure functions: raw Itaú JSON → Berlin Group shapes
├── config.py       Credential storage (keyring + file fallback)
├── server.py       FastAPI REST app + FastMCP server (one process)
└── cli.py          Typer CLI — thin wrappers around client + server
```

### Login flow (reverse-engineered)

```
GET  /trx/                              prime session cookies
POST /trx/doLogin                       form login → redirect on success
GET  /trx/                              parse accounts + CSRF token from HTML
POST /trx/tarjetas/credito              AJAX: credit card list with hashes
POST /trx/tarjetas/credito/{hash}/...   AJAX: CC moves (current or historic)
POST /trx/cuentas/1/{hash}/...          AJAX: account moves (current or historic)
```

All AJAX calls require `X-CSRF-TOKEN` and `X-Requested-With: XMLHttpRequest`.

### Response normalization

Raw Itaú fields → Berlin Group equivalents:

| Itaú field | Normalized field |
|-----------|-----------------|
| `idCupon` | `transactionId` (CC) |
| `idMovimiento` / `nroMovimiento` | `transactionId` (account) |
| `fecha` | `bookingDate` (ISO 8601) |
| `importe` | `transactionAmount.amount` (negated for CC debits) |
| `moneda` | `transactionAmount.currency` (ISO 4217) |
| `nombreComercio` | `creditorName` |
| `cantCuotas` / `nroCuota` | `cardTransaction.installments` |

### Dependency overview

| Package | Role |
|---------|------|
| `httpx` | HTTP client with cookie jar |
| `fastapi` | REST framework |
| `fastmcp` | MCP server (SSE + stdio) |
| `typer` + `rich` | CLI + terminal output |
| `keyring` | System keyring integration |

---

## Contributing

### Setup

```bash
git clone https://github.com/matintosh/my-itau
cd itau-uy-api
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

### Explore without real credentials

```bash
python probe.py          # tests endpoints are reachable without logging in
```

### Integration test

Requires real credentials in `.env`:

```bash
cp .env.example .env     # fill in ITAU_DOCUMENT_NUMBER and ITAU_PASSWORD
python test_login.py
```

### Key areas for contribution

- **Endpoint breakage** — Itaú changes their frontend periodically. The login flow is in `client.py:_do_login` and `_parse_dashboard`. CSRF parsing is in `_parse_dashboard`.
- **New data** — savings accounts, fixed-term deposits, and investment data exist in the portal but are not yet scraped.
- **Error codes** — `ERROR_CODES` in `client.py` covers three known codes. Others are `"Login failed (code XXXXX)"` — PRs mapping new codes are welcome.
- **Normalizer coverage** — `normalizers.py` maps common fields; edge cases in raw payloads (unusual date formats, missing fields) surface as empty strings rather than errors.

### Guidelines

- Keep `client.py` a pure HTTP client — no FastAPI, no CLI imports.
- Keep `normalizers.py` pure functions — no I/O, no network calls.
- New REST endpoints go in `server.py`; new CLI commands in `cli.py`.
- Don't commit real credentials or raw API responses.

### If Itaú breaks the login flow

1. Run `python probe.py` to confirm which endpoints still exist.
2. Capture a fresh login in browser devtools (Network tab, filter `/trx/`).
3. Compare the new form fields / redirect URLs against `client.py:_do_login`.
4. Update `_do_login` and `_parse_dashboard` accordingly.


## Inspiration
This tool was inspired by https://github.com/GonzaloRizzo/Itau-API

