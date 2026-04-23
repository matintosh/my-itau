"""
Credential management for my-itau.

Priority (highest → lowest):
  1. Environment variables (ITAU_DOCUMENT_NUMBER, ITAU_PASSWORD, API_KEY)
  2. System keyring (macOS Keychain / Linux Secret Service via D-Bus)
  3. ~/.my-itau/config.json (plaintext fallback, chmod 600)

Sensitive fields (password, api key values) go to keyring when available and
are removed from the JSON file on first save.  Non-sensitive fields
(document_number, default_card, api key metadata) always live in the JSON file.

API key storage
---------------
Multiple named API keys are supported. Each key has:
  - alias       : short human name ("home", "ci", "mobile")
  - expires_at  : ISO 8601 datetime string, or null for no expiry
  - created_at  : ISO 8601 datetime string

Key values live in keyring under service "my-itau:apikey", username = alias.
When keyring is unavailable, the value is stored inline in the config file.
"""

import json
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

CONFIG_DIR = Path.home() / ".my-itau"
CONFIG_FILE = CONFIG_DIR / "config.json"

_SERVICE = "my-itau"
_APIKEY_SERVICE = "my-itau:apikey"

# ---------------------------------------------------------------------------
# Keyring helpers — all failures are silenced; callers check return values.
# ---------------------------------------------------------------------------

try:
    import keyring
    import keyring.errors as _kr_errors

    def _kr_get(service: str, key: str) -> Optional[str]:
        try:
            return keyring.get_password(service, key) or None
        except Exception:
            return None

    def _kr_set(service: str, key: str, value: str) -> bool:
        """Store *value* in keyring. Returns True on success."""
        try:
            keyring.set_password(service, key, value)
            return keyring.get_password(service, key) == value
        except Exception:
            return False

    def _kr_delete(service: str, key: str) -> None:
        try:
            keyring.delete_password(service, key)
        except Exception:
            pass

    def keyring_available() -> bool:
        return _KEYRING_OK

    _probe_key = "__probe__"
    try:
        keyring.set_password(_SERVICE, _probe_key, "1")
        _KEYRING_OK = keyring.get_password(_SERVICE, _probe_key) == "1"
        keyring.delete_password(_SERVICE, _probe_key)
    except Exception:
        _KEYRING_OK = False

except ImportError:
    _KEYRING_OK = False

    def _kr_get(service: str, key: str) -> Optional[str]:        # type: ignore[misc]
        return None

    def _kr_set(service: str, key: str, value: str) -> bool:     # type: ignore[misc]
        return False

    def _kr_delete(service: str, key: str) -> None:              # type: ignore[misc]
        pass

    def keyring_available() -> bool:                             # type: ignore[misc]
        return False


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _read_file() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write_file(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    CONFIG_FILE.chmod(0o600)


# ---------------------------------------------------------------------------
# Credential storage
# ---------------------------------------------------------------------------

def load() -> dict:
    """Return merged config. Keyring beats file; env vars beat both."""
    cfg = _read_file()

    for field in ("password",):
        val = _kr_get(_SERVICE, field)
        if val:
            cfg[field] = val

    if os.getenv("ITAU_DOCUMENT_NUMBER"):
        cfg["document_number"] = os.getenv("ITAU_DOCUMENT_NUMBER")
    if os.getenv("ITAU_PASSWORD"):
        cfg["password"] = os.getenv("ITAU_PASSWORD")

    return cfg


def save(document_number: str, password: str) -> bool:
    """
    Persist credentials. Password goes to keyring when available, else file.
    Returns True if keyring was used.
    """
    cfg = _read_file()
    cfg["document_number"] = document_number

    if _kr_set(_SERVICE, "password", password):
        cfg.pop("password", None)
        stored_in_keyring = True
    else:
        cfg["password"] = password
        stored_in_keyring = False

    _write_file(cfg)
    return stored_in_keyring


def save_default_card(card_hash: str) -> None:
    cfg = _read_file()
    cfg["default_card"] = card_hash
    _write_file(cfg)


def default_card() -> str:
    return _read_file().get("default_card", "")


def credentials() -> Tuple[str, str]:
    """Return (document_number, password) or ('', '') if not configured."""
    cfg = load()
    return cfg.get("document_number", ""), cfg.get("password", "")


# ---------------------------------------------------------------------------
# API key management
# ---------------------------------------------------------------------------

def generate_api_key() -> str:
    """Generate a cryptographically random API key."""
    return secrets.token_urlsafe(32)


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


def _is_expired(entry: dict) -> bool:
    exp = entry.get("expires_at")
    if not exp:
        return False
    try:
        return datetime.utcnow() > datetime.fromisoformat(exp)
    except ValueError:
        return False


def list_api_keys() -> List[Dict]:
    """
    Return all API key entries with their metadata.
    Each entry: {alias, created_at, expires_at, expired, in_keyring}.
    Key values are never returned.
    """
    cfg = _read_file()
    entries = cfg.get("api_keys", [])

    # Backward compat: old single api_key string
    if not entries and cfg.get("api_key"):
        entries = [{"alias": "default", "created_at": None, "expires_at": None}]

    result = []
    for e in entries:
        in_keyring = _kr_get(_APIKEY_SERVICE, e["alias"]) is not None
        result.append({
            "alias":      e["alias"],
            "created_at": e.get("created_at"),
            "expires_at": e.get("expires_at"),
            "expired":    _is_expired(e),
            "in_keyring": in_keyring,
        })
    return result


def add_api_key(alias: str, key: str, expires_at: Optional[str] = None) -> bool:
    """
    Store a new API key. Returns True if value went to keyring.
    Replaces any existing entry with the same alias.
    """
    cfg = _read_file()
    entries = cfg.get("api_keys", [])

    # Migrate legacy single key → named entry
    if "api_key" in cfg and not any(e["alias"] == "default" for e in entries):
        old_key = cfg.pop("api_key")
        _kr_delete(_SERVICE, "api_key")
        entries.append({"alias": "default", "created_at": _now_iso(), "expires_at": None})
        _migrate_old_key(old_key, entries)

    # Remove existing entry with same alias
    entries = [e for e in entries if e["alias"] != alias]
    _kr_delete(_APIKEY_SERVICE, alias)

    entry: Dict = {"alias": alias, "created_at": _now_iso(), "expires_at": expires_at}

    if _kr_set(_APIKEY_SERVICE, alias, key):
        stored_in_keyring = True
    else:
        entry["key"] = key
        stored_in_keyring = False

    entries.append(entry)
    cfg["api_keys"] = entries
    _write_file(cfg)
    return stored_in_keyring


def _migrate_old_key(old_key: str, entries: List[Dict]) -> None:
    """Move old single api_key value into the new named-key system."""
    if not _kr_set(_APIKEY_SERVICE, "default", old_key):
        for e in entries:
            if e["alias"] == "default":
                e["key"] = old_key


def remove_api_key(alias: str) -> bool:
    """Remove an API key by alias. Returns True if it existed."""
    cfg = _read_file()
    entries = cfg.get("api_keys", [])
    before = len(entries)
    entries = [e for e in entries if e["alias"] != alias]
    _kr_delete(_APIKEY_SERVICE, alias)
    cfg["api_keys"] = entries
    _write_file(cfg)
    return len(entries) < before


def validate_api_key(presented: str) -> Optional[str]:
    """
    Check *presented* against all active stored keys.
    Returns the matched alias on success, None on failure.
    ENV override: API_KEY env var is always accepted (returns alias "env").
    If no keys are configured at all, returns "open" (server is unsecured).
    """
    env_key = os.getenv("API_KEY")
    if env_key:
        if presented == env_key:
            return "env"
        # Env key is set — reject anything that doesn't match it
        # unless there are also stored keys we should check
        cfg = _read_file()
        if not cfg.get("api_keys"):
            return None

    cfg = _read_file()
    entries = cfg.get("api_keys", [])

    # Backward compat: old single api_key
    if not entries:
        old = cfg.get("api_key") or (env_key or "")
        if not old:
            return "open"
        return "default" if presented == old else None

    if not entries:
        return "open"

    for entry in entries:
        if _is_expired(entry):
            continue
        alias = entry["alias"]
        stored = _kr_get(_APIKEY_SERVICE, alias) or entry.get("key", "")
        if stored and presented == stored:
            return alias

    return None


def any_api_keys_configured() -> bool:
    """True if at least one API key is stored (env var counts)."""
    if os.getenv("API_KEY"):
        return True
    cfg = _read_file()
    return bool(cfg.get("api_keys") or cfg.get("api_key"))


def purge_keyring() -> None:
    """Remove all my-itau entries from the system keyring."""
    _kr_delete(_SERVICE, "password")
    for entry in _read_file().get("api_keys", []):
        _kr_delete(_APIKEY_SERVICE, entry["alias"])
    # Legacy
    _kr_delete(_SERVICE, "api_key")
