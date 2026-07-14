from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone

import pytest

import codex_usage_tracker.unified_public_projection as projection_module
from codex_usage_tracker.canonical_ledger import append_sqlite_facts
from codex_usage_tracker.cli import main
from codex_usage_tracker.unified_public_projection import (
    PUBLIC_PROJECTION_KIND,
    build_unified_public_projection,
    validate_unified_public_projection,
    write_unified_public_projection,
)


NOW = datetime(2026, 7, 13, 4, 37, 12, tzinfo=timezone.utc)


def _usage(source_id: str, occurred_at: str, **updates):
    row = {
        "fact_type": "usage_event_v1", "schema_version": 1,
        "source_namespace": "private:source", "source_event_id": source_id,
        "harness": "hermes", "purpose": "main", "record_kind": "api_attempt",
        "occurred_at": occurred_at, "recorded_at": occurred_at,
        "usage_source": "provider_reported", "usage_completeness": "complete",
        "measurement_confidence": "exact", "cost_status": "included",
        "provider": "private-provider", "model_requested": "private-model",
        "input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
        "output_tokens": 0, "reasoning_tokens": 0,
        "x_private_payload": {"prompt": "never publish"},
    }
    row.update(updates)
    return row


def test_unified_projection_has_exact_allowlisted_schema_hourly_zeros_and_token_semantics(tmp_path):
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [
        _usage("exact", "2026-07-13T01:10:00Z", input_tokens=10, cache_read_tokens=20,
               cache_write_tokens=5, output_tokens=7, reasoning_tokens=3),
        _usage("aggregate", "2026-07-13T01:20:00Z", harness="claude", provider="other",
               record_kind="historical_aggregate", measurement_confidence="reconstructed",
               reconstructed_call_count=3, input_tokens=4, output_tokens=2, reasoning_tokens=2),
        _usage("current-partial", "2026-07-13T04:01:00Z", input_tokens=999),
    ], fact_type="usage_event_v1")

    payload = build_unified_public_projection(ledger, hours=3, source="test-public", now=NOW)

    assert set(payload) == {"kind", "schema_version", "source", "generated_at", "bucket_minutes", "rows", "summary"}
    assert payload["kind"] == PUBLIC_PROJECTION_KIND == "namrop_public_usage_projection.v1"
    assert payload["schema_version"] == 1 and payload["bucket_minutes"] == 60
    assert payload["source"] == "test-public"
    assert len(payload["rows"]) == 3
    expected_row_keys = {
        "window_start", "window_end", "input_tokens", "cache_read_tokens",
        "cache_write_tokens", "output_tokens", "reasoning_tokens", "prompt_tokens",
        "total_tokens", "request_attempts", "cache_hit_pct", "measurement_confidence",
    }
    assert all(set(row) == expected_row_keys for row in payload["rows"])
    first = payload["rows"][0]
    assert (first["window_start"], first["window_end"]) == (
        "2026-07-13T01:00:00Z", "2026-07-13T02:00:00Z")
    assert first["prompt_tokens"] == 39
    assert first["total_tokens"] == 48  # reasoning is diagnostic, never added twice
    assert first["reasoning_tokens"] == 5
    assert first["request_attempts"] == 4
    assert first["cache_hit_pct"] == 51.3
    assert first["measurement_confidence"] == "mixed"
    assert payload["rows"][1]["total_tokens"] == 0
    assert payload["rows"][1]["cache_hit_pct"] is None
    assert payload["rows"][1]["measurement_confidence"] == "unknown"
    assert payload["summary"] == {
        "updated_at": payload["generated_at"], "window_count": 3, "total_tokens": 48,
        "request_attempts": 4, "latest_total_tokens": 0, "latest_cache_hit_pct": None,
    }
    serialized = json.dumps(payload)
    for forbidden in ("private-provider", "private-model", "private:source", "never publish", "harness"):
        assert forbidden not in serialized


def test_unified_projection_rejects_non_sqlite_wrong_binding_and_bounds(tmp_path):
    jsonl = tmp_path / "usage.jsonl"
    jsonl.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="SQLite"):
        build_unified_public_projection(jsonl, now=NOW)
    with pytest.raises(ValueError, match="hours"):
        build_unified_public_projection(tmp_path / "missing.sqlite3", hours=169, now=NOW)

    quota = tmp_path / "quota.sqlite3"
    append_sqlite_facts(quota, [], fact_type="quota_observation_v1")
    with pytest.raises(ValueError, match="fact_type"):
        build_unified_public_projection(quota, now=NOW)


def test_projection_validator_rejects_unknown_keys_at_every_level(tmp_path):
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [], fact_type="usage_event_v1")
    payload = build_unified_public_projection(ledger, hours=1, now=NOW)
    validate_unified_public_projection(payload)
    for mutant in (
        {**payload, "provider": "leak"},
        {**payload, "rows": [{**payload["rows"][0], "x_private": "leak"}]},
        {**payload, "summary": {**payload["summary"], "cost_usd": "leak"}},
    ):
        with pytest.raises(ValueError, match="keys"):
            validate_unified_public_projection(mutant)


def test_projection_validator_rejects_path_sources_and_semantically_invalid_allowlisted_data(tmp_path):
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [], fact_type="usage_event_v1")
    payload = build_unified_public_projection(ledger, hours=1, now=NOW)
    with pytest.raises(ValueError, match="source"):
        validate_unified_public_projection({**payload, "source": "/private/path"})
    with pytest.raises(ValueError, match="generated_at"):
        validate_unified_public_projection({**payload, "generated_at": 0})
    bad_row = {**payload["rows"][0], "prompt_tokens": 1}
    with pytest.raises(ValueError, match="prompt_tokens"):
        validate_unified_public_projection({**payload, "rows": [bad_row]})


def test_projection_validator_rejects_inconsistent_summary_and_noncanonical_windows(tmp_path):
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [], fact_type="usage_event_v1")
    payload = build_unified_public_projection(ledger, hours=2, now=NOW)

    with pytest.raises(ValueError, match="summary.total_tokens"):
        validate_unified_public_projection({
            **payload,
            "summary": {**payload["summary"], "total_tokens": 1},
        })
    shifted = {**payload["rows"][0], "window_end": "2026-07-13T03:30:00Z"}
    with pytest.raises(ValueError, match="complete hour"):
        validate_unified_public_projection({**payload, "rows": [shifted, payload["rows"][1]]})


def test_unified_projection_defaults_to_168_complete_hours(tmp_path):
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [], fact_type="usage_event_v1")
    payload = build_unified_public_projection(ledger, now=NOW)
    assert len(payload["rows"]) == 168
    assert payload["rows"][-1]["window_end"] == "2026-07-13T04:00:00Z"


def test_unified_projection_canonicalizes_trailing_zero_generated_at_fraction(tmp_path):
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [], fact_type="usage_event_v1")

    payload = build_unified_public_projection(
        ledger,
        hours=1,
        now=datetime(2026, 7, 13, 4, 37, 12, 120_000, tzinfo=timezone.utc),
    )

    assert payload["generated_at"] == "2026-07-13T04:37:12.12Z"
    assert payload["summary"]["updated_at"] == payload["generated_at"]
    validate_unified_public_projection(payload)


def test_unified_projection_now_default_also_emits_canonical_timestamp(tmp_path, monkeypatch):
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [], fact_type="usage_event_v1")

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 13, 4, 37, 12, 120_000, tzinfo=tz)

    monkeypatch.setattr(projection_module, "datetime", FrozenDateTime)
    payload = build_unified_public_projection(ledger, hours=1)

    assert payload["generated_at"] == "2026-07-13T04:37:12.12Z"
    validate_unified_public_projection(payload)


def test_public_projection_upper_bound_keeps_current_and_future_events_out_of_input_cap(
    tmp_path, monkeypatch
):
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [
        _usage("complete", "2026-07-13T03:30:00Z", input_tokens=7),
        _usage("current", "2026-07-13T04:01:00Z", input_tokens=999),
        _usage("future", "2026-07-14T04:01:00Z", input_tokens=999),
    ], fact_type="usage_event_v1")
    monkeypatch.setattr(projection_module, "_MAX_INPUT_EVENTS", 1)

    payload = build_unified_public_projection(ledger, hours=1, now=NOW)

    assert payload["summary"]["total_tokens"] == 7


def test_public_projection_includes_lower_fraction_and_excludes_upper_fraction(tmp_path):
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [
        _usage("before", "2026-07-13T02:59:59.9Z", input_tokens=100),
        _usage("after-lower", "2026-07-13T03:00:00.1Z", input_tokens=7),
        _usage("before-upper", "2026-07-13T03:59:59.9Z", output_tokens=5),
        _usage("after-upper", "2026-07-13T04:00:00.1Z", input_tokens=1000),
    ], fact_type="usage_event_v1")

    payload = build_unified_public_projection(ledger, hours=1, now=NOW)

    assert payload["rows"][0]["total_tokens"] == 12
    assert payload["rows"][0]["request_attempts"] == 2


def test_unified_projection_atomic_failure_preserves_prior_artifact(tmp_path, monkeypatch):
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [], fact_type="usage_event_v1")
    output = tmp_path / "public.json"
    output.write_text("prior artifact\n", encoding="utf-8")

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("codex_usage_tracker.unified_public_projection.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        write_unified_public_projection(ledger, output, hours=1, now=NOW)
    assert output.read_text(encoding="utf-8") == "prior artifact\n"
    assert not list(tmp_path.glob(".public.json.*.tmp"))


def test_unified_projection_fsyncs_containing_directory_after_replace(tmp_path, monkeypatch):
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [], fact_type="usage_event_v1")
    output = tmp_path / "public.json"
    fsync_targets = []
    original_fsync = os.fsync

    def tracking_fsync(descriptor):
        fsync_targets.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        return original_fsync(descriptor)

    monkeypatch.setattr(projection_module.os, "fsync", tracking_fsync)
    write_unified_public_projection(ledger, output, hours=1, now=NOW)

    assert fsync_targets == ["file", "directory"]
    assert output.exists()


def test_unified_projection_refuses_to_replace_its_private_input(tmp_path):
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [], fact_type="usage_event_v1")
    before = ledger.read_bytes()
    with pytest.raises(ValueError, match="same file"):
        write_unified_public_projection(ledger, ledger, hours=1, now=NOW)
    assert ledger.read_bytes() == before


def test_unified_projection_refuses_hard_link_alias_of_private_input(tmp_path):
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [], fact_type="usage_event_v1")
    alias = tmp_path / "public.json"
    alias.hardlink_to(ledger)
    before = ledger.read_bytes()
    with pytest.raises(ValueError, match="same file"):
        write_unified_public_projection(ledger, alias, hours=1, now=NOW)
    assert ledger.read_bytes() == before


def test_write_public_usage_projection_cli_uses_separate_command(tmp_path, monkeypatch, capsys):
    captured = {}

    def fake_writer(ledger_path, projection_path, **kwargs):
        captured.update(ledger_path=ledger_path, projection_path=projection_path, **kwargs)
        return projection_path

    monkeypatch.setattr("codex_usage_tracker.cli.write_unified_public_projection", fake_writer)
    monkeypatch.setattr("sys.argv", [
        "tracker", "write-public-usage-projection", "--usage-ledger", "/private/usage.sqlite3",
        "--public-projection", str(tmp_path / "public.json"), "--hours", "24",
        "--public-projection-source", "namrop-test",
    ])
    assert main() == 0
    assert captured == {
        "ledger_path": "/private/usage.sqlite3", "projection_path": str(tmp_path / "public.json"),
        "hours": 24, "source": "namrop-test",
    }
    assert capsys.readouterr().out.strip() == f"public_projection: {tmp_path / 'public.json'}"
