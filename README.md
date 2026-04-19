# itau-uy-api

Thin HTTP wrapper around [itaulink.com.uy](https://www.itaulink.com.uy) for personal use.
Built via traffic analysis of the Itaú Uruguay web app.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env with your credentials + a random API_KEY

# 1. Probe — verify endpoints are still live (no credentials needed)
python probe.py

# 2. Full integration test (real credentials)
python test_login.py

# 3. Run the API server
# --host 0.0.0.0 required to reach it over LAN
uvicorn main:app --host 0.0.0.0 --port 8787
```

## API

All requests require `X-Api-Key: <your API_KEY>` header.

### POST /login
```json
{ "document_number": "12345678", "password": "mypassword" }
```
Returns `session_token`, `accounts`, and `credit_cards`.

### GET /credit-cards
`X-Session-Token: <token>` → list of credit cards with hashes.

### GET /credit-cards/{hash}/moves?month=4&year=2025
Returns raw moves payload.

### GET /accounts/{hash}/moves?month=4&year=2025
Returns raw bank-account moves.

### DELETE /logout

## Notes

- Sessions are in-memory — restart to clear.
- Itaú session cookie lasts ~20 min; re-authenticate via `POST /login` after expiry.
- Password sent in plaintext over HTTPS to Itaú's servers (same as the browser).
