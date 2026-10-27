"""Fetch usage payload from ChatGPT usage endpoint."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from .auth import get_credentials

LOGGER = logging.getLogger(__name__)


def _is_header_safe(value: str) -> bool:
    """Visible ASCII only (33..126): no whitespace, control or non-ASCII."""
    return all(33 <= ord(char) <= 126 for char in value)


def fetch_usage() -> Optional[Dict[str, Any]]:
    try:
        # Re-borrow on every attempt: Hermes alone owns token refresh/writes.
        creds = get_credentials()
        if creds is None:
            return None

        url = f"{creds['base_url'].rstrip('/')}/backend-api/wham/usage"
        api_key = str(creds["api_key"])
        account_id = str(creds["account_id"]) if creds.get("account_id") else None
        # Reject before any header exists: h11 echoes an illegal header value
        # into its exception text, which would reach the traceback log. The
        # error names the field only, never the value.
        for field, value in (("api_key", api_key), ("account_id", account_id)):
            if value is not None and not _is_header_safe(value):
                raise ValueError(f"credential {field} has characters not allowed in an HTTP header value")
        headers = {"Authorization": f"Bearer {api_key}"}
        if account_id is not None:
            headers["ChatGPT-Account-Id"] = account_id

        response = httpx.get(url, headers=headers, timeout=30.0)
        response.raise_for_status()
        return response.json()
    except Exception as exc:  # credential, transport, status and JSON failures are retryable
        # Never log request headers, credential values or response bodies.
        LOGGER.exception("Usage fetch failed")
        # httpcore's connection pool uses `raise exc from None`, hiding the
        # original socket traceback. Retain that evidence too (e.g. whether
        # EBUSY came from getaddrinfo rather than connect or a file read).
        current = exc
        seen = {id(current)}
        while True:
            cause = current.__cause__ or current.__context__
            if cause is None or id(cause) in seen:
                break
            seen.add(id(cause))
            if current.__suppress_context__ and current.__cause__ is None:
                LOGGER.error(
                    "Usage fetch suppressed cause",
                    exc_info=(type(cause), cause, cause.__traceback__),
                )
            current = cause
        return None

