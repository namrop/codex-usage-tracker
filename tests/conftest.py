"""Shared test scaffolding.

Lane retries pause for a few seconds between attempts in production. The suite
exercises deliberately broken lanes constantly, so the default backoff sleep is
neutralized here; tests that assert the backoff schedule inject their own
``lane_sleep`` through ``collect_all``.

Hermes home resolution is pinned to a generic host by default: on Sol the real
``/var/lib/hermes/primary`` root exists, and a test that resolved onto it would
be reading the live credential store. Tests that exercise the Sol route mock
the presence flag back to True themselves.

OpenClaw and the search lane's default sources and ledger are pinned to the
test's tmp dir the same way (see ``_isolate_default_sources``).
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


@pytest.fixture(autouse=True)
def _isolate_default_sources(tmp_path, monkeypatch):
    """Keep every default path a test does not name inside ``tmp_path``.

    - ``collect-all`` / ``collect-searches`` default to reading ~/.openclaw.
    - ``collect_all`` with scope ``all`` and no ``search_ledger`` used to read
      the real Hermes search audit feed and append to the real search ledger.
      Until 2026-09-25 the test suite was, unknowingly, the only thing that
      ever collected searches on Sol (found while wiring Brave tracking, keeper
      msg 1553067845766742228), and a run could fail when the real ledger had
      an active WAL.
    """
    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path / "no-openclaw-home"))
    # ``collect-all`` also defaults to reading ~/.gemini/antigravity-cli.
    monkeypatch.setenv("ANTIGRAVITY_HOME", str(tmp_path / "no-antigravity-home"))
    # CLI parser defaults (the dashboard reads SEARCH_LEDGER_PATH as "is a
    # ledger configured", so the module defaults are patched, not the env).
    monkeypatch.setattr("codex_usage_tracker.cli.DEFAULT_SEARCH_LEDGER", str(tmp_path / "default-search-ledger.sqlite3"))
    monkeypatch.setattr("codex_usage_tracker.cli.DEFAULT_SEARCH_AUDIT", str(tmp_path / "default-search-audit.jsonl"))
    monkeypatch.setattr(
        "codex_usage_tracker.collector.DEFAULT_SEARCH_LEDGER", str(tmp_path / "default-search-ledger.sqlite3")
    )
    monkeypatch.setattr(
        "codex_usage_tracker.collector.default_search_audit_path",
        lambda environ=None: tmp_path / "default-search-audit.jsonl",
    )
