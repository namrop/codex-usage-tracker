"""Web search/extract accounting: contract, incremental read, derived quota, summary.

Keeper ruling 2026-09-10 ("the ai usage ledger should start tracking searches
as well in a new table"). The fixture beside this file is a realistic slice of
the Hermes audit feed: two healthy calls, the Z.ai weekly exhaustion that
stopped the masthead composer, the Brave fallback that covered it, a failed
keyless retry, and one line that is not a search event at all.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from codex_usage_tracker.canonical_ledger import (
    ValidationError,
    append_sqlite_facts,
    audit_sqlite_ledger,
    canonical_json,
    export_sqlite_to_jsonl,
    migrate_jsonl_to_sqlite,
    normalize_fact,
    query_sqlite_facts,
)
from codex_usage_tracker.search_events import (
    build_search_summary,
    collect_hermes_search_events,
    derive_search_quota_observations,
    project_search_event,
    read_watermark,
    search_event_identity,
    write_watermark,
)

FIXTURE = Path(__file__).parent / "fixtures" / "search_audit_sample.jsonl"
NAMESPACE = "sol:hermes-search"


def _fixture_rows() -> list[dict]:
    rows = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _drain(generator):
    """Materialize a lane generator, keeping its StopIteration diagnostics."""
    rows = []
    while True:
        try:
            rows.append(next(generator))
        except StopIteration as stop:
            return rows, (stop.value if isinstance(stop.value, dict) else {})


def test_fixture_projects_onto_the_contract():
    facts = [project_search_event(raw, NAMESPACE) for raw in _fixture_rows()[:5]]
    assert [fact["provider"] for fact in facts] == ["zai", "firecrawl", "zai", "brave", "keyless"]
    assert [fact["purpose"] for fact in facts] == [
        "web_search", "web_extract", "web_search", "web_search", "web_search"
    ]
    assert [fact["status"] for fact in facts] == ["ok", "ok", "rate_limited", "ok", "error"]
    # Local offsets are canonicalized to UTC before anything is named by them.
    assert facts[0]["observed_at"] == "2026-09-09T18:02:11Z"
    assert all(fact["fact_type"] == "search_event_v1" for fact in facts)
    assert all(fact["harness"] == "hermes" for fact in facts)
    assert facts[3]["fallback_from"] == "zai"
    assert facts[1]["query_sha256"] is None and facts[1]["url_count"] == 3


def test_no_query_text_or_url_can_enter_the_contract():
    """A feed that starts emitting query text must not carry it into canon.

    The projection names every field it copies, so a wider audit row is
    narrowed rather than passed through: the contract cannot leak a field it
    does not know about, and the canonical bytes never mention one.
    """
    raw = dict(_fixture_rows()[0], query="how to plant coral", urls=["https://example.test"])
    fact = project_search_event(raw, NAMESPACE)
    assert "query" not in fact and "urls" not in fact
    payload = canonical_json(fact)
    assert "coral" not in payload and "example.test" not in payload
    # And the contract itself refuses an unknown key handed to it directly.
    with pytest.raises(ValidationError):
        normalize_fact({**fact, "query": "how to plant coral"})


def test_rows_that_are_not_search_events_are_rejected():
    with pytest.raises(ValueError):
        project_search_event({"not": "a search event"}, NAMESPACE)
    with pytest.raises(ValueError):
        project_search_event(dict(_fixture_rows()[0], schema="search_event.v2"), NAMESPACE)
    with pytest.raises(ValueError):
        project_search_event(dict(_fixture_rows()[0], status="throttled"), NAMESPACE)


def test_identity_is_stable_and_offset_independent():
    raw = _fixture_rows()[0]
    first = project_search_event(raw, NAMESPACE)["source_search_event_id"]
    # The same instant re-emitted in another offset is the same event.
    same_instant = dict(raw, observed_at="2026-09-09T18:02:11+00:00")
    assert project_search_event(same_instant, NAMESPACE)["source_search_event_id"] == first
    assert first == search_event_identity("2026-09-09T18:02:11Z", "sess-a", "1" * 64, "web_search")
    # A different tool at the same instant is a different event.
    assert project_search_event(dict(raw, tool="web_extract"), NAMESPACE)[
        "source_search_event_id"
    ] != first


def test_incremental_read_resumes_and_is_replay_safe(tmp_path):
    audit = tmp_path / "search_audit.jsonl"
    lines = FIXTURE.read_text(encoding="utf-8").splitlines(keepends=True)
    audit.write_text("".join(lines[:2]), encoding="utf-8")

    first, first_diag = _drain(collect_hermes_search_events(audit, NAMESPACE))
    assert len(first) == 2
    assert first_diag["next_offset"] == audit.stat().st_size
    assert first_diag["malformed_lines"] == 0

    audit.write_text("".join(lines), encoding="utf-8")
    second, second_diag = _drain(
        collect_hermes_search_events(audit, NAMESPACE, watermark=first_diag)
    )
    # Only the new lines, and the non-event line counted rather than fatal.
    assert len(second) == 3
    assert second_diag["malformed_lines"] == 1
    assert second_diag["next_offset"] == audit.stat().st_size

    replayed, _ = _drain(collect_hermes_search_events(audit, NAMESPACE))
    assert len(replayed) == 5
    identities = {fact["source_search_event_id"] for fact in replayed}
    assert identities == {fact["source_search_event_id"] for fact in first + second}


def test_missing_feed_is_an_empty_lane_not_an_error(tmp_path):
    rows, diagnostics = _drain(
        collect_hermes_search_events(tmp_path / "absent.jsonl", NAMESPACE)
    )
    assert rows == []
    assert diagnostics["next_offset"] == 0 and diagnostics["inode"] is None


def test_torn_tail_is_deferred_to_the_next_pass(tmp_path):
    audit = tmp_path / "search_audit.jsonl"
    lines = FIXTURE.read_text(encoding="utf-8").splitlines(keepends=True)
    audit.write_text(lines[0] + lines[1][:40], encoding="utf-8")
    rows, diagnostics = _drain(collect_hermes_search_events(audit, NAMESPACE))
    assert len(rows) == 1
    assert diagnostics["next_offset"] == len(lines[0].encode("utf-8"))
    assert diagnostics["malformed_lines"] == 0

    audit.write_text("".join(lines[:2]), encoding="utf-8")
    rest, _ = _drain(collect_hermes_search_events(audit, NAMESPACE, watermark=diagnostics))
    assert len(rest) == 1


def test_rotation_restarts_from_zero(tmp_path):
    audit = tmp_path / "search_audit.jsonl"
    lines = FIXTURE.read_text(encoding="utf-8").splitlines(keepends=True)
    audit.write_text("".join(lines[:3]), encoding="utf-8")
    _, diagnostics = _drain(collect_hermes_search_events(audit, NAMESPACE))
    audit.unlink()
    audit.write_text(lines[0], encoding="utf-8")
    rows, rotated = _drain(
        collect_hermes_search_events(audit, NAMESPACE, watermark=diagnostics)
    )
    assert len(rows) == 1
    assert rotated["next_offset"] == audit.stat().st_size


def test_watermark_roundtrip_is_private_and_atomic(tmp_path):
    target = tmp_path / "state" / "search.watermark.json"
    write_watermark(target, {"next_offset": 512, "inode": 7, "size": 512})
    assert read_watermark(target)["next_offset"] == 512
    assert (target.stat().st_mode & 0o777) == 0o600
    target.write_text("{ not json", encoding="utf-8")
    assert read_watermark(target) == {}


def test_rate_limited_event_naming_a_reset_derives_a_quota_observation():
    raw = _fixture_rows()
    facts = [project_search_event(row, NAMESPACE) for row in raw[:5]]
    observations = derive_search_quota_observations(facts, NAMESPACE, raw_rows=raw[:5])
    assert len(observations) == 1
    row = observations[0]
    # Filed against the provider name the live z-ai quota probe already uses,
    # so the exhaustion joins that provider's series instead of a new one.
    assert row["provider"] == "z-ai"
    assert row["quota_name"] == "web_search_limit"
    assert row["remaining_value"] == "0"
    assert row["resets_at"] == "2026-09-24T00:00:00Z"
    assert row["measurement_confidence"] == "estimated"
    assert row["x_search_backend"] == "zai"
    # Re-derivation is idempotent: same identity, same canonical bytes.
    again = derive_search_quota_observations(facts, NAMESPACE, raw_rows=raw[:5])
    assert canonical_json(again[0]) == canonical_json(row)


def test_rate_limited_event_without_a_reset_derives_nothing():
    raw = dict(_fixture_rows()[2], error_code="rate_limited")
    facts = [project_search_event(raw, NAMESPACE)]
    assert derive_search_quota_observations(facts, NAMESPACE, raw_rows=[raw]) == []


def test_summary_counts_calls_rate_limits_and_fallbacks():
    facts = [project_search_event(row, NAMESPACE) for row in _fixture_rows()[:5]]
    summary = build_search_summary(
        facts, days=7, now=datetime(2026, 9, 11, 3, 0, tzinfo=timezone.utc)
    )
    assert summary["totals"] == {
        "calls": 5, "rate_limited": 1, "errors": 1, "fallbacks": 2
    }
    backends = {entry["backend"]: entry for entry in summary["backends"]}
    assert set(backends) == {"zai", "firecrawl", "brave", "keyless"}
    assert backends["zai"]["calls"] == 2 and backends["zai"]["rate_limited"] == 1
    assert backends["zai"]["last_rate_limited_at"] == "2026-09-11T01:14:58Z"
    assert "reset=2026-09-24" in backends["zai"]["last_error_code"]
    assert backends["brave"]["fallback_from"] == {"zai": 1}
    days = {entry["day"] for entry in summary["daily"]}
    assert days == {"2026-09-09", "2026-09-11"}


def test_summary_window_excludes_facts_outside_it():
    facts = [project_search_event(row, NAMESPACE) for row in _fixture_rows()[:5]]
    summary = build_search_summary(
        facts, days=1, now=datetime(2026, 9, 11, 3, 0, tzinfo=timezone.utc)
    )
    assert summary["totals"]["calls"] == 3
    assert summary["window"] == {"start_day": "2026-09-11", "end_day": "2026-09-11"}


def test_search_ledger_is_a_type_bound_sqlite_ledger(tmp_path):
    """The new table is the ordinary facts shape, and the generic verbs see it."""
    ledger = tmp_path / "search_events.sqlite3"
    facts = [project_search_event(row, NAMESPACE) for row in _fixture_rows()[:5]]
    result = append_sqlite_facts(ledger, facts, fact_type="search_event_v1")
    assert (result.discovered, result.appended, result.replayed) == (5, 5, 0)
    # Re-ingest of the same feed slice appends nothing.
    replay = append_sqlite_facts(ledger, facts, fact_type="search_event_v1")
    assert (replay.appended, replay.replayed) == (0, 5)

    assert audit_sqlite_ledger(ledger, fact_type="search_event_v1") == 5
    stored = query_sqlite_facts(ledger, fact_type="search_event_v1", limit=10)
    assert {row["provider"] for row in stored} == {"zai", "firecrawl", "brave", "keyless"}

    exported = tmp_path / "search_events.jsonl"
    assert export_sqlite_to_jsonl(ledger, exported, fact_type="search_event_v1") == 5
    round_trip = tmp_path / "round_trip.sqlite3"
    migrated = migrate_jsonl_to_sqlite(
        exported, round_trip, fact_type="search_event_v1"
    )
    assert migrated.appended == 5


def test_search_ledger_refuses_a_foreign_fact_type(tmp_path):
    ledger = tmp_path / "search_events.sqlite3"
    facts = [project_search_event(_fixture_rows()[0], NAMESPACE)]
    append_sqlite_facts(ledger, facts, fact_type="search_event_v1")
    with pytest.raises(ValidationError):
        append_sqlite_facts(ledger, [], fact_type="usage_event_v1")
