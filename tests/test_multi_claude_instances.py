from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from codex_usage_tracker.canonical_ledger import (
    append_sqlite_facts,
    canonical_json,
    normalize_fact,
    read_sqlite_facts,
)
from codex_usage_tracker.claude_instances import (
    ClaudeInstance,
    parse_claude_instance_declaration,
)
from codex_usage_tracker.cli import main
import codex_usage_tracker.collector as collector_module
from codex_usage_tracker.collector import collect_all
import codex_usage_tracker.dashboard as dashboard_module
from codex_usage_tracker.dashboard import create_app
import codex_usage_tracker.quota as quota_module
from codex_usage_tracker.quota import collect_claude_code_quota
from codex_usage_tracker.unified_public_projection import build_unified_public_projection


def _claude_receipt(namespace: str, event_id: str, *, status: str = "ok", output: int = 5):
    return normalize_fact(
        {
            "fact_type": "usage_event_v1",
            "schema_version": 1,
            "source_namespace": namespace,
            "source_event_id": event_id,
            "harness": "claude_code",
            "purpose": "main",
            "record_kind": "api_attempt",
            "occurred_at": "2026-08-05T11:15:00Z",
            "recorded_at": "2026-08-05T11:15:00Z",
            "provider": "anthropic",
            "model_requested": "claude-sonnet-5",
            "model_reported": "claude-sonnet-5",
            "request_status": status,
            "input_tokens": 10,
            "cache_read_tokens": 2,
            "cache_write_tokens": 3,
            "output_tokens": output,
            "reasoning_tokens": 0,
            "usage_source": "provider_reported",
            "usage_completeness": "complete",
            "measurement_confidence": "exact",
            "cost_status": "included",
        }
    )


def _write_claude_transcript(config_dir: Path, session: str, input_tokens: int, output_tokens: int):
    project = config_dir / "projects" / "project"
    project.mkdir(parents=True)
    row = {
        "type": "assistant",
        "sessionId": session,
        "timestamp": "2026-08-05T11:15:00Z",
        "message": {
            "id": f"message-{session}",
            "model": "claude-sonnet-5",
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": 2,
                "cache_creation_input_tokens": 3,
                "output_tokens": output_tokens,
            },
        },
    }
    (project / "session.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")


def _collection_paths(tmp_path):
    return {
        "state_db": tmp_path / "missing-state.db",
        "opencode_dbs": [],
        "codex_ledger": tmp_path / "missing-codex.jsonl",
        "usage_ledger": tmp_path / "usage.sqlite3",
        "quota_ledger": tmp_path / "quota.sqlite3",
    }


def _empty_other_collectors(monkeypatch):
    monkeypatch.setattr(collector_module, "collect_hermes_usage", lambda *args, **kwargs: [])
    monkeypatch.setattr(collector_module, "collect_opencode_usage", lambda *args, **kwargs: [])
    monkeypatch.setattr(collector_module, "codex_quota_observations", lambda *args, **kwargs: [])
    monkeypatch.setattr(collector_module, "derive_opencode_go_quotas", lambda *args, **kwargs: [])


def test_collect_all_legacy_claude_root_and_primary_namespace_remain_unchanged(tmp_path, monkeypatch):
    _empty_other_collectors(monkeypatch)
    calls = []

    def fake_claude(root, namespace):
        calls.append((Path(root), namespace))
        return [_claude_receipt(namespace, "legacy")]

    monkeypatch.setattr(collector_module, "collect_claude_usage", fake_claude)
    legacy_root = tmp_path / "legacy-projects"
    result = collect_all(
        claude_root=legacy_root,
        live_quota=False,
        scope="usage",
        **_collection_paths(tmp_path),
    )

    assert calls == [(legacy_root, "sol:claude-code")]
    assert result["sources"]["claude_code"] == {"discovered": 1}
    assert "claude_code_secondary" not in result["sources"]
    rows = read_sqlite_facts(tmp_path / "usage.sqlite3", fact_type="usage_event_v1")
    assert [row["source_namespace"] for row in rows] == ["sol:claude-code"]


def test_two_claude_usage_roots_keep_namespaces_and_pool_public_aggregates(tmp_path, monkeypatch):
    _empty_other_collectors(monkeypatch)
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    _write_claude_transcript(primary, "primary-session", 10, 5)
    _write_claude_transcript(secondary, "secondary-session", 20, 7)

    result = collect_all(
        claude_instances=[
            ClaudeInstance("claude-code", primary),
            ClaudeInstance("claude-code-secondary", secondary),
        ],
        live_quota=False,
        scope="usage",
        **_collection_paths(tmp_path),
    )

    assert result["sources"]["claude_code"] == {"discovered": 1}
    assert result["sources"]["claude_code_secondary"] == {"discovered": 1}
    rows = read_sqlite_facts(tmp_path / "usage.sqlite3", fact_type="usage_event_v1")
    assert {row["source_namespace"] for row in rows} == {
        "sol:claude-code",
        "sol:claude-code-secondary",
    }

    projection = build_unified_public_projection(
        tmp_path / "usage.sqlite3",
        hours=2,
        now=datetime(2026, 8, 5, 13, 30, tzinfo=timezone.utc),
    )
    assert projection["summary"]["total_tokens"] == 52
    assert projection["provider_rows"] == [
        {
            "label": "Anthropic",
            "total_tokens": 52,
            "request_attempts": 2,
            "share_pct": 100.0,
            "measurement_confidence": "exact",
        }
    ]
    assert projection["harness_rows"][0]["label"] == "Claude Code"
    assert projection["harness_rows"][0]["total_tokens"] == 52
    assert projection["model_rows"] == [
        {
            "provider_label": "Anthropic",
            "model_label": "Claude Sonnet 5",
            "total_tokens": 52,
            "request_attempts": 2,
            "share_pct": 100.0,
            "measurement_confidence": "exact",
        }
    ]
    time_series = dashboard_module._build_unified_time_series(
        rows,
        window_start=datetime(2026, 8, 5, 11, tzinfo=timezone.utc),
        window_end=datetime(2026, 8, 5, 13, tzinfo=timezone.utc),
        generated_at=datetime(2026, 8, 5, 13, 30, tzinfo=timezone.utc),
    )
    assert time_series["model_series"] == [
        {
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "label": "claude-sonnet-5 · anthropic",
            "total_tokens": 52,
            "values": [52, 0],
        }
    ]
    serialized = json.dumps({"projection": projection, "time_series": time_series})
    assert "sol:claude-code" not in serialized
    assert "account_ref" not in serialized


def test_secondary_claude_failure_is_isolated_warned_and_strict_policy_is_truthful(
    tmp_path, monkeypatch
):
    _empty_other_collectors(monkeypatch)
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"

    def fake_claude(root, namespace):
        if Path(root) == secondary / "projects":
            raise OSError("private secondary source path")
        return [_claude_receipt(namespace, "primary")]

    monkeypatch.setattr(collector_module, "collect_claude_usage", fake_claude)
    kwargs = {
        **_collection_paths(tmp_path),
        "usage_ledger": tmp_path / "usage-nonstrict.sqlite3",
        "claude_instances": [
            ClaudeInstance("claude-code", primary),
            ClaudeInstance("claude-code-secondary", secondary),
        ],
        "live_quota": False,
        "scope": "usage",
    }
    result = collect_all(**kwargs)

    assert result["sources"]["claude_code"] == {"discovered": 1}
    assert result["sources"]["claude_code_secondary"] == {"discovered": 0}
    assert result["warnings"] == [
        {"source": "claude_code_secondary", "error": "OSError"}
    ]
    assert len(read_sqlite_facts(kwargs["usage_ledger"], fact_type="usage_event_v1")) == 1

    kwargs["usage_ledger"] = tmp_path / "usage-strict.sqlite3"
    kwargs["strict_sources"] = True
    with pytest.raises(RuntimeError, match="strict source policy"):
        collect_all(**kwargs)
    assert len(read_sqlite_facts(kwargs["usage_ledger"], fact_type="usage_event_v1")) == 1


def test_secondary_claude_finalization_uses_same_reconciliation_path(tmp_path, monkeypatch):
    _empty_other_collectors(monkeypatch)
    namespace = "sol:claude-code-secondary"
    baseline = _claude_receipt(namespace, "secondary-stream", status="unknown", output=5)
    finalized = dict(
        baseline,
        request_status="ok",
        occurred_at="2026-08-05T11:15:03Z",
        recorded_at="2026-08-05T11:15:03Z",
        output_tokens=25,
    )
    append_sqlite_facts(
        tmp_path / "usage.sqlite3", [baseline], fact_type="usage_event_v1"
    )
    monkeypatch.setattr(
        collector_module,
        "collect_claude_usage",
        lambda root, source_namespace: [finalized],
    )

    result = collect_all(
        claude_instances=[
            ClaudeInstance("claude-code-secondary", tmp_path / "secondary")
        ],
        live_quota=False,
        scope="usage",
        **_collection_paths(tmp_path),
    )

    rows = read_sqlite_facts(tmp_path / "usage.sqlite3", fact_type="usage_event_v1")
    correction = next(row for row in rows if row["record_kind"] == "correction")
    assert correction["corrects_source_namespace"] == namespace
    assert correction["output_tokens"] == 20
    assert result["generated_corrections"] == 1
    assert result["stabilized_replays"][0]["source"] == "claude_code_secondary"


@pytest.mark.parametrize(
    "declaration",
    [
        "missing-separator",
        "CLAUDE-code=/absolute",
        "claude_code=/absolute",
        "not-claude=/absolute",
        "claude-code-=/absolute",
        "claude-code--secondary=/absolute",
        "claude-code-secondary=relative/path",
        "claude-code-secondary=",
    ],
)
def test_claude_instance_cli_rejects_unsafe_declarations(declaration, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["tracker", "collect-all", "--claude-instance", declaration, "--dry-run"],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_claude_instance_cli_rejects_duplicate_suffix(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "sys.argv",
        [
            "tracker",
            "collect-all",
            "--claude-instance",
            f"claude-code={tmp_path / 'one'}",
            "--claude-instance",
            f"claude-code={tmp_path / 'two'}",
            "--dry-run",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_claude_instance_parser_expands_user_home_without_requiring_existing_path(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))

    instance = parse_claude_instance_declaration(
        "claude-code-secondary=~/.claude-accounts/secondary"
    )

    assert instance == ClaudeInstance(
        "claude-code-secondary", tmp_path / ".claude-accounts" / "secondary"
    )


def test_claude_instance_primary_uses_default_quota_layout_but_keeps_declared_usage_root(
    tmp_path,
):
    primary = ClaudeInstance("claude-code", tmp_path / ".claude")
    secondary = ClaudeInstance(
        "claude-code-secondary", tmp_path / ".claude-accounts" / "secondary"
    )

    assert primary.config_dir == tmp_path / ".claude"
    assert primary.quota_config_dir is None
    assert primary.account_ref is None
    assert secondary.quota_config_dir == tmp_path / ".claude-accounts" / "secondary"
    assert secondary.account_ref == "claude-code-secondary"


def test_claude_instance_cli_threads_repeatable_structured_instances(monkeypatch, tmp_path, capsys):
    captured = {}

    def fake_collect_all(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(collector_module, "collect_all", fake_collect_all)
    monkeypatch.setattr(
        "sys.argv",
        [
            "tracker",
            "collect-all",
            "--claude-instance",
            f"claude-code={tmp_path / 'primary'}",
            "--claude-instance",
            f"claude-code-secondary={tmp_path / 'secondary'}",
            "--dry-run",
            "--no-live-quota",
        ],
    )

    assert main() == 0
    assert captured["claude_instances"] == [
        ClaudeInstance("claude-code", tmp_path / "primary"),
        ClaudeInstance("claude-code-secondary", tmp_path / "secondary"),
    ]
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def _claude_usage_screen():
    return """
Current session
34% used
Resets 3:40pm (America/New_York)
Current week (all models)
47% used
Resets Aug 12, 8pm (America/New_York)
Current week (Fable)
81% used
Resets Aug 12, 8pm (America/New_York)
"""


def test_claude_quota_applies_config_and_stable_private_account_identity(tmp_path, monkeypatch):
    captured = []

    def fake_capture(**kwargs):
        captured.append(kwargs)
        return _claude_usage_screen()

    monkeypatch.setattr(quota_module, "capture_claude_code_usage_screen", fake_capture)
    secondary_config = tmp_path / "secondary-config"
    secondary = collect_claude_code_quota(
        "sol:claude-code-secondary-quota",
        probe_dir=tmp_path / "probe-secondary",
        claude_config_dir=secondary_config,
        account_ref="claude-code-secondary",
        observed_at="2026-08-05T12:00:00Z",
    )
    primary = collect_claude_code_quota(
        "sol:claude-code-quota",
        probe_dir=tmp_path / "probe-primary",
        observed_at="2026-08-05T12:00:00Z",
    )

    assert captured[0]["claude_config_dir"] == secondary_config
    assert captured[0]["account_ref"] == "claude-code-secondary"
    assert all(row["account_ref"] == "claude-code-secondary" for row in secondary)
    assert all(row["account_ref"] is None for row in primary)


def test_collect_all_uses_independent_claude_quota_configs_probes_and_namespaces(
    tmp_path, monkeypatch
):
    _empty_other_collectors(monkeypatch)
    captures = []
    monkeypatch.setattr(
        quota_module,
        "capture_claude_code_usage_screen",
        lambda **kwargs: captures.append(kwargs) or _claude_usage_screen(),
    )
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    probe = tmp_path / "claude-probe"

    result = collect_all(
        claude_instances=[
            ClaudeInstance("claude-code", primary),
            ClaudeInstance("claude-code-secondary", secondary),
        ],
        claude_quota_command="claude",
        claude_probe_dir=probe,
        environment={},
        live_quota=True,
        scope="quota",
        **_collection_paths(tmp_path),
    )

    assert result["sources"]["claude_code_quota"] == {"discovered": 3}
    assert result["sources"]["claude_code_secondary_quota"] == {"discovered": 3}
    assert [(call["claude_config_dir"], Path(call["probe_dir"]), call["account_ref"]) for call in captures] == [
        (None, probe, None),
        (secondary, tmp_path / "claude-probe-secondary", "claude-code-secondary"),
    ]
    rows = read_sqlite_facts(tmp_path / "quota.sqlite3", fact_type="quota_observation_v1")
    assert {row["source_namespace"] for row in rows} >= {
        "sol:claude-code-quota",
        "sol:claude-code-secondary-quota",
    }
    assert {row["account_ref"] for row in rows if row["provider"] == "anthropic"} == {
        None,
        "claude-code-secondary",
    }


def test_multi_claude_quota_dry_run_never_probes_or_creates_probe_dirs(tmp_path, monkeypatch):
    _empty_other_collectors(monkeypatch)

    def forbidden_probe(*args, **kwargs):
        raise AssertionError("Claude quota probe must not run during dry-run")

    monkeypatch.setattr(collector_module, "collect_claude_code_quota", forbidden_probe)
    probe = tmp_path / "claude-probe"
    result = collect_all(
        claude_instances=[
            ClaudeInstance("claude-code", tmp_path / "primary"),
            ClaudeInstance("claude-code-secondary", tmp_path / "secondary"),
        ],
        claude_quota_command="claude",
        claude_probe_dir=probe,
        environment={},
        live_quota=True,
        dry_run=True,
        scope="quota",
        **_collection_paths(tmp_path),
    )

    assert result["sources"]["claude_code_quota"] == {"discovered": 0}
    assert result["sources"]["claude_code_secondary_quota"] == {"discovered": 0}
    assert not probe.exists()
    assert not (tmp_path / "claude-probe-secondary").exists()


def test_tmux_command_only_sets_claude_config_dir_for_custom_account(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    started = []
    screens = iter(["❯", _claude_usage_screen(), "❯", _claude_usage_screen()])

    def fake_run(command, **kwargs):
        started.append(command)

        class Completed:
            stdout = ""

        return Completed()

    monkeypatch.setattr(quota_module.subprocess, "run", fake_run)
    monkeypatch.setattr(quota_module, "_tmux_capture", lambda session: next(screens))
    monkeypatch.setattr(quota_module, "_tmux_keys", lambda *args: None)
    monkeypatch.setattr(quota_module.time, "sleep", lambda seconds: None)

    primary_screen = quota_module.capture_claude_code_usage_screen(
        probe_dir=tmp_path / "probe-primary",
        timeout=1,
    )
    secondary_screen = quota_module.capture_claude_code_usage_screen(
        probe_dir=tmp_path / "probe-secondary",
        claude_config_dir=config,
        account_ref="stable-secondary",
        timeout=1,
    )

    assert "Current week (all models)" in primary_screen
    assert "Current week (all models)" in secondary_screen
    primary_command, secondary_command = [
        command for command in started if command[:2] == ["tmux", "new-session"]
    ]
    primary_probe = str((tmp_path / "probe-primary").resolve())
    primary_tail = primary_command[primary_command.index(primary_probe) + 1 :]
    assert primary_tail[0] == "claude"
    assert "env" not in primary_command
    assert not any("CLAUDE_CONFIG_DIR" in str(part) for part in primary_command)

    secondary_probe = str((tmp_path / "probe-secondary").resolve())
    secondary_tail = secondary_command[secondary_command.index(secondary_probe) + 1 :]
    assert secondary_tail[:2] == ["env", f"CLAUDE_CONFIG_DIR={config.resolve()}"]
    assert "--safe-mode" in primary_tail
    assert "--safe-mode" in secondary_tail
    assert not any("token" in str(part).casefold() for part in primary_tail + secondary_tail)


def _quota_row(
    source_id: str,
    account_ref: str | None,
    used: int,
    *,
    quota_name: str = "seven_day",
):
    return normalize_fact(
        {
            "fact_type": "quota_observation_v1",
            "schema_version": 1,
            "source_namespace": f"private:{source_id}",
            "source_observation_id": source_id,
            "harness": "claude_code",
            "observed_at": "2026-08-05T11:15:00Z",
            "provider": "anthropic",
            "account_ref": account_ref,
            "quota_name": quota_name,
            "quota_scope": "account",
            "window_kind": "rolling",
            "limit_value": "100",
            "remaining_value": str(100 - used),
            "used_value": str(used),
            "unit": "percent",
            "measurement_confidence": "exact",
            "provider_payload_ref": f"/private/{source_id}",
        }
    )


def test_unified_weekly_keeps_two_anonymized_anthropic_accounts_independent(
    tmp_path, monkeypatch
):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 5, 12, 30, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)
    rows = [
        _quota_row("primary-private-source", None, 20),
        _quota_row("secondary-private-source", "claude-code-secondary", 80),
    ]
    ledger = tmp_path / "quota.jsonl"
    ledger.write_text("\n".join(canonical_json(row) for row in rows) + "\n", encoding="utf-8")

    response = create_app(quota_ledger=str(ledger)).test_client().get(
        "/api/subscriptions?hours=1&history=0"
    )
    assert response.status_code == 200
    payload = response.get_json()
    unified = payload["time_series"]["unified_weekly"]
    anthropic = [row for row in unified["series"] if row["provider"] == "anthropic"]
    assert [row["label"] for row in anthropic] == [
        "Anthropic / Claude · account 1",
        "Anthropic / Claude · account 2",
    ]
    assert [row["values"] for row in anthropic] == [[20.0], [80.0]]
    assert [row["provider_account_index"] for row in anthropic] == [1, 2]
    assert all(row["provider_account_count"] == 2 for row in anthropic)
    assert not any(
        row["provider"] == "anthropic" for row in unified["no_data_providers"]
    )

    serialized = json.dumps(payload)
    for private_value in (
        "primary-private-source",
        "secondary-private-source",
        "claude-code-secondary",
        "account_ref",
        "source_namespace",
        "source_observation_id",
        "/private/",
    ):
        assert private_value not in serialized


def test_two_anthropic_accounts_have_stable_safe_metadata_across_every_quota_window(
    tmp_path, monkeypatch
):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 5, 12, 30, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)
    rows = [
        _quota_row(
            f"{private_source}-{quota_name}",
            account_ref,
            used,
            quota_name=quota_name,
        )
        for private_source, account_ref, values in (
            ("primary-private-source", None, (11, 22, 33)),
            ("secondary-private-source", "claude-code-secondary", (61, 72, 83)),
        )
        for quota_name, used in zip(
            ("five_hour", "seven_day", "seven_day_fable"), values, strict=True
        )
    ]
    ledger = tmp_path / "quota.jsonl"
    ledger.write_text(
        "\n".join(canonical_json(row) for row in reversed(rows)) + "\n",
        encoding="utf-8",
    )

    payload = create_app(quota_ledger=str(ledger)).test_client().get(
        "/api/subscriptions?hours=1&history=0"
    ).get_json()
    provider = next(
        row
        for row in payload["time_series"]["providers"]
        if row["provider"] == "anthropic"
    )
    assert len(provider["series"]) == 6
    by_account = {
        account_index: [
            row
            for row in provider["series"]
            if row["provider_account_index"] == account_index
        ]
        for account_index in (1, 2)
    }
    assert all(
        {row["quota_name"] for row in account_rows}
        == {"five_hour", "seven_day", "seven_day_fable"}
        for account_rows in by_account.values()
    )
    assert all(
        row["provider_account_count"] == 2
        for row in provider["series"] + provider["unavailable_series"]
    )
    assert {row["quota_name"]: row["values"] for row in by_account[1]} == {
        "five_hour": [11.0],
        "seven_day": [22.0],
        "seven_day_fable": [33.0],
    }
    assert {row["quota_name"]: row["values"] for row in by_account[2]} == {
        "five_hour": [61.0],
        "seven_day": [72.0],
        "seven_day_fable": [83.0],
    }

    anthropic_weekly = [
        row
        for row in payload["time_series"]["unified_weekly"]["series"]
        if row["provider"] == "anthropic"
    ]
    assert [row["label"] for row in anthropic_weekly] == [
        "Anthropic / Claude · account 1",
        "Anthropic / Claude · account 2",
    ]
    assert [row["values"] for row in anthropic_weekly] == [[22.0], [72.0]]
    assert [row["provider_account_index"] for row in anthropic_weekly] == [1, 2]
    assert all(row["provider_account_count"] == 2 for row in anthropic_weekly)
    assert [22.0 + 72.0] not in [row["values"] for row in anthropic_weekly]

    anthropic_five_hour = [
        row
        for row in payload["time_series"]["unified_five_hour"]["series"]
        if row["provider"] == "anthropic"
    ]
    assert [row["label"] for row in anthropic_five_hour] == [
        "Anthropic / Claude · account 1",
        "Anthropic / Claude · account 2",
    ]
    assert [row["values"] for row in anthropic_five_hour] == [[11.0], [61.0]]
    assert [row["provider_account_index"] for row in anthropic_five_hour] == [1, 2]
    assert all(row["provider_account_count"] == 2 for row in anthropic_five_hour)
    assert [11.0 + 61.0] not in [row["values"] for row in anthropic_five_hour]

    serialized = json.dumps(payload)
    for private_value in (
        "primary-private-source",
        "secondary-private-source",
        "claude-code-secondary",
        "account_ref",
        "source_namespace",
        "source_observation_id",
        "/private/",
    ):
        assert private_value not in serialized


def test_single_anthropic_account_preserves_labels_and_card_metadata(
    tmp_path, monkeypatch
):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 5, 12, 30, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)
    ledger = tmp_path / "quota.jsonl"
    ledger.write_text(canonical_json(_quota_row("only-private-source", None, 30)) + "\n")
    client = create_app(quota_ledger=str(ledger)).test_client()

    payload = client.get("/api/subscriptions?hours=1&history=0").get_json()
    anthropic = next(
        row
        for row in payload["time_series"]["unified_weekly"]["series"]
        if row["provider"] == "anthropic"
    )
    assert anthropic["label"] == "Anthropic / Claude"
    assert anthropic["provider_account_index"] == 1
    assert anthropic["provider_account_count"] == 1

    provider = next(
        row
        for row in payload["time_series"]["providers"]
        if row["provider"] == "anthropic"
    )
    assert provider["series"][0]["label"] == "seven day"
    assert provider["series"][0]["provider_account_index"] == 1
    assert provider["series"][0]["provider_account_count"] == 1

    html = client.get("/").get_data(as_text=True)
    assert "subscriptionUnifiedProviderColor" in html
    assert "subscriptionUnifiedQuotaDash" in html
    assert "provider_account_index" in html
    assert "provider_account_count" in html
    assert "borderDash: subscriptionUnifiedQuotaDash(row)" in html


def test_quota_history_removes_provider_small_multiples_and_account_partition_helper(
    tmp_path,
):
    html = create_app(
        atrium_root=str(tmp_path / "atrium"),
        ledger=str(tmp_path / "usage.jsonl"),
    ).test_client().get("/").get_data(as_text=True)

    assert "subscriptionProviderAccountCards" not in html
    assert "renderSubscriptionProviderCharts" not in html
    assert "subscriptionProviderCharts" not in html
    assert "subscriptionProviderChartGrid" not in html
    assert "Provider quota history" not in html
    assert "patterns[(index - 1) % patterns.length]" in html
    assert "const dashPattern = subscriptionUnifiedQuotaDash(row);" in html
    assert "account_ref" not in html
    assert "source_namespace" not in html
    assert "claude-code-secondary" not in html
