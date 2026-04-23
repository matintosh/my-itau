"""
Full integration test — requires real credentials in .env.

Usage:
  cp .env.example .env
  # fill in ITAU_DOCUMENT_NUMBER and ITAU_PASSWORD
  python test_login.py

Prints accounts, credit cards, and the first few moves of the first card.
"""

import os
import json
import logging
from dotenv import load_dotenv
from itau_client import ItauClient, ItauAuthError, ItauSessionExpired

load_dotenv()

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")

doc = os.environ["ITAU_DOCUMENT_NUMBER"]
pwd = os.environ["ITAU_PASSWORD"]

client = ItauClient()
print("\n── Login ────────────────────────────────────────────────────")
try:
    client.login(doc, pwd)
    print("  ✓ Logged in successfully")
except ItauAuthError as e:
    print(f"  ✗ Auth error: {e}")
    raise SystemExit(1)

print(f"\n── Accounts ({len(client.accounts)}) ──────────────────────────────────────")
for a in client.accounts:
    print(f"  {a['type']} {a['currency']} balance={a['balance']}  hash={a['hash']}")

print(f"\n── Credit cards ({len(client.credit_cards)}) ──────────────────────────────")
for c in client.credit_cards:
    print(f"  {c['brand']} ...{c['last_digits']}  hash={c['hash']}")

if client.credit_cards:
    card = client.credit_cards[0]
    print(f"\n── Moves for card {card['hash']} (current month) ──────────────────")
    try:
        moves = client.get_credit_card_moves(card["hash"])
        print(f"  {len(moves)} moves found")
        for m in moves[:5]:
            print(" ", json.dumps(m, ensure_ascii=False, default=str))
    except ItauSessionExpired:
        print("  ✗ Session expired")
    except Exception as e:
        print(f"  ✗ Error: {e}")
