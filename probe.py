"""
Probe script — tests the Itaú Uruguay endpoints WITHOUT real credentials.

Run:
  python probe.py

What it checks:
  1. Can we reach itaulink.com.uy at all?
  2. Does GET /trx/ return 200?
  3. Does POST /trx/doLogin return a redirect (3xx)?  ← proves the endpoint still exists
  4. Does the redirect on bad creds go to a known error URL?
  5. Does /trx/tarjetas/... return 302 to expiredSession (not 404)?  ← proves endpoint exists
"""

import sys
import httpx

BASE = "https://www.itaulink.com.uy/trx"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-UY,es;q=0.9",
}

ok = True


def check(label: str, condition: bool, detail: str = "") -> None:
    global ok
    symbol = "✓" if condition else "✗"
    print(f"  {symbol}  {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        ok = False


with httpx.Client(
    base_url=BASE,
    follow_redirects=False,
    timeout=15,
    headers=HEADERS,
) as http:

    print("\n── 1. Reachability ─────────────────────────────────────────")
    try:
        r = http.get("/")
        # /trx/ redirects to login page when unauthenticated — that's expected
        check(
            "GET /trx/ reachable (2xx or redirect)",
            r.status_code < 500,
            f"HTTP {r.status_code}",
        )
    except httpx.ConnectError as e:
        check("GET /trx/ reachable", False, str(e))

    print("\n── 2. Login endpoint ───────────────────────────────────────")
    try:
        r = http.post(
            "/doLogin",
            data={
                "tipo_documento": "1",
                "tipo_usuario": "R",
                "nro_documento": "00000000",   # bogus
                "pass": "wrongpass",
            },
        )
        is_redirect = r.status_code in (301, 302, 303, 307, 308)
        check("/trx/doLogin returns redirect", is_redirect, f"HTTP {r.status_code}")
        loc = r.headers.get("location", "")
        check(
            "Redirect contains error code (bad creds rejected, not 404)",
            "message_code" in loc or "error" in loc.lower() or "/trx/" in loc,
            f"Location: {loc}",
        )
    except Exception as e:
        check("/trx/doLogin reachable", False, str(e))

    print("\n── 3. Credit-card endpoint ─────────────────────────────────")
    DUMMY_HASH = "fakehash123"
    try:
        r = http.post(
            f"/tarjetas/1/{DUMMY_HASH}/mesActual",
            headers={"Accept": "application/json"},
        )
        check(
            "/trx/tarjetas/... endpoint exists (not 404)",
            r.status_code != 404,
            f"HTTP {r.status_code}",
        )
        loc = r.headers.get("location", "")
        check(
            "Redirects to expiredSession or similar (auth required, not 404)",
            "expiredSession" in loc or "solicitar_ingreso" in loc or r.status_code in (302, 401, 403),
            f"Location: {loc or '(none)'}",
        )
    except Exception as e:
        check("/trx/tarjetas/... reachable", False, str(e))

    print("\n── 4. Account endpoint ─────────────────────────────────────")
    try:
        r = http.post(
            f"/cuentas/1/{DUMMY_HASH}/mesActual",
            headers={"Accept": "application/json"},
        )
        check(
            "/trx/cuentas/... endpoint exists (not 404)",
            r.status_code != 404,
            f"HTTP {r.status_code}",
        )
    except Exception as e:
        check("/trx/cuentas/... reachable", False, str(e))

print()
if ok:
    print("All checks passed — the old endpoints appear to still be active.")
else:
    print("Some checks failed — inspect the details above.")
print()
sys.exit(0 if ok else 1)
