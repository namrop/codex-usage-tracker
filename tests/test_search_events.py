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
import sys

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


# --- collection lane and CLI -------------------------------------------------


def _collect_searches(tmp_path, audit, **overrides):
    from codex_usage_tracker.collector import collect_all

    return collect_all(
        state_db=tmp_path / "unused-state.db",
        opencode_dbs=[],
        codex_ledger=tmp_path / "unused-codex.jsonl",
        usage_ledger=tmp_path / "usage_events.sqlite3",
        quota_ledger=tmp_path / "quota_observations.sqlite3",
        search_ledger=tmp_path / "search_events.sqlite3",
        search_audit=audit,
        scope="searches",
        live_quota=False,
        **overrides,
    )


def test_search_scope_writes_the_ledger_the_quota_row_and_the_watermark(tmp_path):
    audit = tmp_path / "search_audit.jsonl"
    audit.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    result = _collect_searches(tmp_path, audit)
    assert result["searches"]["appended"] == 5
    assert result["searches"]["malformed_lines"] == 1
    assert result["searches"]["derived_quota_observations"] == 1
    # The derived exhaustion lands in the quota ledger, not the search ledger.
    assert result["quotas"]["appended"] == 1
    assert result["lanes"]["hermes_search"]["succeeded"] is True
    assert result["lanes"]["hermes_search_quota"]["succeeded"] is True
    assert result["ceded"] == []

    quota_rows = query_sqlite_facts(
        tmp_path / "quota_observations.sqlite3",
        fact_type="quota_observation_v1",
        limit=10,
    )
    assert [row["provider"] for row in quota_rows] == ["z-ai"]
    assert quota_rows[0]["resets_at"] == "2026-09-24T00:00:00Z"

    watermark = Path(result["paths"]["search_watermark"])
    assert read_watermark(watermark)["next_offset"] == audit.stat().st_size
    assert Path(result["paths"]["search_ledger"]).exists()
    # The searches scope owns its own heartbeat, not usage's or quota's.
    assert [Path(p).name for p in result["paths"]["heartbeats"]] == ["search-collect.ok"]


def test_second_run_over_an_unchanged_feed_appends_nothing(tmp_path):
    audit = tmp_path / "search_audit.jsonl"
    audit.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    _collect_searches(tmp_path, audit)
    again = _collect_searches(tmp_path, audit)
    assert again["searches"]["appended"] == 0
    assert again["searches"]["discovered"] == 0
    assert again["quotas"]["appended"] == 0


def test_a_feed_that_grows_is_read_from_the_watermark(tmp_path):
    audit = tmp_path / "search_audit.jsonl"
    lines = FIXTURE.read_text(encoding="utf-8").splitlines(keepends=True)
    audit.write_text("".join(lines[:2]), encoding="utf-8")
    first = _collect_searches(tmp_path, audit)
    assert first["searches"]["appended"] == 2

    audit.write_text("".join(lines), encoding="utf-8")
    second = _collect_searches(tmp_path, audit)
    assert second["searches"]["appended"] == 3
    assert second["searches"]["derived_quota_observations"] == 1


def test_a_missing_feed_is_an_empty_lane_that_still_succeeds(tmp_path):
    result = _collect_searches(tmp_path, tmp_path / "not_yet_written.jsonl")
    assert result["searches"]["appended"] == 0
    assert result["ceded"] == []
    assert result["lanes"]["hermes_search"]["succeeded"] is True
    # Nothing was read, so nothing is claimed by a watermark either.
    assert "search_watermark" not in result["paths"]


def test_a_dry_run_writes_no_ledger_and_no_watermark(tmp_path):
    audit = tmp_path / "search_audit.jsonl"
    audit.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    result = _collect_searches(tmp_path, audit, dry_run=True)
    assert result["searches"]["discovered"] == 5
    assert not (tmp_path / "search_events.sqlite3").exists()
    assert "search_watermark" not in result["paths"]


def test_the_usage_scope_never_touches_the_search_ledger(tmp_path):
    from codex_usage_tracker.collector import collect_all

    audit = tmp_path / "search_audit.jsonl"
    audit.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    # claude_root is pinned inside tmp_path on purpose: its default is the
    # operator's own ~/.claude/projects, and a test must never read that.
    (tmp_path / "claude" / "projects").mkdir(parents=True)
    result = collect_all(
        state_db=tmp_path / "absent-state.db",
        claude_root=tmp_path / "claude" / "projects",
        opencode_dbs=[],
        codex_ledger=tmp_path / "absent-codex.jsonl",
        usage_ledger=tmp_path / "usage_events.sqlite3",
        quota_ledger=tmp_path / "quota_observations.sqlite3",
        search_ledger=tmp_path / "search_events.sqlite3",
        search_audit=audit,
        scope="usage",
        live_quota=False,
    )
    assert "searches" not in result
    assert not (tmp_path / "search_events.sqlite3").exists()


def test_an_unknown_scope_is_still_refused(tmp_path):
    from codex_usage_tracker.collector import collect_all

    with pytest.raises(ValueError, match="all, usage, quota, searches"):
        collect_all(
            state_db=tmp_path / "state.db",
            opencode_dbs=[],
            codex_ledger=tmp_path / "codex.jsonl",
            usage_ledger=tmp_path / "usage_events.sqlite3",
            quota_ledger=tmp_path / "quota_observations.sqlite3",
            search_ledger=tmp_path / "search_events.sqlite3",
            search_audit=tmp_path / "feed.jsonl",
            scope="search",
            live_quota=False,
        )


def test_search_ledger_may_not_alias_another_canonical_ledger(tmp_path):
    from codex_usage_tracker.collector import collect_all

    shared = tmp_path / "shared.sqlite3"
    with pytest.raises(ValueError, match="distinct paths"):
        collect_all(
            state_db=tmp_path / "state.db",
            opencode_dbs=[],
            codex_ledger=tmp_path / "codex.jsonl",
            usage_ledger=tmp_path / "usage_events.sqlite3",
            quota_ledger=shared,
            search_ledger=shared,
            search_audit=tmp_path / "feed.jsonl",
            scope="searches",
            live_quota=False,
        )


def _run_cli(monkeypatch, *args) -> None:
    monkeypatch.setattr(sys, "argv", ["codex-usage-tracker", *args])


def test_cli_collect_searches_and_searches_round_trip(tmp_path, capsys, monkeypatch):
    from codex_usage_tracker import cli

    audit = tmp_path / "search_audit.jsonl"
    audit.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    ledger = tmp_path / "search_events.sqlite3"
    _run_cli(
        monkeypatch,
        "collect-searches",
        "--search-ledger", str(ledger),
        "--search-audit", str(audit),
        "--quota-ledger", str(tmp_path / "quota_observations.sqlite3"),
        "--usage-ledger", str(tmp_path / "usage_events.sqlite3"),
        "--state-db", str(tmp_path / "state.db"),
    )
    assert cli.main() == 0
    capsys.readouterr()

    _run_cli(monkeypatch, "searches", "--sqlite", str(ledger), "--days", "92")
    assert cli.main() == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["ledger_present"] is True
    assert summary["totals"] == {
        "calls": 5, "rate_limited": 1, "errors": 1, "fallbacks": 2
    }
    backends = {entry["backend"]: entry for entry in summary["backends"]}
    assert backends["zai"]["rate_limited"] == 1
    assert backends["brave"]["fallback_from"] == {"zai": 1}


def test_cli_searches_reports_an_absent_ledger_as_empty_not_failed(
    tmp_path, capsys, monkeypatch
):
    from codex_usage_tracker import cli

    _run_cli(monkeypatch, "searches", "--sqlite", str(tmp_path / "nothing.sqlite3"))
    assert cli.main() == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["ledger_present"] is False
    assert summary["totals"]["calls"] == 0


def test_generic_ledger_verbs_accept_the_new_fact_type():
    from codex_usage_tracker.cli import CANONICAL_FACT_TYPES

    assert "search_event_v1" in CANONICAL_FACT_TYPES


# --- dashboard ---------------------------------------------------------------


def _search_app(tmp_path, *, populate=True):
    from codex_usage_tracker.dashboard import create_app

    ledger = tmp_path / "search_events.sqlite3"
    if populate:
        append_sqlite_facts(
            ledger,
            [project_search_event(row, NAMESPACE) for row in _fixture_rows()[:5]],
            fact_type="search_event_v1",
        )
    app = create_app(
        atrium_root=str(tmp_path), ledger=None, search_ledger=str(ledger)
    )
    app.config.update(TESTING=True)
    return app


def test_api_searches_reports_backends_days_and_rate_limits(tmp_path):
    app = _search_app(tmp_path)
    with app.test_client() as client:
        response = client.get("/api/searches?days=92")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["totals"] == {
        "calls": 5, "rate_limited": 1, "errors": 1, "fallbacks": 2
    }
    backends = {entry["backend"]: entry for entry in payload["backends"]}
    assert backends["zai"]["rate_limited"] == 1
    assert "reset=2026-09-24" in backends["zai"]["last_error_code"]
    assert backends["brave"]["fallback_from"] == {"zai": 1}
    assert payload["generated_at"].endswith("Z")
    # No query text or URL can reach the panel, because none is in the ledger.
    assert "query" not in response.get_data(as_text=True)


def test_api_searches_filters_by_typed_column(tmp_path):
    app = _search_app(tmp_path)
    with app.test_client() as client:
        payload = client.get("/api/searches?days=92&provider=zai").get_json()
    assert payload["totals"]["calls"] == 2
    assert [entry["backend"] for entry in payload["backends"]] == ["zai"]


def test_api_searches_is_404_when_no_ledger_is_configured(tmp_path):
    from codex_usage_tracker.dashboard import create_app

    app = create_app(atrium_root=str(tmp_path), ledger=None, search_ledger=None)
    app.config.update(TESTING=True)
    with app.test_client() as client:
        assert client.get("/api/searches").status_code == 404


def test_api_searches_reads_an_uncollected_ledger_as_an_empty_panel(tmp_path):
    app = _search_app(tmp_path, populate=False)
    with app.test_client() as client:
        response = client.get("/api/searches")
    assert response.status_code == 200
    assert response.get_json()["totals"]["calls"] == 0


def test_derived_exhaustion_does_not_break_the_existing_quota_surfaces(tmp_path):
    """The derived row enters a ledger the quota panels already read.

    window_kind is unknown and limit_value is null on a derived observation —
    shapes the probed z-ai rows never have — so every route that consumes the
    quota ledger is exercised with one in place.
    """
    from codex_usage_tracker.dashboard import create_app

    facts = [project_search_event(row, NAMESPACE) for row in _fixture_rows()[:5]]
    observations = derive_search_quota_observations(facts, NAMESPACE)
    quota_ledger = tmp_path / "quota_observations.sqlite3"
    append_sqlite_facts(quota_ledger, observations, fact_type="quota_observation_v1")

    app = create_app(
        atrium_root=str(tmp_path), ledger=None, quota_ledger=str(quota_ledger)
    )
    app.config.update(TESTING=True)
    with app.test_client() as client:
        for rule in sorted(str(rule) for rule in app.url_map.iter_rules()):
            if "<" in rule:
                continue
            response = client.get(rule)
            if rule == "/api/health":
                # A 503 here is the health answer ("down": no usage ledger is
                # configured in this fixture), not a crash.
                assert response.status_code in (200, 503) and response.get_json()["status"], rule
                continue
            assert response.status_code < 500, rule


def test_cli_searches_reports_whether_the_bounded_query_filled_up(tmp_path, capsys, monkeypatch):
    from codex_usage_tracker import cli

    ledger = tmp_path / "search_events.sqlite3"
    append_sqlite_facts(
        ledger,
        [project_search_event(row, NAMESPACE) for row in _fixture_rows()[:5]],
        fact_type="search_event_v1",
    )
    _run_cli(monkeypatch, "searches", "--sqlite", str(ledger), "--days", "92")
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["truncated"] is False


def test_read_surfaces_never_mint_the_collector_s_ledger(tmp_path, capsys, monkeypatch):
    """A read must not leave a 0-byte SQLite file at the collector's destination."""
    from codex_usage_tracker import cli
    from codex_usage_tracker.dashboard import create_app

    ledger = tmp_path / "search_events.sqlite3"
    _run_cli(monkeypatch, "searches", "--sqlite", str(ledger))
    assert cli.main() == 0
    capsys.readouterr()
    assert not ledger.exists()

    app = create_app(atrium_root=str(tmp_path), ledger=None, search_ledger=str(ledger))
    app.config.update(TESTING=True)
    with app.test_client() as client:
        assert client.get("/api/searches").status_code == 200
    assert not ledger.exists()


def test_month_to_date_counts_by_harness_and_estimates_brave():
    """Keeper msg 1553067845766742228: "brave needs usage tracking too"."""
    from datetime import datetime, timezone

    from codex_usage_tracker.search_events import build_search_month_to_date

    def fact(observed, provider, harness, status="ok"):
        return {"observed_at": observed, "provider": provider, "harness": harness, "status": status}

    facts = [
        fact("2026-08-31T23:59:59Z", "brave", "hermes"),  # last month
        fact("2026-09-11T10:35:00Z", "brave", "hermes"),
        fact("2026-09-12T10:35:00Z", "brave", "hermes", status="rate_limited"),
        fact("2026-09-20T10:35:00Z", "brave", "openclaw"),
        fact("2026-09-21T10:35:00Z", "zai", "hermes"),
    ]
    mtd = build_search_month_to_date(facts, now=datetime(2026, 9, 25, 15, tzinfo=timezone.utc))
    assert mtd["month_start"] == "2026-09-01" and mtd["ledger_since"] == "2026-08-31T23:59:59Z"
    brave, zai = mtd["backends"]
    assert (brave["backend"], brave["calls"], brave["billable_calls"], brave["by_harness"]) == (
        "brave", 3, 2, {"hermes": 2, "openclaw": 1})
    assert (brave["rate_usd"], brave["estimated_usd"]) == ("0.005", "0.01")
    assert zai["calls"] == 1 and "estimated_usd" not in zai
