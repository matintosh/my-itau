# itau-uy-api

Thin HTTP wrapper around [itaulink.com.uy](https://www.itaulink.com.uy) for personal use.  
Reverse-engineered from [GonzaloRizzo/Itau-API](https://github.com/GonzaloRizzo/Itau-API) + fresh traffic analysis.

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
# --host 0.0.0.0 is required so the iPhone can reach it over LAN
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
`X-Session-Token: <token>` → list of credit cards with hashes

### GET /credit-cards/{hash}/moves?month=4&year=2025
Returns raw moves **and** the same `data.datos.datosMovimientos.movimientos` envelope
the mobile app's `itau-parser.ts` expects.

### GET /accounts/{hash}/moves?month=4&year=2025
Raw bank-account moves.

### DELETE /logout

## Notes

- Sessions are in-memory — restart the server to clear them.
- The Itaú session cookie lasts ~20 min; after that, POST /login again.
- Password is sent in plaintext over HTTPS to Itaú's servers (same as the browser).
- For the mobile app integration: `POST /login`, grab the first credit card `hash`, then
  `GET /credit-cards/{hash}/moves` — feed the `data` field to `parseItauJson()`.
