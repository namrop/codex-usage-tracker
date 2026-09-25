"""Contract v2 physical-request identity (keeper ratified 2026-09-25).

Fixtures named in the v2 proposal (Atrium 01a0b287-a7cb-7ccb-9f7b-de85e4558e93,
landing step 3): fork copy counted once; distinct requests stay distinct;
conflicting payloads never merged; id-less rows never collapse; earliest
observation wins (attribution stays on the thread that ran the call);
cross-harness same id; multi-hop row. Every SQLite assertion is paired with
the Python mirror so the rule cannot drift between the two implementations.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from codex_usage_tracker.canonical_ledger import (
    IDENTITY_RULE,
    append_sqlite_facts,
    elect_physical_requests,
    physical_request_key,
    query_sqlite_physical_requests,
)
from codex_usage_tracker.unified_public_projection import build_unified_public_projection

WINDOW = ("2026-09-24T00:00:00Z", "2026-09-25T00:00:00Z")


def _claude(session: str, msg: str, occurred_at: str, *, namespace: str = "sol:claude-code", **updates):
    row = {
        "fact_type": "usage_event_v1", "schema_version": 1,
        "source_namespace": namespace, "source_event_id": f"{session}:{msg}",
        "harness": "claude_code", "purpose": "main", "record_kind": "api_attempt",
        "occurred_at": occurred_at, "recorded_at": occurred_at,
        "usage_source": "provider_reported", "usage_completeness": "complete",
        "measurement_confidence": "exact", "cost_status": "included",
        "provider": "anthropic", "model_requested": "claude-opus-5-5", "model_reported": "claude-opus-5-5",
        "session_id": session,
        "input_tokens": 2, "cache_read_tokens": 1000, "cache_write_tokens": 0,
        "output_tokens": 50, "reasoning_tokens": 0,
    }
    row.update(updates)
    return row


def _hermes(event: str, occurred_at: str, **updates):
    row = {
        "fact_type": "usage_event_v1", "schema_version": 1,
        "source_namespace": "sol:hermes", "source_event_id": event,
        "harness": "hermes", "purpose": "main", "record_kind": "api_attempt",
        "occurred_at": occurred_at, "recorded_at": occurred_at,
        "usage_source": "provider_reported", "usage_completeness": "unknown",
        "measurement_confidence": "exact", "cost_status": "included",
        "provider": "custom:meridian-yugen", "model_requested": "claude-opus-5-5",
        "input_tokens": 2, "cache_read_tokens": 1000, "cache_write_tokens": 0,
        "output_tokens": 50, "reasoning_tokens": 0,
    }
    row.update(updates)
    return row


def _both(tmp_path, facts, **query):
    """Elect via SQLite and via the Python mirror; assert they agree."""
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, facts, fact_type="usage_event_v1")
    rows, stats = query_sqlite_physical_requests(
        ledger, occurred_or_observed_at_gte=WINDOW[0], occurred_or_observed_at_lt=WINDOW[1], **query
    )
    mirror, mirror_stats = elect_physical_requests(facts)
    for field, expected in (query.get("filters") or {}).items():
        mirror = [row for row in mirror if row.get(field) == expected]
    ids = lambda items: sorted((row["source_namespace"], row["source_event_id"]) for row in items)
    assert ids(rows) == ids(mirror)
    assert stats == mirror_stats
    return rows, stats


def test_fork_copy_is_counted_once_and_original_keeps_attribution(tmp_path):
    original = _claude("aaaa-original", "msg_01A", "2026-09-24T10:00:00Z")
    copies = [_claude(f"fork-{index}", "msg_01A", "2026-09-24T10:00:00Z") for index in range(5)]
    rows, stats = _both(tmp_path, [original, *copies])
    assert [row["session_id"] for row in rows] == ["aaaa-original"]
    assert stats["observations"] == 6 and stats["physical_requests"] == 1
    assert stats["copies_suppressed"] == 5 and stats["payload_conflicts"] == 0
    assert stats["identity_rule"] == IDENTITY_RULE == "v2.1"


def test_fork_copy_ingested_first_still_loses_to_nothing_but_ingestion_order(tmp_path):
    # Copies carry identical timestamps; with a tie the first INGESTED wins,
    # which in practice is the original because forks come later.
    first = _claude("zzzz-first-ingested", "msg_01B", "2026-09-24T10:00:00Z")
    later = _claude("aaaa-later", "msg_01B", "2026-09-24T10:00:00Z")
    rows, _ = _both(tmp_path, [first, later])
    assert [row["session_id"] for row in rows] == ["zzzz-first-ingested"]


def test_distinct_requests_stay_distinct(tmp_path):
    facts = [
        _claude("s1", "msg_01C", "2026-09-24T10:00:00Z"),
        _claude("s1", "msg_01D", "2026-09-24T10:00:00Z"),
        _claude("s2", "msg_01E", "2026-09-24T11:00:00Z"),
    ]
    rows, stats = _both(tmp_path, facts)
    assert len(rows) == 3 and stats["copies_suppressed"] == 0


def test_conflicting_payloads_are_counted_never_merged(tmp_path):
    original = _claude("s1", "msg_01F", "2026-09-24T10:00:00Z", output_tokens=50)
    disagreeing = _claude("s2", "msg_01F", "2026-09-24T10:00:00Z", output_tokens=51)
    rows, stats = _both(tmp_path, [original, disagreeing])
    assert len(rows) == 1 and rows[0]["output_tokens"] == 50
    assert stats["payload_conflicts"] == 1


def test_idless_rows_never_collapse_even_when_identical(tmp_path):
    # Not a relayed route: id-less rows on a direct provider are their own requests.
    facts = [_hermes(f"evt-{index}", "2026-09-24T10:00:00Z", provider="zai") for index in range(3)]
    rows, stats = _both(tmp_path, facts)
    assert len(rows) == 3
    assert stats["harness_only_requests"] == 3 and stats["copies_suppressed"] == 0


def test_cross_harness_multi_hop_same_request_counts_once(tmp_path):
    # Hermes -> Meridian -> Anthropic: the outer hop carries upstream_provider
    # and the provider response id, so it joins the SDK receipt's key.
    # The caller's observation wins even when it is stamped LATER than the
    # relay's receipt: it carries the session attribution; the SDK session id
    # is Meridian plumbing.
    hermes = _hermes("evt-1", "2026-09-24T10:00:02Z",
                     provider_request_id="msg_01G", upstream_provider="anthropic")
    sdk = _claude("sdk-session", "msg_01G", "2026-09-24T10:00:01Z")
    assert physical_request_key(hermes) == physical_request_key(sdk) == "req|anthropic|msg_01G"
    rows, stats = _both(tmp_path, [sdk, hermes])
    assert len(rows) == 1 and rows[0]["harness"] == "hermes"
    assert stats["observations"] == 2 and stats["physical_requests"] == 1
    assert stats["cross_harness_requests"] == 1
    # Two harnesses stamping one call a second apart is not a copy disagreeing.
    assert stats["timestamp_disagreements"] == 0 and stats["payload_conflicts"] == 0


def test_copy_timestamp_split_within_one_source_is_flagged(tmp_path):
    first = _claude("s1", "msg_01T", "2026-09-24T10:00:00Z")
    moved = _claude("s2", "msg_01T", "2026-09-24T10:00:05Z")
    _, stats = _both(tmp_path, [first, moved])
    assert stats["timestamp_disagreements"] == 1 and stats["cross_harness_requests"] == 0


def test_reasoning_difference_is_not_a_payload_conflict(tmp_path):
    # Anthropic reports thinking inside output_tokens; Hermes records the
    # diagnostic reasoning bucket as 0, the Claude collector as thinking_tokens.
    hermes = _hermes("evt-r", "2026-09-24T10:00:02Z", provider_request_id="msg_01R",
                     upstream_provider="anthropic", reasoning_tokens=0)
    sdk = _claude("sdk", "msg_01R", "2026-09-24T10:00:01Z", reasoning_tokens=30)
    _, stats = _both(tmp_path, [sdk, hermes])
    assert stats["payload_conflicts"] == 0


def test_cross_harness_token_disagreement_is_counted(tmp_path):
    hermes = _hermes("evt-c", "2026-09-24T10:00:02Z", provider_request_id="msg_01C2",
                     upstream_provider="anthropic", cache_write_tokens=7)
    sdk = _claude("sdk", "msg_01C2", "2026-09-24T10:00:01Z")
    rows, stats = _both(tmp_path, [sdk, hermes])
    assert len(rows) == 1 and stats["payload_conflicts"] == 1


def test_outer_hop_without_upstream_provider_does_not_guess_a_join(tmp_path):
    hermes = _hermes("evt-2", "2026-09-24T10:00:00Z", provider_request_id="msg_01H")
    sdk = _claude("sdk-session", "msg_01H", "2026-09-24T10:00:00Z")
    rows, _ = _both(tmp_path, [hermes, sdk])
    assert len(rows) == 2  # over, never under: no inferred marking


def test_provider_reported_usage_outranks_estimates(tmp_path):
    estimate = _hermes("evt-est", "2026-09-24T09:00:00Z", provider_request_id="resp_1",
                       provider="openai", usage_source="estimated", measurement_confidence="reconstructed")
    receipt = _hermes("evt-rcpt", "2026-09-24T09:30:00Z", provider_request_id="resp_1", provider="openai")
    rows, _ = _both(tmp_path, [estimate, receipt])
    assert [row["source_event_id"] for row in rows] == ["evt-rcpt"]


def test_filters_apply_after_election_so_partitions_stay_additive(tmp_path):
    sdk = _claude("sdk", "msg_01I", "2026-09-24T10:00:00Z")
    hermes = _hermes("evt-3", "2026-09-24T10:00:01Z", provider_request_id="msg_01I", upstream_provider="anthropic")
    solo = _hermes("evt-4", "2026-09-24T11:00:00Z", provider="zai")
    facts = [sdk, hermes, solo]
    total, _ = _both(tmp_path / "a", facts)
    claude_only, _ = _both(tmp_path / "b", facts, filters={"harness": "claude_code"})
    hermes_only, _ = _both(tmp_path / "c", facts, filters={"harness": "hermes"})
    assert len(total) == len(claude_only) + len(hermes_only) == 2
    # The relayed call is elected on its caller's row, so it partitions under
    # hermes, and the claude_code view does not count it a second time.
    assert claude_only == []
    assert [row["source_event_id"] for row in hermes_only] == ["evt-3", "evt-4"]


def test_limit_bounds_elected_requests_not_observations(tmp_path):
    facts = [_claude(f"fork-{index}", "msg_01J", "2026-09-24T10:00:00Z") for index in range(10)]
    rows, stats = _both(tmp_path, facts, limit=1)
    assert len(rows) == 1 and stats["observations"] == 10


@pytest.mark.parametrize("tmp_subdir", ["projection"])
def test_public_projection_counts_physical_requests(tmp_path, tmp_subdir):
    ledger = tmp_path / tmp_subdir / "usage.sqlite3"
    facts = [_claude(f"fork-{index}", "msg_01K", "2026-09-24T10:15:00Z") for index in range(4)]
    append_sqlite_facts(ledger, facts, fact_type="usage_event_v1")
    now = datetime(2026, 9, 24, 12, 5, tzinfo=timezone.utc)
    payload = build_unified_public_projection(ledger, hours=3, source="test-public", now=now)
    assert payload["summary"]["total_tokens"] == 2 + 1000 + 50
    assert payload["summary"]["request_attempts"] == 1


# ── v2.1 (keeper, msgs 1552986099343818813 / 1552992232733413420): a Hermes
# call relayed through Meridian counts only through Anthropic's id; without
# it, the Claude SDK receipt of the same call is what counts. ──

from codex_usage_tracker.canonical_ledger import RELAY_BARE_CUSTOM_WINDOW, is_unkeyed_relay


@pytest.mark.parametrize("updates", [
    {"provider": "custom:meridian-primary"},
    {"provider": "custom:meridian-yugen"},
    {"provider": "custom"},  # inside the audited bare-custom window
    {"provider": "custom", "occurred_at": "2026-09-25T12:30:00Z", "x_relay_route": "meridian-yugen"},
])
def test_an_idless_relayed_hermes_row_is_never_a_request(tmp_path, updates):
    moment = updates.pop("occurred_at", "2026-09-24T10:00:02Z")
    window = ("2026-09-24T00:00:00Z", "2026-09-26T00:00:00Z")
    hermes = _hermes("evt-relay", moment, **updates)
    sdk = _claude("sdk-session", "msg_01RELAY", moment)
    assert physical_request_key(hermes).startswith("relay|")
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [hermes, sdk], fact_type="usage_event_v1")
    rows, stats = query_sqlite_physical_requests(
        ledger, occurred_or_observed_at_gte=window[0], occurred_or_observed_at_lt=window[1]
    )
    mirror, mirror_stats = elect_physical_requests([hermes, sdk])
    assert stats == mirror_stats
    assert [row["harness"] for row in rows] == [row["harness"] for row in mirror] == ["claude_code"]
    assert stats["observations"] == 2 and stats["physical_requests"] == 1
    assert stats["relay_observations_without_id"] == 1 and stats["copies_suppressed"] == 1
    assert stats["harness_only_requests"] == 0


def test_relay_rows_leave_filtered_views_additive(tmp_path):
    facts = [
        _hermes("evt-r1", "2026-09-24T10:00:00Z"),  # relayed, no id
        _claude("sdk", "msg_01S", "2026-09-24T10:00:00Z"),
        _hermes("evt-z", "2026-09-24T11:00:00Z", provider="zai"),
    ]
    total, stats = _both(tmp_path / "a", facts)
    hermes_only, _ = _both(tmp_path / "b", facts, filters={"harness": "hermes"})
    claude_only, _ = _both(tmp_path / "c", facts, filters={"harness": "claude_code"})
    assert len(total) == len(hermes_only) + len(claude_only) == 2
    assert [row["source_event_id"] for row in hermes_only] == ["evt-z"]
    assert stats["relay_observations_without_id"] == 1


def test_what_is_not_an_unkeyed_relay():
    # A relayed row WITH Anthropic's id joins its receipt instead.
    keyed = _hermes("evt-k", "2026-09-24T10:00:00Z", provider_request_id="msg_01K2", upstream_provider="anthropic")
    assert physical_request_key(keyed) == "req|anthropic|msg_01K2"
    # Bare "custom" outside the audited window (June: local model endpoints).
    assert not is_unkeyed_relay(_hermes("evt-j", "2026-06-11T03:37:27Z", provider="custom"))
    assert not is_unkeyed_relay(_hermes("evt-late", RELAY_BARE_CUSTOM_WINDOW[1] + "Z", provider="custom"))
    assert is_unkeyed_relay(_hermes("evt-in", "2026-09-25T11:59:59.999Z", provider="custom"))
    # Other harnesses and other providers are untouched.
    assert not is_unkeyed_relay(_hermes("evt-o", "2026-09-24T10:00:00Z", provider="openai-codex"))
    assert not is_unkeyed_relay(_claude("s", "msg_01X", "2026-09-24T10:00:00Z", provider="custom:meridian-primary"))
