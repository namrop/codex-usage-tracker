"""Authentication helpers for Codex usage tracker."""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

LOGGER = logging.getLogger(__name__)

TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_FILE = Path.home() / ".hermes" / "auth.json"
BASE_URL = "https://chatgpt.com"


def _read_auth_file() -> Optional[Dict[str, Any]]:
    if not AUTH_FILE.exists():
        LOGGER.error("Auth file not found: %s", AUTH_FILE)
        return None

    try:
        with AUTH_FILE.open("r", encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.error("Failed to read auth file %s: %s", AUTH_FILE, exc)
        return None


def _write_auth_file(payload: Dict[str, Any]) -> bool:
    try:
        AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        with AUTH_FILE.open("w", encoding="utf-8") as fp:
            json.dump(payload, fp, indent=2)
        return True
    except OSError as exc:
        LOGGER.error("Failed to save auth file %s: %s", AUTH_FILE, exc)
        return False


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


def _is_token_expired(token: str) -> bool:
    payload = _decode_jwt_payload(token)
    if not payload:
        return True
    exp = payload.get("exp")
    if not isinstance(exp, int):
        return True
    return time.time() >= float(exp)


def _extract_tokens(auth_payload: Dict[str, Any]) -> Dict[str, Any]:
    providers = auth_payload.get("providers", {})
    provider_block = providers.get("openai-codex", {})
    return provider_block.get("tokens", {}) if isinstance(provider_block, dict) else {}


def _persist_tokens(
    auth_payload: Dict[str, Any],
    tokens: Dict[str, Any],
) -> bool:
    providers = auth_payload.setdefault("providers", {})
    provider_block = providers.setdefault("openai-codex", {})
    if not isinstance(provider_block, dict):
        provider_block = {}
        providers["openai-codex"] = provider_block
    provider_block["tokens"] = tokens
    return _write_auth_file(auth_payload)


def _refresh_access_token(refresh_token: str) -> Optional[Dict[str, Any]]:
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }
    try:
        response = httpx.post(TOKEN_URL, data=payload, timeout=30.0)
        response.raise_for_status()
        return response.json()
    except Exception as exc:  # broad for compatibility with request/parsing failures
        LOGGER.error("Token refresh failed: %s", exc)
        return None


def get_credentials() -> Optional[Dict[str, Any]]:
    auth_payload = _read_auth_file()
    if auth_payload is None:
        return None

    tokens = _extract_tokens(auth_payload)
    if not isinstance(tokens, dict):
        LOGGER.error("Invalid token payload in auth file")
        return None

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    account_id = tokens.get("account_id")

    if isinstance(access_token, str) and not _is_token_expired(access_token):
        return {
            "api_key": access_token,
            "base_url": BASE_URL,
            "account_id": account_id if isinstance(account_id, str) else None,
        }

    if not isinstance(refresh_token, str):
        LOGGER.error("No valid refresh_token found; cannot refresh expred access token")
        return None

    refreshed = _refresh_access_token(refresh_token)
    if refreshed is None:
        return None

    new_access_token = refreshed.get("access_token")
    if not isinstance(new_access_token, str):
        LOGGER.error("Token refresh response missing access_token")
        return None

    new_refresh_token = refreshed.get("refresh_token")
    if not isinstance(new_refresh_token, str):
        new_refresh_token = refresh_token

    refreshed_tokens = dict(tokens)
    refreshed_tokens["access_token"] = new_access_token
    refreshed_tokens["refresh_token"] = new_refresh_token
    if isinstance(refreshed.get("account_id"), str):
        refreshed_tokens["account_id"] = refreshed["account_id"]

    if not _persist_tokens(auth_payload, refreshed_tokens):
        LOGGER.warning("Could not persist refreshed tokens to %s", AUTH_FILE)

    return {
        "api_key": new_access_token,
        "base_url": BASE_URL,
        "account_id": (
            refreshed_tokens.get("account_id")
            if isinstance(refreshed_tokens.get("account_id"), str)
            else None
        ),
    }

