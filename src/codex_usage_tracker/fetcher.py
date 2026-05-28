"""Fetch usage payload from ChatGPT usage endpoint."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from .auth import get_credentials

LOGGER = logging.getLogger(__name__)


def fetch_usage() -> Optional[Dict[str, Any]]:
    creds = get_credentials()
    if creds is None:
        return None

    url = f"{creds['base_url'].rstrip('/')}/backend-api/wham/usage"
    headers = {"Authorization": f"Bearer {creds['api_key']}"}
    if creds.get("account_id"):
        headers["ChatGPT-Account-Id"] = str(creds["account_id"])

    try:
        response = httpx.get(url, headers=headers, timeout=30.0)
        response.raise_for_status()
        return response.json()
    except Exception as exc:  # broad for compatibility with both transport and response errors
        LOGGER.error("Usage fetch failed: %s", exc)
        return None

