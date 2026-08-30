"""Shared test scaffolding.

Lane retries pause for a few seconds between attempts in production. The suite
exercises deliberately broken lanes constantly, so the default backoff sleep is
neutralized here; tests that assert the backoff schedule inject their own
``lane_sleep`` through ``collect_all``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_lane_backoff(monkeypatch):
    monkeypatch.setattr(
        "codex_usage_tracker.collector._default_lane_sleep",
        lambda seconds: None,
        raising=False,
    )
