"""Collector convergence: source regressions, re-finalization, probe retry.

These cover the three failure modes that kept the hourly collection lane red:
a transcript that lost its terminal receipt and now re-derives a strictly
poorer fact, a stream that finalizes a second time after an earlier
correction, and a bimodal ``/usage`` TUI probe that misses its first window.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from codex_usage_tracker import quota as quota_module
from codex_usage_tracker.canonical_ledger import append_sqlite_facts, read_sqlite_facts
from codex_usage_tracker.collector import StrictSourcesError, collect_all, record_adjudications


def _claude_fact(source_event_id: str = "private-source-id", **overrides: Any) -> dict[str, Any]:
    row = {
        "fact_type": "usage_event_v1",
        "schema_version": 1,
        "source_namespace": "test:claude",
        "source_event_id": source_event_id,
        "harness": "claude_code",
        "provider": "anthropic",
        "purpose": "main",
        "record_kind": "api_attempt",
        "occurred_at": "2026-08-26T12:00:00Z",
        "recorded_at": "2026-08-26T12:00:00Z",
        "usage_source": "provider_reported",
        "usage_completeness": "complete",
        "measurement_confidence": "exact",
        "cost_status": "included",
    }
    row.update(overrides)
    return row


def _stub_claude(monkeypatch, rows):
    monkeypatch.setattr("codex_usage_tracker.collector.collect_hermes_usage", lambda *args: [])
    monkeypatch.setattr("codex_usage_tracker.collector.collect_claude_usage", lambda *args: list(rows))
    monkeypatch.setattr("codex_usage_tracker.collector.collect_opencode_usage", lambda *args: [])


def _kwargs(tmp_path: Path, usage: Path) -> dict[str, Any]:
    return dict(
        state_db=tmp_path / "state.db",
        claude_root=tmp_path / "claude",
        opencode_dbs=[],
        codex_ledger=tmp_path / "codex.jsonl",
        usage_ledger=usage,
        quota_ledger=tmp_path / "quota.sqlite3",
        live_quota=False,
        scope="usage",
        strict_sources=True,
    )


# --- 1a: source regression -------------------------------------------------


def test_lost_terminal_receipt_keeps_canonical_fact_and_adjudicates_once(tmp_path, monkeypatch):
    """A torn-write transcript re-derives less; canon wins and stops failing."""
    usage = tmp_path / "usage.sqlite3"
    settled = _claude_fact(
        request_status="ok",
        input_tokens=4,
        cache_read_tokens=220332,
        cache_write_tokens=5405,
        output_tokens=1101,
        reasoning_tokens=900,
    )
    regressed = dict(
        settled,
        request_status="unknown",
        output_tokens=17,
        reasoning_tokens=0,
    )
    append_sqlite_facts(usage, [settled], fact_type="usage_event_v1")
    _stub_claude(monkeypatch, [regressed])
    kwargs = _kwargs(tmp_path, usage)

    first = collect_all(**kwargs)
    second = collect_all(**kwargs)

    rows = read_sqlite_facts(usage, fact_type="usage_event_v1")
    assert len(rows) == 1
    assert rows[0]["output_tokens"] == 1101
    assert rows[0]["request_status"] == "ok"

    for result in (first, second):
        assert result["conflicts"] == []
        assert result["warnings"] == []
        assert result["usage"] == {"appended": 0, "discovered": 1, "replayed": 1}
        assert len(result["resolutions"]) == 1
        assert result["resolutions"][0]["resolution"] == "source_regression"
        assert result["resolutions"][0]["source"] == "claude_code"
        assert result["resolutions"][0]["changed_fields"] == [
            "output_tokens",
            "reasoning_tokens",
            "request_status",
        ]

    # Record once, then quiet: the second run still reports the regression but
    # does not re-record it, and never re-warns.
    assert first["resolutions"][0]["newly_recorded"] is True
    assert second["resolutions"][0]["newly_recorded"] is False
    assert (
        second["resolutions"][0]["first_recorded_at"]
        == first["resolutions"][0]["first_recorded_at"]
    )
    assert first["counts"]["resolutions_recorded"] == 1
    assert second["counts"]["resolutions_recorded"] == 0
    assert "private-source-id" not in json.dumps(second)


def test_source_regression_store_is_private_and_keyed_on_incoming_sha(tmp_path, monkeypatch):
    usage = tmp_path / "usage.sqlite3"
    settled = _claude_fact(request_status="ok", output_tokens=100, input_tokens=10)
    append_sqlite_facts(usage, [settled], fact_type="usage_event_v1")
    _stub_claude(monkeypatch, [dict(settled, request_status="unknown", output_tokens=40)])
    kwargs = _kwargs(tmp_path, usage)
    collect_all(**kwargs)

    store = tmp_path / "adjudications.sqlite3"
    assert store.exists()
    assert store.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(store) as connection:
        recorded = connection.execute(
            "SELECT resolution, source FROM adjudications"
        ).fetchall()
    assert recorded == [("source_regression", "claude_code")]

    # A different regressed re-derivation is a new incoming sha: adjudicated
    # again rather than silently absorbed into the earlier ruling.
    _stub_claude(monkeypatch, [dict(settled, request_status="unknown", output_tokens=41)])
    later = collect_all(**kwargs)
    assert later["counts"]["resolutions_recorded"] == 1
    with sqlite3.connect(store) as connection:
        assert connection.execute("SELECT count(*) FROM adjudications").fetchone()[0] == 2


def test_token_growth_is_never_treated_as_a_regression(tmp_path, monkeypatch):
    """Mixed-direction change stays quarantined; canon is not silently kept."""
    usage = tmp_path / "usage.sqlite3"
    settled = _claude_fact(request_status="ok", output_tokens=100, input_tokens=10)
    append_sqlite_facts(usage, [settled], fact_type="usage_event_v1")
    _stub_claude(
        monkeypatch,
        [dict(settled, request_status="unknown", output_tokens=40, input_tokens=99)],
    )

    with pytest.raises(RuntimeError, match="strict source policy"):
        collect_all(**_kwargs(tmp_path, usage))


def test_competing_ok_observations_stay_quarantined(tmp_path, monkeypatch):
    """Two rows that both claim completion are ambiguous, not a regression."""
    usage = tmp_path / "usage.sqlite3"
    settled = _claude_fact(request_status="ok", output_tokens=100)
    append_sqlite_facts(usage, [settled], fact_type="usage_event_v1")
    _stub_claude(monkeypatch, [dict(settled, request_status="ok", output_tokens=40)])

    with pytest.raises(RuntimeError, match="strict source policy"):
        collect_all(**_kwargs(tmp_path, usage))


def test_record_adjudications_is_idempotent_on_repeat(tmp_path):
    store = tmp_path / "state" / "adjudications.sqlite3"
    resolution = {
        "source": "claude_code",
        "resolution": "source_regression",
        "identity_sha256": "a" * 64,
        "existing_sha256": "b" * 64,
        "incoming_sha256": "c" * 64,
        "changed_fields": ["output_tokens"],
    }
    first = record_adjudications(store, [resolution], now="2026-08-29T00:00:00Z")
    second = record_adjudications(store, [resolution], now="2026-08-30T00:00:00Z")
    assert first[0]["newly_recorded"] is True
    assert second[0]["newly_recorded"] is False
    assert second[0]["first_recorded_at"] == "2026-08-29T00:00:00Z"
    assert record_adjudications(store, []) == []


# --- 1b: re-finalization ---------------------------------------------------


def test_second_monotonic_finalization_emits_an_additional_delta(tmp_path, monkeypatch):
    usage = tmp_path / "usage.sqlite3"
    original = _claude_fact(request_status="unknown", output_tokens=5, input_tokens=10)
    append_sqlite_facts(usage, [original], fact_type="usage_event_v1")
    kwargs = _kwargs(tmp_path, usage)

    _stub_claude(monkeypatch, [dict(original, request_status="ok", output_tokens=1028)])
    first = collect_all(**kwargs)
    assert first["generated_corrections"] == 1

    # The transcript keeps growing after the first finalization landed.
    _stub_claude(monkeypatch, [dict(original, request_status="ok", output_tokens=2000)])
    second = collect_all(**kwargs)
    third = collect_all(**kwargs)

    rows = read_sqlite_facts(usage, fact_type="usage_event_v1")
    corrections = [row for row in rows if row["record_kind"] == "correction"]
    assert second["generated_corrections"] == 1
    assert second["conflicts"] == []
    assert third["generated_corrections"] == 0
    assert third["conflicts"] == []
    assert len(corrections) == 2
    assert sorted(int(row["output_tokens"]) for row in corrections) == [972, 1023]
    # The original attempt is still immutable and the totals still sum exactly.
    assert next(row for row in rows if row["record_kind"] == "api_attempt")["output_tokens"] == 5
    assert sum(int(row.get("output_tokens") or 0) for row in rows) == 2000


def test_repeat_finalization_with_zero_delta_emits_nothing(tmp_path, monkeypatch):
    """The live shape: an already-corrected identity re-derives the same total."""
    usage = tmp_path / "usage.sqlite3"
    original = _claude_fact(request_status="unknown", output_tokens=2, cache_read_tokens=108477)
    finalized = dict(
        original,
        request_status="ok",
        output_tokens=1101,
        cache_read_tokens=220332,
        occurred_at="2026-08-26T12:03:00Z",
        recorded_at="2026-08-26T12:03:00Z",
    )
    append_sqlite_facts(usage, [original], fact_type="usage_event_v1")
    _stub_claude(monkeypatch, [finalized])
    kwargs = _kwargs(tmp_path, usage)

    results = [collect_all(**kwargs) for _ in range(3)]
    rows = read_sqlite_facts(usage, fact_type="usage_event_v1")

    assert [result["generated_corrections"] for result in results] == [1, 0, 0]
    assert all(result["conflicts"] == [] for result in results)
    assert all(result["resolutions"] == [] for result in results)
    assert len([row for row in rows if row["record_kind"] == "correction"]) == 1
    assert sum(int(row.get("output_tokens") or 0) for row in rows) == 1101


def test_non_monotonic_change_after_a_correction_stays_quarantined(tmp_path, monkeypatch):
    usage = tmp_path / "usage.sqlite3"
    original = _claude_fact(request_status="unknown", output_tokens=5)
    append_sqlite_facts(usage, [original], fact_type="usage_event_v1")
    kwargs = _kwargs(tmp_path, usage)

    _stub_claude(monkeypatch, [dict(original, request_status="ok", output_tokens=1028)])
    collect_all(**kwargs)

    # Still claims ``ok`` but with a smaller total than the corrected state.
    _stub_claude(monkeypatch, [dict(original, request_status="ok", output_tokens=600)])
    with pytest.raises(RuntimeError, match="strict source policy"):
        collect_all(**kwargs)


# --- 1c: per-run report ----------------------------------------------------


def test_collect_report_is_written_on_success_and_on_strict_failure(tmp_path, monkeypatch):
    usage = tmp_path / "usage.sqlite3"
    settled = _claude_fact(request_status="ok", output_tokens=100)
    append_sqlite_facts(usage, [settled], fact_type="usage_event_v1")
    _stub_claude(monkeypatch, [dict(settled, request_status="unknown", output_tokens=40)])
    kwargs = _kwargs(tmp_path, usage)

    result = collect_all(**kwargs)
    report_path = Path(result["paths"]["report"])
    assert report_path == tmp_path / "health" / "collect-usage-last.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["scope"] == "usage"
    assert report["generated_at"].endswith("Z")
    assert report["resolutions"][0]["resolution"] == "source_regression"
    assert report["counts"]["resolutions_recorded"] == 1

    # A genuine conflict is reported too, and the exception routes to the file.
    _stub_claude(monkeypatch, [dict(settled, request_status="ok", output_tokens=41)])
    from codex_usage_tracker.collector import StrictSourcesError

    with pytest.raises(StrictSourcesError) as raised:
        collect_all(**kwargs)
    assert raised.value.report_path == str(report_path)
    failure = json.loads(report_path.read_text(encoding="utf-8"))
    assert failure["counts"]["conflicts"] == 1
    assert failure["conflicts"][0]["error"] == "IdentityConflictError"
    assert "private-source-id" not in json.dumps(failure)


def test_dry_run_writes_no_report_and_no_adjudication_store(tmp_path, monkeypatch):
    usage = tmp_path / "usage.jsonl"
    _stub_claude(monkeypatch, [])
    collect_all(**dict(_kwargs(tmp_path, usage), quota_ledger=tmp_path / "quota.jsonl", dry_run=True, strict_sources=False))
    assert not (tmp_path / "health").exists()
    assert not (tmp_path / "adjudications.sqlite3").exists()


def test_collect_summary_line_tolerates_a_minimal_result():
    from codex_usage_tracker.cli import _collect_summary_line

    assert _collect_summary_line({"ok": True}).startswith("collect-all: scope=all")
    line = _collect_summary_line(
        {
            "scope": "usage",
            "usage": {"appended": 2, "replayed": 5},
            "counts": {"conflicts": 0, "resolutions": 3, "resolutions_recorded": 1, "warnings": 0},
            "generated_corrections": 1,
            "paths": {"report": "/tmp/report.json"},
        }
    )
    assert "quarantined=0" in line and "regressions=3" in line
    assert "newly_adjudicated=1" in line and "report=/tmp/report.json" in line


# --- 2a: per-lane health and heartbeat semantics ---------------------------


def test_heartbeat_is_touched_even_when_a_lane_quarantines(tmp_path, monkeypatch):
    """The heartbeat states that canonical writes landed, not that all is well."""
    usage = tmp_path / "usage.sqlite3"
    settled = _claude_fact(request_status="ok", output_tokens=100)
    append_sqlite_facts(usage, [settled], fact_type="usage_event_v1")
    _stub_claude(monkeypatch, [dict(settled, request_status="ok", output_tokens=41)])
    heartbeat = tmp_path / "health" / "usage-collect.ok"

    with pytest.raises(StrictSourcesError):
        collect_all(**_kwargs(tmp_path, usage))

    assert heartbeat.exists()
    assert heartbeat.stat().st_mode & 0o777 == 0o644
    # A usage run must never touch the quota heartbeat: a cross-touch would let
    # one healthy scope mask the other's dead collector from the watchdog.
    assert not (tmp_path / "health" / "quota-collect.ok").exists()
    lanes = json.loads((tmp_path / "health" / "lanes-usage.json").read_text(encoding="utf-8"))
    assert not (tmp_path / "health" / "lanes-quota.json").exists()
    assert lanes["schema_version"] == 1 and lanes["scope"] == "usage"
    assert lanes["lanes"]["claude_code"]["last_error_class"] == "IdentityConflictError"
    assert lanes["lanes"]["claude_code"]["quarantined_count"] == 1
    assert lanes["lanes"]["claude_code"]["last_success_at"] is None
    assert lanes["lanes"]["hermes"]["last_success_at"] == lanes["updated_at"]
    assert "private-source-id" not in json.dumps(lanes)


def test_lane_health_keeps_the_last_success_of_a_lane_that_later_fails(tmp_path, monkeypatch):
    usage = tmp_path / "usage.sqlite3"
    settled = _claude_fact(request_status="ok", output_tokens=100)
    _stub_claude(monkeypatch, [settled])
    collect_all(**_kwargs(tmp_path, usage))
    healthy = json.loads((tmp_path / "health" / "lanes-usage.json").read_text(encoding="utf-8"))
    first_success = healthy["lanes"]["claude_code"]["last_success_at"]
    assert first_success is not None
    assert healthy["lanes"]["claude_code"]["rows_written"] == 1

    _stub_claude(monkeypatch, [dict(settled, request_status="ok", output_tokens=41)])
    with pytest.raises(StrictSourcesError):
        collect_all(**_kwargs(tmp_path, usage))

    degraded = json.loads((tmp_path / "health" / "lanes-usage.json").read_text(encoding="utf-8"))
    lane = degraded["lanes"]["claude_code"]
    assert lane["last_success_at"] == first_success
    assert lane["last_run_at"] >= first_success
    assert lane["last_error_class"] == "IdentityConflictError"


def test_lane_health_records_a_source_read_failure(tmp_path, monkeypatch):
    usage = tmp_path / "usage.sqlite3"

    def broken(*args, **kwargs):
        raise OSError("private source path")

    monkeypatch.setattr("codex_usage_tracker.collector.collect_hermes_usage", broken)
    monkeypatch.setattr("codex_usage_tracker.collector.collect_claude_usage", lambda *args: [])
    monkeypatch.setattr("codex_usage_tracker.collector.collect_opencode_usage", lambda *args: [])

    with pytest.raises(StrictSourcesError):
        collect_all(**_kwargs(tmp_path, usage))

    lanes = json.loads((tmp_path / "health" / "lanes-usage.json").read_text(encoding="utf-8"))
    assert lanes["lanes"]["hermes"]["last_error_class"] == "OSError"
    assert lanes["lanes"]["hermes"]["last_success_at"] is None
    assert lanes["lanes"]["opencode"]["last_success_at"] == lanes["updated_at"]
    # The scope's canonical append still committed, so the heartbeat is honest.
    assert (tmp_path / "health" / "usage-collect.ok").exists()


def test_a_read_only_feeder_lane_counts_as_healthy(tmp_path, monkeypatch):
    """The quota scope reads opencode usage without ever persisting it."""
    monkeypatch.setattr(
        "codex_usage_tracker.collector.collect_opencode_usage",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "codex_usage_tracker.collector.codex_quota_observations",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "codex_usage_tracker.collector.derive_opencode_go_quotas",
        lambda *args, **kwargs: [],
    )
    collect_all(
        **dict(
            _kwargs(tmp_path, tmp_path / "usage.sqlite3"),
            quota_ledger=tmp_path / "quota.sqlite3",
            scope="quota",
            live_quota=False,
        )
    )

    lanes = json.loads((tmp_path / "health" / "lanes-quota.json").read_text(encoding="utf-8"))
    assert lanes["lanes"]["opencode"]["last_success_at"] == lanes["updated_at"]
    assert (tmp_path / "health" / "quota-collect.ok").exists()
    assert not (tmp_path / "health" / "usage-collect.ok").exists()


def test_dry_run_writes_no_lane_health_or_heartbeat(tmp_path, monkeypatch):
    usage = tmp_path / "usage.jsonl"
    _stub_claude(monkeypatch, [])
    collect_all(
        **dict(
            _kwargs(tmp_path, usage),
            quota_ledger=tmp_path / "quota.jsonl",
            dry_run=True,
            strict_sources=False,
        )
    )
    assert not (tmp_path / "health").exists()


def test_lane_health_survives_a_corrupt_previous_file(tmp_path, monkeypatch):
    usage = tmp_path / "usage.sqlite3"
    health = tmp_path / "health"
    health.mkdir()
    (health / "lanes-usage.json").write_text("{ not json", encoding="utf-8")
    _stub_claude(monkeypatch, [_claude_fact(request_status="ok", output_tokens=1)])

    collect_all(**_kwargs(tmp_path, usage))

    lanes = json.loads((health / "lanes-usage.json").read_text(encoding="utf-8"))
    assert lanes["lanes"]["claude_code"]["last_success_at"] == lanes["updated_at"]


def test_summary_line_names_the_degraded_lane():
    from codex_usage_tracker.cli import _collect_summary_line

    line = _collect_summary_line(
        {
            "scope": "usage",
            "lanes": {
                "hermes": {"succeeded": True},
                "claude_code": {"succeeded": False},
                "opencode": {"succeeded": True},
            },
        }
    )
    assert "lanes=2/3" in line and "degraded=claude_code" in line


# --- 1d: bounded /usage probe retry ---------------------------------------


def test_usage_probe_retries_a_timeout_then_succeeds(monkeypatch):
    calls: list[float] = []
    slept: list[float] = []

    def flaky(**kwargs):
        calls.append(kwargs["timeout"])
        if len(calls) < 3:
            raise TimeoutError("Claude Code /usage screen did not become ready")
        return "screen"

    monkeypatch.setattr(quota_module, "capture_claude_code_usage_screen", flaky)
    monkeypatch.setattr(quota_module.time, "sleep", slept.append)

    screen = quota_module.capture_claude_code_usage_screen_with_retry(
        probe_dir="/nonexistent", timeout=45.0, backoff=3.0
    )
    assert screen == "screen"
    assert len(calls) == 3
    assert slept == [3.0, 3.0]


def test_usage_probe_retry_is_bounded_and_reraises(monkeypatch):
    calls: list[int] = []

    def always_timeout(**kwargs):
        calls.append(1)
        raise TimeoutError("Claude Code /usage screen did not become ready")

    monkeypatch.setattr(quota_module, "capture_claude_code_usage_screen", always_timeout)
    monkeypatch.setattr(quota_module.time, "sleep", lambda _: None)

    with pytest.raises(TimeoutError):
        quota_module.capture_claude_code_usage_screen_with_retry(probe_dir="/nonexistent")
    assert len(calls) == quota_module.CLAUDE_USAGE_PROBE_ATTEMPTS


def test_usage_probe_does_not_retry_a_real_fault(monkeypatch):
    calls: list[int] = []

    def unsafe_probe_dir(**kwargs):
        calls.append(1)
        raise ValueError("Claude Code probe directory must be empty")

    monkeypatch.setattr(quota_module, "capture_claude_code_usage_screen", unsafe_probe_dir)
    monkeypatch.setattr(quota_module.time, "sleep", lambda _: None)

    with pytest.raises(ValueError):
        quota_module.capture_claude_code_usage_screen_with_retry(probe_dir="/nonexistent")
    assert len(calls) == 1


def test_collect_claude_code_quota_retries_through_the_public_entrypoint(monkeypatch):
    calls: list[int] = []

    def flaky(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("Claude Code /usage screen did not become ready")
        return (
            "Current session\n"
            "  40% used\n"
            "  Resets Aug 29, 8:00pm (America/New_York)\n"
            "Current week (all models)\n"
            "  12% used\n"
            "  Resets Sep 1, 8:00pm (America/New_York)\n"
        )

    monkeypatch.setattr(quota_module, "capture_claude_code_usage_screen", flaky)
    monkeypatch.setattr(quota_module.time, "sleep", lambda _: None)

    rows = quota_module.collect_claude_code_quota(
        "test:claude-code-quota",
        probe_dir="/nonexistent",
        observed_at="2026-08-29T19:00:00Z",
    )
    assert len(calls) == 2
    assert {row["quota_name"] for row in rows}
