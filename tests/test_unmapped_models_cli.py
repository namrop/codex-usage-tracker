"""``unmapped-models``: a bounded, read-only private inventory of the
provider/model IDs the canonical ledger carries that no reviewed public
mapping and/or no list-price table covers.

Added 2026-09-06 after gpt-6-astra, claude-fable-5-1, glm-5.3 and
glm-5.3-flash arrived in the ledger and silently disappeared into Other
(public projection) and into the provider default (pricing) until someone
traced them by hand. This command is visibility only: it prints raw private
IDs to the operator's terminal and never writes anything, never publishes a
raw name, and never adds anything to an allowlist.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import pytest

from codex_usage_tracker import cli
from codex_usage_tracker.canonical_ledger import append_sqlite_facts


NOW = datetime(2026, 7, 13, 4, 37, 12, tzinfo=timezone.utc)


def _usage(source_id: str, occurred_at: str, **updates):
    row = {
        "fact_type": "usage_event_v1", "schema_version": 1,
        "source_namespace": "private:source", "source_event_id": source_id,
        "harness": "hermes", "purpose": "main", "record_kind": "api_attempt",
        "occurred_at": occurred_at, "recorded_at": occurred_at,
        "usage_source": "provider_reported", "usage_completeness": "complete",
        "measurement_confidence": "exact", "cost_status": "included",
        "provider": "anthropic", "model_requested": "claude-opus-5",
        "input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
        "output_tokens": 0, "reasoning_tokens": 0,
    }
    row.update(updates)
    return row


def _ledger(tmp_path, facts):
    path = tmp_path / "usage.sqlite3"
    append_sqlite_facts(path, facts, fact_type="usage_event_v1")
    return path


def _argv(monkeypatch, *args) -> None:
    monkeypatch.setattr(sys, "argv", ["codex-usage-tracker", "unmapped-models", *args])


def _run(capsys, monkeypatch, *args) -> dict:
    _argv(monkeypatch, *args)
    assert cli.main() == 0
    return json.loads(capsys.readouterr().out)


def test_unmapped_models_pushes_window_into_integrity_checked_online_query(
    tmp_path, capsys, monkeypatch
):
    from codex_usage_tracker import canonical_ledger

    ledger = _ledger(tmp_path, [
        _usage("old", "2025-01-01T03:10:00Z", provider="unknown", model_requested="old"),
        _usage("recent", "2026-07-13T03:10:00Z", provider="unknown", model_requested="recent"),
    ])
    calls = []
    original = canonical_ledger.query_sqlite_facts

    def record_query(path, **kwargs):
        calls.append(kwargs)
        return original(path, **kwargs)

    monkeypatch.setattr(canonical_ledger, "query_sqlite_facts", record_query)
    result = _run(capsys, monkeypatch, "--sqlite", str(ledger), "--hours", "24")
    assert calls[0]["limit"] == 1
    assert calls[1]["occurred_or_observed_at_gte"] == "2026-07-12T03:10:00Z"
    # Same online integrity reader as the dashboard; not a full schema audit.
    assert calls[1]["contract_validation"] is False
    assert result["counts"]["scanned_events"] == 1
    assert result["unmapped"][0]["model"] == "recent"


def test_unmapped_models_reports_unpublishable_and_unpriced_ids_with_counts(
    tmp_path, capsys, monkeypatch
):
    ledger = _ledger(tmp_path, [
        # Fully covered: reviewed public label and a list price.
        _usage("known", "2026-07-13T03:10:00Z", provider="openai-codex",
               model_requested="gpt-6-astra", input_tokens=100, output_tokens=10),
        _usage("known-2", "2026-07-13T03:11:00Z", provider="openai-codex",
               model_requested="gpt-6-astra", input_tokens=5),
        # Recognized provider, brand new model: no public label, no price.
        _usage("new-model", "2026-07-13T03:12:00Z", provider="openai-codex",
               model_requested="gpt-7-nova", input_tokens=40, output_tokens=2),
        # Unknown provider entirely.
        _usage("new-provider", "2026-07-13T03:13:00Z", provider="mystery-lab",
               model_requested="whoknows", input_tokens=7),
    ])

    payload = _run(capsys, monkeypatch, "--sqlite", str(ledger), "--hours", "24")

    entries = {(row["provider"], row["model"]): row for row in payload["unmapped"]}
    assert ("openai-codex", "gpt-6-astra") not in entries
    nova = entries[("openai-codex", "gpt-7-nova")]
    assert nova["events"] == 1
    assert nova["total_tokens"] == 42
    assert nova["public_label"] is None
    assert nova["list_priced"] is False
    mystery = entries[("mystery-lab", "whoknows")]
    assert mystery["events"] == 1
    assert mystery["public_provider_label"] == "Other"
    assert mystery["list_priced"] is False
    assert payload["counts"]["unmapped_pairs"] == 2
    assert payload["counts"]["scanned_pairs"] == 3
    # The window is anchored on the newest fact in the ledger, not on wall
    # clock, so a stalled collector yields a report instead of silence.
    assert payload["window"] == {
        "hours": 24,
        "start": "2026-07-12T03:13:00Z",
        "end": "2026-07-13T03:13:00Z",
    }


def test_unmapped_models_flags_a_priced_model_that_has_no_public_label(
    tmp_path, capsys, monkeypatch
):
    """The two allowlists are separate: a model can be priced and still be
    invisible on the public projection. Both gaps must be reported."""
    ledger = _ledger(tmp_path, [
        _usage("priced-unpublished", "2026-07-13T03:10:00Z", provider="anthropic",
               model_requested="claude-99-experimental", input_tokens=11),
    ])

    payload = _run(capsys, monkeypatch, "--sqlite", str(ledger), "--hours", "24")

    entry = payload["unmapped"][0]
    assert entry["model"] == "claude-99-experimental"
    assert entry["public_label"] is None
    # anthropic keeps a documented provider default, so it IS priced.
    assert entry["list_priced"] is True
    assert entry["public_provider_label"] == "Anthropic"


def test_unmapped_models_prefers_model_reported_and_is_ordered_by_volume(
    tmp_path, capsys, monkeypatch
):
    ledger = _ledger(tmp_path, [
        _usage("substituted", "2026-07-13T03:10:00Z", provider="openrouter",
               model_requested="some-router-alias", model_reported="unknown-lab/thing-9",
               input_tokens=500),
        _usage("smaller", "2026-07-13T03:11:00Z", provider="openrouter",
               model_requested="another-alias", model_reported="unknown-lab/thing-1",
               input_tokens=5),
    ])

    payload = _run(capsys, monkeypatch, "--sqlite", str(ledger), "--hours", "24")

    assert [row["model"] for row in payload["unmapped"]] == [
        "unknown-lab/thing-9", "unknown-lab/thing-1",
    ]
    assert "some-router-alias" not in json.dumps(payload)


def test_unmapped_models_is_read_only_and_bounded(tmp_path, capsys, monkeypatch):
    ledger = _ledger(tmp_path, [
        _usage("known", "2026-07-13T03:10:00Z", provider="zai",
               model_requested="glm-5.3", input_tokens=3),
    ])
    before = ledger.read_bytes()

    def artifacts():
        # SQLite's own WAL/shm/lock sidecars are connection bookkeeping, not
        # output; everything else would be a file this command produced.
        return sorted(
            path.name
            for path in tmp_path.iterdir()
            if not path.name.endswith(("-shm", "-wal", ".lock"))
        )

    before_artifacts = artifacts()
    payload = _run(capsys, monkeypatch, "--sqlite", str(ledger), "--hours", "24")

    assert payload["unmapped"] == []
    assert payload["counts"]["unmapped_pairs"] == 0
    assert payload["scan_truncated"] is False
    # The ledger is byte-identical and no report artifact was written.
    assert ledger.read_bytes() == before
    assert artifacts() == before_artifacts == ["usage.sqlite3"]

    # The hours window is bounded exactly like the public projection command.
    for bad_hours in ("0", "100000"):
        _argv(monkeypatch, "--sqlite", str(ledger), "--hours", bad_hours)
        with pytest.raises(SystemExit):
            cli.main()


def test_unmapped_models_never_writes_a_raw_name_into_the_public_projection(
    tmp_path, capsys, monkeypatch
):
    """Visibility, not automation: seeing an unmapped ID must not make it
    publishable. The public projection still refuses it."""
    from codex_usage_tracker.unified_public_projection import (
        build_unified_public_projection,
    )

    ledger = _ledger(tmp_path, [
        _usage("new", "2026-07-13T03:10:00Z", provider="openai-codex",
               model_requested="gpt-7-nova", input_tokens=1000),
    ])
    payload = _run(capsys, monkeypatch, "--sqlite", str(ledger), "--hours", "24")
    assert payload["unmapped"][0]["model"] == "gpt-7-nova"

    projection = build_unified_public_projection(ledger, hours=2, now=NOW)
    assert "gpt-7-nova" not in json.dumps(projection)
    assert [row["model_label"] for row in projection["model_rows"]] == ["Other"]
