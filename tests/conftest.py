"""Shared test scaffolding.

Lane retries pause for a few seconds between attempts in production. The suite
exercises deliberately broken lanes constantly, so the default backoff sleep is
neutralized here; tests that assert the backoff schedule inject their own
``lane_sleep`` through ``collect_all``.

Hermes home resolution is pinned to a generic host by default: on Sol the real
``/var/lib/hermes/primary`` root exists, and a test that resolved onto it would
be reading the live credential store. Tests that exercise the Sol route mock
the presence flag back to True themselves.
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


@pytest.fixture(autouse=True)
def _sol_primary_absent_by_default(monkeypatch):
    monkeypatch.setattr(
        "codex_usage_tracker.hermes_home._sol_primary_present",
        lambda: False,
        raising=False,
    )
