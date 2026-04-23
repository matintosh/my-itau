# Backward-compat shim — canonical module is my_itau.client.
from my_itau.client import (  # noqa: F401
    BASE_URL,
    ERROR_CODES,
    ItauAuthError,
    ItauClient,
    ItauSessionExpired,
    TRX,
)
