"""The Meridian lanes are the two Claude accounts (keeper, 2026-09-25, Discord
unified usage ledger thread msg 1552986099343818813: "meridian primary and
meridian yugen are really anthropic 1 and 2"; "they could be pooled though,
and just ommitted from the pie charts"; go: msg 1552992232733413420).

Share views (provider donuts, status strip, public chart) pool them as
Anthropic; row views (provider/model table, model series) name the account
as "Claude 1" / "Claude 2", the ordinals the quota surfaces use.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from codex_usage_tracker.canonical_ledger import append_sqlite_facts
from codex_usage_tracker.dashboard import create_app
from codex_usage_tracker.unified_public_projection import build_unified_public_projection

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _at(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def _fact(namespace, event, harness, provider, occurred_at, tokens, **updates):
    row = {
        "fact_type": "usage_event_v1", "schema_version": 1,
        "source_namespace": namespace, "source_event_id": event,
        "harness": harness, "purpose": "main", "record_kind": "api_attempt",
        "occurred_at": occurred_at, "recorded_at": occurred_at,
        "usage_source": "provider_reported", "usage_completeness": "complete",
        "measurement_confidence": "exact", "cost_status": "included",
        "provider": provider, "model_requested": "claude-opus-5-5", "model_reported": "claude-opus-5-5",
        "input_tokens": tokens, "cache_read_tokens": 0, "cache_write_tokens": 0,
        "output_tokens": 0, "reasoning_tokens": 0,
    }
    row.update(updates)
    return row


def _ledger(tmp_path):
    facts = [
        # Claude 1: a Hermes call through meridian-primary joined to its receipt,
        # and a direct Claude Code call on the primary account.
        _fact("sol:hermes", "h1", "hermes", "custom:meridian-primary", _at(1), 100,
              provider_request_id="msg_01PRIMARYAAAAAAAAAAAAAAA", upstream_provider="anthropic"),
        _fact("sol:claude-code", "sdk:msg_01PRIMARYAAAAAAAAAAAAAAA", "claude_code", "anthropic", _at(1), 100),
        _fact("sol:claude-code", "cli:msg_01DIRECTAAAAAAAAAAAAAAAAA", "claude_code", "anthropic", _at(2), 10),
        # Claude 2: a yugen Hermes call with no Anthropic id (counts through
        # its receipt), and the receipt.
        _fact("sol:hermes", "h2", "hermes", "custom:meridian-yugen", _at(3), 200),
        _fact("sol:claude-code-secondary", "sdk2:msg_01YUGENBBBBBBBBBBBBBBBBBB", "claude_code", "anthropic", _at(3), 200),
        # A bare "custom" Hermes row with Anthropic's id: Anthropic, account
        # not recorded (the route field starts at the collector cutover).
        _fact("sol:hermes", "h3", "hermes", "custom", _at(4), 30,
              provider_request_id="msg_01BARECCCCCCCCCCCCCCCCCC", upstream_provider="anthropic"),
        # After the cutover the collector records the route: Claude 2.
        _fact("sol:hermes", "h4", "hermes", "custom", _at(0.5), 40,
              provider_request_id="msg_01ROUTEDDDDDDDDDDDDDDDDD", upstream_provider="anthropic",
              x_relay_route="meridian-yugen"),
        # Not Anthropic: untouched.
        _fact("sol:hermes", "h5", "hermes", "zai", _at(1), 7, model_requested="glm-5.3", model_reported="glm-5.3"),
    ]
    path = tmp_path / "usage.sqlite3"
    append_sqlite_facts(path, facts, fact_type="usage_event_v1")
    return path


def test_share_views_pool_meridian_under_anthropic(tmp_path):
    payload = create_app(unified_usage_ledger=str(_ledger(tmp_path))).test_client().get(
        "/api/unified-usage?hours=24"
    ).get_json()
    providers = {row["provider"]: row for row in payload["by_provider"]}
    assert set(providers) == {"anthropic", "z-ai-glm"}
    # 100 (joined) + 10 + 200 (receipt; the id-less Hermes row is not counted) + 30 + 40
    assert providers["anthropic"]["total_tokens"] == 380
    assert providers["anthropic"]["events"] == 5
    assert set(providers["anthropic"]["provider_raw_ids"]) == {"anthropic", "custom", "custom:meridian-primary"}
    strip = {row["provider"] for row in payload["time_series"]["provider_status_series"]}
    assert strip == {"anthropic", "z-ai-glm"}
    assert payload["identity"]["relay_observations_without_id"] == 1


def test_row_views_name_the_account(tmp_path):
    payload = create_app(unified_usage_ledger=str(_ledger(tmp_path))).test_client().get(
        "/api/unified-usage?hours=24"
    ).get_json()
    table = {row["provider_label"]: row for row in payload["by_provider_model"]}
    assert set(table) == {"Claude 1", "Claude 2", "Claude (account not recorded)", "zai"}
    assert table["Claude 1"]["total_tokens"] == 110 and table["Claude 1"]["provider_account_index"] == 1
    assert table["Claude 2"]["total_tokens"] == 240 and table["Claude 2"]["provider_account_count"] == 2
    assert table["Claude (account not recorded)"]["provider_account_index"] is None
    assert table["zai"]["provider"] == "zai" and table["zai"]["provider_account_index"] is None
    labels = {row["label"] for row in payload["time_series"]["model_series"]}
    assert {"claude-opus-5-5 · Claude 1", "claude-opus-5-5 · Claude 2"} <= labels
    assert not any("meridian" in label for label in labels)


def test_public_chart_counts_meridian_as_anthropic(tmp_path):
    projection = build_unified_public_projection(_ledger(tmp_path), hours=24, source="test", now=NOW)
    labels = {row["label"]: row for row in projection["provider_rows"]}
    assert "Other" not in labels
    assert labels["Anthropic"]["total_tokens"] == 380
