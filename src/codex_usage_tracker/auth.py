"""Read-only authentication helpers for Codex usage tracker.

This module deliberately **never writes** ``auth.json``. Hermes is the sole
owner of the ChatGPT/Codex OAuth token lifecycle; the tracker is a read-only
consumer that borrows the current access token to poll a usage endpoint.

Why this is a hard rule, not a preference — this module used to refresh the
token itself, and that caused a live outage:

1. ``auth.json`` holds *two* copies of the Codex credentials: the singleton at
   ``providers.openai-codex.tokens`` and a pool entry at
   ``credential_pool.openai-codex``. The pool copy is what serves Hermes's
   actual API traffic. A refresh here rotated the singleton only, leaving the
   running gateway holding a token that had just been invalidated — it then
   failed with ``HTTP 401: Provided authentication token is expired``. Even
   writing both copies would not fix this: the gateway holds an *in-memory*
   pool snapshot, so any out-of-process refresh can strand it. Only the process
   that owns the pool can refresh the token safely.
2. Hermes serializes its writes with an flock on ``auth.lock`` and maintains
   ``last_refresh``. This module did neither, and rewrote the whole file
   non-atomically from a possibly-stale in-memory copy — able to clobber the
   credentials of the eight other providers that share the file, or to corrupt
   it outright.

So: if the access token is expired, we skip the poll and say so loudly. Hermes
refreshes proactively (120 s before expiry) whenever it selects a Codex
credential, so the gap is normally brief. A persistent gap is a real signal
that Hermes-side auth needs attention — not something for this module to paper
over by minting tokens behind Hermes's back.

See ``07_systems/hermes/incidents/hermes_codex_oauth_dual_uncoordinated_refreshers_auth_store_write_race_incident_2026-08-15.md``
in the Atrium canon for the full incident.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

LOGGER = logging.getLogger(__name__)

BASE_URL = "https://chatgpt.com"

# Treat a token expiring within this window as unusable, so we don't start a
# request with a token that dies mid-flight.
TOKEN_EXPIRY_SKEW_SECONDS = 60


def _resolve_auth_file() -> Path:
    """Resolve Codex OAuth token storage across Hermes layouts.

    Legacy desktop installs store auth at ``~/.hermes/auth.json``. Hermes
    profiles and Sol primary use ``HERMES_HOME`` directly, with auth at
    ``$HERMES_HOME/auth.json``. ``CODEX_USAGE_AUTH_FILE`` is a narrow escape
    hatch for supervised deployments that need an explicit path.
    """

    explicit = os.environ.get("CODEX_USAGE_AUTH_FILE")
    if explicit:
        return Path(explicit).expanduser()

    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        return Path(hermes_home).expanduser() / "auth.json"

    return Path.home() / ".hermes" / "auth.json"


def _read_auth_file() -> Optional[Dict[str, Any]]:
    auth_file = _resolve_auth_file()
    if not auth_file.exists():
        LOGGER.error("Auth file not found: %s", auth_file)
        return None

    try:
        with auth_file.open("r", encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.error("Failed to read auth file %s: %s", auth_file, exc)
        return None


def _decode_jwt_payload(token: str) -> Optional[Dict[str, Any]]:
    try:
        _, payload_b64, _ = token.split(".")
    except ValueError:
        return None

    padding = "=" * ((4 - len(payload_b64) % 4) % 4)
    payload_b64 += padding
    try:
        payload = base64.urlsafe_b64decode(payload_b64.encode("ascii"))
        return json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _token_expiry(token: str) -> Optional[float]:
    """Return the token's ``exp`` claim, or None if it cannot be determined."""
    payload = _decode_jwt_payload(token)
    if not payload:
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return float(exp)


def _is_token_expired(token: str, skew_seconds: float = TOKEN_EXPIRY_SKEW_SECONDS) -> bool:
    """True if the token is expired, expiring within ``skew_seconds``, or unreadable.

    Unreadable tokens count as expired: this module cannot refresh, so the only
    safe response to a token it cannot vouch for is to decline to use it.
    """
    exp = _token_expiry(token)
    if exp is None:
        return True
    return time.time() + max(0.0, skew_seconds) >= exp


def _pool_tokens(auth_payload: Dict[str, Any]) -> Dict[str, Any]:
    """The newest usable ``credential_pool.openai-codex`` entry's tokens.

    ``hermes auth add openai-codex`` writes a ``manual:device_code`` pool
    entry and no singleton (2026-09-11: after the live store lost its Codex
    credential, that is how it comes back), so a singleton-only reader stays
    dead after a re-auth. Entries the pool has marked dead are skipped; the
    most recently refreshed entry wins.
    """
    pool = auth_payload.get("credential_pool", {})
    entries = pool.get("openai-codex") if isinstance(pool, dict) else None
    if not isinstance(entries, list):
        return {}
    usable = [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("access_token"), str)
        and entry.get("access_token")
        and str(entry.get("last_status") or "").lower() != "dead"
    ]
    if not usable:
        return {}

    def freshness(entry: Dict[str, Any]) -> float:
        for key in ("last_refresh", "last_status_at"):
            value = entry.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return 0.0

    newest = max(usable, key=freshness)
    return {
        "access_token": newest.get("access_token"),
        "account_id": newest.get("account_id"),
    }


def _extract_tokens(auth_payload: Dict[str, Any]) -> Dict[str, Any]:
    providers = auth_payload.get("providers", {})
    provider_block = providers.get("openai-codex", {})
    tokens = provider_block.get("tokens", {}) if isinstance(provider_block, dict) else {}
    if isinstance(tokens, dict) and isinstance(tokens.get("access_token"), str) and tokens.get("access_token"):
        return tokens
    return _pool_tokens(auth_payload)


def get_credentials() -> Optional[Dict[str, Any]]:
    """Borrow Hermes's current Codex access token, read-only.

    Returns None — never refreshes — when the stored token is missing or
    expired. See the module docstring for why this module must not refresh.
    """
    auth_payload = _read_auth_file()
    if auth_payload is None:
        return None

    tokens = _extract_tokens(auth_payload)
    if not isinstance(tokens, dict):
        LOGGER.error("Invalid token payload in auth file")
        return None

    access_token = tokens.get("access_token")
    account_id = tokens.get("account_id")

    if not isinstance(access_token, str) or not access_token:
        LOGGER.error(
            "No Codex access_token in %s. Hermes owns this credential; "
            "run `hermes auth add openai-codex` to (re)authorize.",
            _resolve_auth_file(),
        )
        return None

    if _is_token_expired(access_token):
        exp = _token_expiry(access_token)
        if exp is None:
            expiry_note = "could not be decoded"
        else:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp))
            verb = "expired at" if time.time() >= exp else "expires at"
            expiry_note = f"{verb} {stamp}"
        LOGGER.error(
            "Codex access token %s; skipping this poll. This module is a "
            "read-only consumer and does not refresh — Hermes refreshes the "
            "token when it next uses the provider. If this persists, check "
            "Hermes-side Codex auth (`hermes auth status`).",
            expiry_note,
        )
        return None

    return {
        "api_key": access_token,
        "base_url": BASE_URL,
        "account_id": account_id if isinstance(account_id, str) else None,
    }

