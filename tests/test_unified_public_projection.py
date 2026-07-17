from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from decimal import localcontext

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


def _quota(source_id: str, **updates):
    row = {
        "fact_type": "quota_observation_v1", "schema_version": 1,
        "source_namespace": "private:quota", "source_observation_id": source_id,
        "harness": "codex", "observed_at": "2026-07-13T03:00:00Z",
        "provider": "openai", "quota_name": "week", "quota_scope": "account",
        "window_kind": "rolling", "unit": "percent", "measurement_confidence": "exact",
        "limit_value": "100", "used_value": "10", "remaining_value": "90",
        "account_ref": "private-account", "provider_payload_ref": "/private/quota/payload",
        "x_arbitrary_balance": "9999",
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

    assert set(payload) == {
        "kind", "schema_version", "source", "generated_at", "bucket_minutes", "rows",
        "provider_rows", "model_rows", "subscription_rows", "summary",
    }
    assert payload["kind"] == PUBLIC_PROJECTION_KIND == "namrop_public_usage_projection.v2"
    assert payload["schema_version"] == 2 and payload["bucket_minutes"] == 60
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
    assert payload["provider_rows"] == [{
        "label": "Other", "total_tokens": 48, "request_attempts": 4,
        "share_pct": 100.0, "measurement_confidence": "mixed",
    }]
    assert payload["model_rows"] == [{
        "provider_label": "Other", "model_label": "Other", "total_tokens": 48,
        "request_attempts": 4, "share_pct": 100.0, "measurement_confidence": "mixed",
    }]
    assert payload["subscription_rows"] == []
    serialized = json.dumps(payload)
    for forbidden in ("private-provider", "private-model", "private:source", "never publish", "harness"):
        assert forbidden not in serialized


def test_unified_projection_never_publishes_subscription_usage_percentages(tmp_path):
    usage = tmp_path / "usage.sqlite3"
    quota = tmp_path / "quota.sqlite3"
    append_sqlite_facts(usage, [], fact_type="usage_event_v1")
    append_sqlite_facts(quota, [
        _quota("private-cap", used_value="63", remaining_value="37"),
    ], fact_type="quota_observation_v1")

    payload = build_unified_public_projection(
        usage,
        quota_ledger_path=quota,
        hours=1,
        now=NOW,
    )
    serialized = json.dumps(payload)

    assert payload["subscription_rows"] == []
    assert "used_pct" not in serialized
    assert "remaining_pct" not in serialized
    assert "private-account" not in serialized


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
    append_sqlite_facts(ledger, [
        _usage("one", "2026-07-13T03:30:00Z", input_tokens=1)
    ], fact_type="usage_event_v1")
    payload = build_unified_public_projection(ledger, hours=1, now=NOW)
    validate_unified_public_projection(payload)
    for mutant in (
        {**payload, "provider": "leak"},
        {**payload, "rows": [{**payload["rows"][0], "x_private": "leak"}]},
        {**payload, "provider_rows": [{**payload["provider_rows"][0], "provider": "leak"}]},
        {**payload, "model_rows": [{**payload["model_rows"][0], "model": "leak"}]},
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
    assert len(json.dumps(payload).encode("utf-8")) < 1024 * 1024


def test_provider_rows_use_explicit_mapping_grouping_sorting_and_bounded_other(tmp_path):
    ledger = tmp_path / "usage.sqlite3"
    providers = [
        ("openai-codex", 100), ("anthropic", 90), ("openai", 40),
        ("harness-openai", 30), ("opencode-go", 60), ("opencode", 50),
        ("openrouter", 40), ("deepseek", 30), ("acubens-mlx", 20),
        ("harness-acubens", 10), ("llmgateway", 20), ("private-provider", 15),
    ]
    append_sqlite_facts(ledger, [
        _usage(str(index), "2026-07-13T03:10:00Z", provider=provider, input_tokens=tokens)
        for index, (provider, tokens) in enumerate(providers)
    ], fact_type="usage_event_v1")

    rows = build_unified_public_projection(ledger, hours=1, now=NOW)["provider_rows"]

    assert len(rows) == 8
    assert [row["total_tokens"] for row in rows] == sorted(
        (row["total_tokens"] for row in rows), reverse=True
    )
    assert {row["label"] for row in rows} == {
        "Codex", "Claude Code", "OpenAI", "OpenCode Go", "OpenCode", "OpenRouter",
        "DeepSeek", "Other",
    }
    assert next(row for row in rows if row["label"] == "OpenAI")["total_tokens"] == 70
    assert next(row for row in rows if row["label"] == "Other")["total_tokens"] == 65
    assert all(set(row) == {
        "label", "total_tokens", "request_attempts", "share_pct", "measurement_confidence",
    } for row in rows)
    assert sum(row["total_tokens"] for row in rows) == sum(tokens for _, tokens in providers)
    assert "private-provider" not in json.dumps(rows)


def test_projection_percentages_use_exact_rational_half_even_rounding(tmp_path):
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [
        _usage(
            "large", "2026-07-13T03:10:00Z", provider="openai-codex",
            model_requested="gpt-5.6", input_tokens=1951,
        ),
        _usage(
            "small", "2026-07-13T03:20:00Z", provider="openai",
            model_requested="gpt-5.5", cache_read_tokens=49,
        ),
    ], fact_type="usage_event_v1")

    with localcontext() as context:
        context.prec = 2
        payload = build_unified_public_projection(ledger, hours=1, now=NOW)

    assert payload["rows"][0]["cache_hit_pct"] == 2.4
    assert [row["share_pct"] for row in payload["provider_rows"]] == [97.6, 2.4]
    assert [row["share_pct"] for row in payload["model_rows"]] == [97.6, 2.4]


def test_model_rows_prefer_reported_sanitize_public_families_and_merge_bounded_other(tmp_path):
    ledger = tmp_path / "usage.sqlite3"
    facts = [
        _usage(
            "reported", "2026-07-13T03:01:00Z", provider="openai",
            model_requested="claude-private-request", model_reported="openai/gpt-5.2-codex-sol",
            input_tokens=100,
        ),
        _usage(
            "path", "2026-07-13T03:02:00Z", provider="anthropic",
            model_requested="vendor/claude-sonnet-4-sol", input_tokens=90,
        ),
        _usage(
            "unsafe", "2026-07-13T03:03:00Z", provider="openrouter",
            model_requested="private.host/internal-model", input_tokens=80,
        ),
        _usage(
            "unsafe-family", "2026-07-13T03:04:00Z", provider="openai",
            model_requested="gpt-hermes-internal", input_tokens=70,
        ),
    ]
    public_models = [
        "gpt-5.6", "gpt-5.5", "gpt-5.5-fast", "gpt-5.3-codex",
        "claude-sonnet-5", "claude-fable-5", "claude-opus-4-8",
        "claude-opus-4-7", "claude-haiku-4-5-20251001", "deepseek-v4-pro",
        "deepseek-chat", "glm-5.2", "qwen3.6-plus",
    ]
    facts.extend(
        _usage(
            f"public-{index}", "2026-07-13T03:10:00Z", provider="openai",
            model_requested=model, input_tokens=20 - index,
        )
        for index, model in enumerate(public_models)
    )
    append_sqlite_facts(ledger, facts, fact_type="usage_event_v1")

    rows = build_unified_public_projection(ledger, hours=1, now=NOW)["model_rows"]
    serialized = json.dumps(rows)

    assert len(rows) == 12
    assert sum(row["total_tokens"] for row in rows) == sum(
        fact["input_tokens"] for fact in facts
    )
    assert {
        "provider_label": "OpenAI", "model_label": "GPT-5.2 Codex", "total_tokens": 100,
        "request_attempts": 1, "share_pct": 19.2, "measurement_confidence": "exact",
    } in rows
    assert any(
        row["provider_label"] == "Claude Code" and row["model_label"] == "Claude Sonnet 4"
        for row in rows
    )
    assert sum(row["model_label"] == "Other" for row in rows) == 1
    assert all(set(row) == {
        "provider_label", "model_label", "total_tokens", "request_attempts", "share_pct",
        "measurement_confidence",
    } for row in rows)
    for private in (
        "claude-private-request", "-sol", "private.host", "internal-model",
        "gpt-hermes-internal", "openai/",
    ):
        assert private.casefold() not in serialized.casefold()


def test_model_rows_do_not_pass_through_private_family_shaped_names(tmp_path):
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [
        _usage(
            "private-gpt", "2026-07-13T03:01:00Z", provider="openai",
            model_requested="private.example/gpt-customer-codename", input_tokens=50,
        ),
        _usage(
            "private-claude", "2026-07-13T03:02:00Z", provider="anthropic",
            model_requested="claude-secret-hostname", input_tokens=40,
        ),
    ], fact_type="usage_event_v1")

    rows = build_unified_public_projection(ledger, hours=1, now=NOW)["model_rows"]

    assert rows == [{
        "provider_label": "Other", "model_label": "Other", "total_tokens": 90,
        "request_attempts": 2, "share_pct": 100.0, "measurement_confidence": "exact",
    }]
    assert "customer-codename" not in json.dumps(rows).casefold()
    assert "secret-hostname" not in json.dumps(rows).casefold()


def test_signed_corrections_preserve_canonical_totals_and_do_not_add_attempts(tmp_path):
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [
        _usage(
            "original", "2026-07-13T03:10:00Z", provider="openai",
            model_requested="gpt-5.2-codex", input_tokens=100, cache_read_tokens=100,
            reasoning_tokens=20,
        ),
        _usage(
            "correction", "2026-07-13T03:10:00Z", provider="openai",
            model_requested="gpt-5.2-codex", record_kind="correction",
            input_tokens=-150, reasoning_tokens=-5,
            corrects_source_namespace="private:source", corrects_source_event_id="original",
        ),
    ], fact_type="usage_event_v1")

    payload = build_unified_public_projection(ledger, hours=1, now=NOW)

    assert payload["summary"]["total_tokens"] == 50
    assert payload["summary"]["request_attempts"] == 1
    assert payload["rows"][0]["reasoning_tokens"] == 15
    assert payload["rows"][0]["cache_hit_pct"] == 100.0
    assert payload["provider_rows"][0]["total_tokens"] == 50
    assert payload["model_rows"][0]["total_tokens"] == 50


def test_quota_ledger_input_is_ignored_at_the_public_projection_boundary(tmp_path):
    usage = tmp_path / "usage.sqlite3"
    quota = tmp_path / "quota.sqlite3"
    append_sqlite_facts(usage, [], fact_type="usage_event_v1")
    append_sqlite_facts(quota, [_quota("private-cap")], fact_type="quota_observation_v1")

    payload = build_unified_public_projection(
        usage, quota_ledger_path=quota, hours=1, now=NOW
    )

    assert payload["subscription_rows"] == []
    assert "used_pct" not in json.dumps(payload)


def test_validator_rejects_nonempty_subscription_rows(tmp_path):
    usage = tmp_path / "usage.sqlite3"
    append_sqlite_facts(usage, [], fact_type="usage_event_v1")
    payload = build_unified_public_projection(usage, hours=1, now=NOW)
    payload["subscription_rows"] = [{
        "service": "Codex",
        "window": "Weekly",
        "used_pct": 63.0,
        "remaining_pct": 37.0,
        "reset_hours": 18,
        "measurement_confidence": "exact",
        "status": "current",
    }]

    with pytest.raises(ValueError, match="subscription_rows must be empty"):
        validate_unified_public_projection(payload)


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
        "--quota-ledger", "/private/quota.sqlite3",
        "--public-projection", str(tmp_path / "public.json"), "--hours", "24",
        "--public-projection-source", "namrop-test",
    ])
    assert main() == 0
    assert captured == {
        "ledger_path": "/private/usage.sqlite3", "projection_path": str(tmp_path / "public.json"),
        "quota_ledger_path": "/private/quota.sqlite3", "hours": 24, "source": "namrop-test",
    }
    assert capsys.readouterr().out.strip() == f"public_projection: {tmp_path / 'public.json'}"


@pytest.mark.parametrize(("private_name", "public_label"), [
    ("gpt-5.6", "GPT-5.6"),
    ("PRIVATE.HOST/GPT-5.5-SOL", "GPT-5.5"),
    ("gpt-5.5-fast", "GPT-5.5 Fast"),
    ("gpt-5.3-codex", "GPT-5.3 Codex"),
    ("claude-sonnet-5", "Claude Sonnet 5"),
    ("claude-fable-5", "Claude Fable 5"),
    ("claude-opus-4-8", "Claude Opus 4.8"),
    ("claude-opus-4.8", "Claude Opus 4.8"),
    ("claude-opus-4-7", "Claude Opus 4.7"),
    ("claude-opus-4.7", "Claude Opus 4.7"),
    ("claude-haiku-4-5-20251001", "Claude Haiku 4.5"),
    ("deepseek-v4-pro", "DeepSeek V4 Pro"),
    ("deepseek-chat", "DeepSeek Chat"),
    ("glm-5.2", "GLM 5.2"),
    ("glm-5.1", "GLM 5.1"),
    ("qwen/qwen3.6-plus", "Qwen 3.6 Plus"),
    ("gemma-4-31b-it", "Gemma 4 31B IT"),
    ("grok-4.20-reasoning", "Grok 4.20 Reasoning"),
    ("aion-2.0", "Aion 2.0"),
])
def test_model_sanitizer_uses_only_finite_exact_normalized_mappings(private_name, public_label):
    assert projection_module._public_model_label(private_name) == public_label
    assert projection_module._public_model_label(public_label) == public_label


@pytest.mark.parametrize("private_name", [
    "private.host/gpt-5-8675309",
    "gpt-5-8675309",
    "private.host/claude-opus-4-8675309",
    "qwen-8675309",
])
def test_model_sanitizer_maps_malicious_numeric_family_names_to_other(tmp_path, private_name):
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [
        _usage(
            "malicious", "2026-07-13T03:01:00Z", provider="openai",
            model_requested=private_name, input_tokens=50,
        ),
    ], fact_type="usage_event_v1")

    assert build_unified_public_projection(ledger, hours=1, now=NOW)["model_rows"] == [{
        "provider_label": "Other", "model_label": "Other", "total_tokens": 50,
        "request_attempts": 1, "share_pct": 100.0, "measurement_confidence": "exact",
    }]


def test_correction_rankings_use_corrected_event_dimensions_not_correction_dimensions(tmp_path):
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [
        _usage(
            "openai-original", "2026-07-13T03:10:00Z", provider="openai",
            model_requested="gpt-5.6", input_tokens=100,
        ),
        _usage(
            "anthropic-original", "2026-07-13T03:11:00Z", provider="anthropic",
            model_requested="claude-sonnet-5", input_tokens=80,
        ),
        _usage(
            "mismatched-correction", "2026-07-13T03:12:00Z", provider="anthropic",
            model_requested="claude-sonnet-5", record_kind="correction", input_tokens=-40,
            corrects_source_namespace="private:source",
            corrects_source_event_id="openai-original",
        ),
    ], fact_type="usage_event_v1")

    payload = build_unified_public_projection(ledger, hours=1, now=NOW)

    assert [(row["label"], row["total_tokens"], row["share_pct"]) for row in payload["provider_rows"]] == [
        ("Claude Code", 80, 57.1), ("OpenAI", 60, 42.9),
    ]
    assert [
        (row["provider_label"], row["model_label"], row["total_tokens"])
        for row in payload["model_rows"]
    ] == [
        ("Claude Code", "Claude Sonnet 5", 80),
        ("OpenAI", "GPT-5.6", 60),
    ]


def test_unresolved_corrections_apply_hourly_but_are_excluded_from_rankings(tmp_path):
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [
        _usage(
            "known", "2026-07-13T03:10:00Z", provider="openai",
            model_requested="gpt-5.6", input_tokens=50,
        ),
        _usage(
            "missing-attribution", "2026-07-13T03:12:00Z", provider="private-provider",
            model_requested="private.host/gpt-5-8675309", record_kind="correction",
            input_tokens=-100, corrects_source_namespace="private:missing",
            corrects_source_event_id="not-queried",
        ),
    ], fact_type="usage_event_v1")

    payload = build_unified_public_projection(ledger, hours=1, now=NOW)

    assert payload["summary"]["total_tokens"] == -50
    assert payload["provider_rows"] == [{
        "label": "OpenAI", "total_tokens": 50, "request_attempts": 1,
        "share_pct": 100.0, "measurement_confidence": "exact",
    }]
    assert payload["model_rows"] == [{
        "provider_label": "OpenAI", "model_label": "GPT-5.6", "total_tokens": 50,
        "request_attempts": 1, "share_pct": 100.0, "measurement_confidence": "exact",
    }]


def test_rankings_omit_nonpositive_groups_and_use_positive_ranking_denominator(tmp_path):
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [
        _usage(
            "overcorrected", "2026-07-13T03:10:00Z", provider="openai",
            model_requested="gpt-5.6", input_tokens=50,
        ),
        _usage(
            "positive", "2026-07-13T03:11:00Z", provider="anthropic",
            model_requested="claude-sonnet-5", input_tokens=100,
        ),
        _usage(
            "overcorrection", "2026-07-13T03:12:00Z", provider="private-provider",
            model_requested="private.host/gpt-5-8675309", record_kind="correction",
            input_tokens=-80, corrects_source_namespace="private:source",
            corrects_source_event_id="overcorrected",
        ),
    ], fact_type="usage_event_v1")

    payload = build_unified_public_projection(ledger, hours=1, now=NOW)

    assert payload["summary"]["total_tokens"] == 70
    assert [(row["label"], row["total_tokens"], row["share_pct"]) for row in payload["provider_rows"]] == [
        ("Claude Code", 100, 100.0),
    ]
    assert [(row["model_label"], row["total_tokens"], row["share_pct"]) for row in payload["model_rows"]] == [
        ("Claude Sonnet 5", 100, 100.0),
    ]


def test_size_guard_measures_pretty_serialization_actually_written(tmp_path, monkeypatch):
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, [], fact_type="usage_event_v1")
    payload = build_unified_public_projection(ledger, hours=1, now=NOW)
    compact_size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    pretty_size = len(
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    assert compact_size < pretty_size
    monkeypatch.setattr(projection_module, "_MAX_PUBLIC_PROJECTION_BYTES", compact_size + 1)

    with pytest.raises(ValueError, match="smaller than 1 MiB"):
        validate_unified_public_projection(payload)
