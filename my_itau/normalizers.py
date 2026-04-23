"""
Open Banking normalizers — Berlin Group / NextGenPSD2.

Converts raw Itaú Uruguay data into OB-compatible shapes.
Pure functions: no I/O, no side effects.

Spec reference: https://www.berlin-group.org/nextgenpsd2-downloads
"""

# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------

_CURRENCY_MAP = {
    "pesos": "UYU",
    "dolares": "USD",
    "dólares": "USD",
    "euros": "EUR",
    "reales": "BRL",
    # Account-level currency codes Itaú uses (not ISO 4217)
    "us.d": "USD",
    "u$s": "USD",
    "u$": "USD",
}


def currency_code(itau_currency: str) -> str:
    """'Pesos' → 'UYU', 'Dolares' → 'USD'. Unknown passthrough."""
    return _CURRENCY_MAP.get((itau_currency or "").lower().strip(), itau_currency or "")


# ---------------------------------------------------------------------------
# Date
# ---------------------------------------------------------------------------

def fmt_date(fecha) -> str:
    """Joda-Time object or string → ISO 8601 'YYYY-MM-DD'."""
    if isinstance(fecha, dict):
        y = fecha.get("year", "")
        m = fecha.get("monthOfYear", 0)
        d = fecha.get("dayOfMonth", 0)
        try:
            return f"{y}-{int(m):02d}-{int(d):02d}"
        except (TypeError, ValueError):
            return ""
    return str(fecha) if fecha else ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _amt(value, *, negate: bool = False) -> str:
    """Number → signed decimal string. negate=True for debits."""
    v = float(value or 0)
    if negate:
        v = -abs(v)
    return f"{v:.2f}"


def is_payment(m: dict) -> bool:
    """True for RECIBO DE PAGO entries (credit card payments)."""
    return (m.get("nombreComercio") or "").upper() == "RECIBO DE PAGO"


# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------

_CASH_ACCOUNT_TYPE = {
    "CAJA_DE_AHORRO": "SVGS",
    "CUENTA_CORRIENTE": "CACC",
    "CUENTA_RECAUDADORA": "CACC",
    "CUENTA_DE_AHORRO_JUNIOR": "SVGS",
    "CUENTA_DE_ALIMENTACION": "CACC",
}

_CARD_STATUS = {
    "desbloqueado": "enabled",
    "bloqueado": "blocked",
}


# ---------------------------------------------------------------------------
# CC transaction
# ---------------------------------------------------------------------------

def cc_transaction(m: dict) -> dict:
    """Raw Itaú CC move → Berlin Group transaction object.

    Amount is negative (debit convention). Installments included when > 1.
    """
    cur = currency_code(m.get("moneda") or "")
    cant = int(m.get("cantCuotas") or 1)

    result: dict = {
        "transactionId": str(m.get("idCupon") or ""),
        "bookingDate": fmt_date(m.get("fecha")),
        "transactionAmount": {
            "amount": _amt(m.get("importe"), negate=True),
            "currency": cur,
        },
        "creditorName": m.get("nombreComercio") or "",
        "remittanceInformationUnstructured": m.get("descripcionAdicional") or "",
        "proprietaryBankTransactionCode": m.get("tipo") or "",
    }

    if cant > 1:
        result["cardTransaction"] = {
            "installments": {"current": m.get("nroCuota"), "total": cant}
        }

    return result


# ---------------------------------------------------------------------------
# Account transaction
# ---------------------------------------------------------------------------

def account_transaction(m: dict, fallback_currency: str = "") -> dict:
    """Raw Itaú account move → Berlin Group transaction object.

    fallback_currency: ISO 4217 code to use when the move has no moneda field
    (account moves don't carry currency — pass it from the account object).
    """
    fecha = m.get("fecha") or m.get("fechaMovimiento") or m.get("fechaContable")
    cur = currency_code(m.get("moneda") or "") or currency_code(fallback_currency)
    amount = m.get("importe") or m.get("monto") or 0

    result: dict = {
        "transactionId": str(m.get("idMovimiento") or m.get("nroMovimiento") or ""),
        "bookingDate": fmt_date(fecha) if isinstance(fecha, dict) else (str(fecha) if fecha else ""),
        "transactionAmount": {
            "amount": f"{float(amount):.2f}",
            "currency": cur,
        },
        "remittanceInformationUnstructured": (
            m.get("descripcion") or m.get("nombreComercio") or m.get("concepto") or ""
        ),
    }

    if m.get("saldo") is not None:
        result["balanceAfterTransaction"] = {
            "balanceAmount": {"amount": f"{float(m['saldo']):.2f}", "currency": cur},
            "balanceType": "interimBooked",
        }

    return result


# ---------------------------------------------------------------------------
# Card (payment instrument)
# ---------------------------------------------------------------------------

def card_to_ob(c: dict) -> dict:
    """Normalised card dict → Berlin Group payment instrument."""
    cur = currency_code(c.get("currency") or "")
    status_raw = (c.get("status") or "").lower()

    result: dict = {
        "resourceId": c.get("hash") or "",
        "maskedPan": c.get("masked_number") or "",
        "name": c.get("brand") or "",
        "ownerName": c.get("holder") or "",
        "currency": cur,
        "status": _CARD_STATUS.get(status_raw, status_raw),
    }

    if c.get("limit") is not None:
        result["creditLimit"] = {"amount": f"{float(c['limit']):.2f}", "currency": cur}

    if c.get("expiry"):
        result["expiryDate"] = c["expiry"]

    return result


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

def account_to_ob(a: dict) -> dict:
    """Normalised account dict → Berlin Group account."""
    cur = currency_code(a.get("currency") or "")
    account_type = a.get("type") or ""

    result: dict = {
        "resourceId": a.get("hash") or "",
        "ownerName": a.get("holder") or "",
        "name": account_type,
        "currency": cur,
        "cashAccountType": _CASH_ACCOUNT_TYPE.get(account_type, "CACC"),
    }

    if a.get("balance") is not None:
        result["balances"] = [{
            "balanceAmount": {"amount": f"{float(a['balance']):.2f}", "currency": cur},
            "balanceType": "closingBooked",
        }]

    return result
