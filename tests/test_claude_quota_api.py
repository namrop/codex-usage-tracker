"""Claude OAuth usage API quota collection and its tmux-probe fallback wiring.

The OAuth usage API replaced the tmux ``/usage`` TUI probe as the primary
Claude quota surface (keeper-ruled 2026-08-29). The probe survives only as
the expired-token fallback: launching the real CLI refreshes the token as a
side effect, and that CLI-owned refresh is deliberately the tracker's only
refresh mechanism — the tracker itself must never refresh or POST.
"""
from __future__ import annotations

import json
from pathlib import Path
import time

import httpx
import pytest

from codex_usage_tracker.canonical_ledger import canonical_timestamp
from codex_usage_tracker.claude_instances import ClaudeInstance
import codex_usage_tracker.collector as collector_module
from codex_usage_tracker.collector import collect_all
from codex_usage_tracker.quota import (
    CLAUDE_OAUTH_USAGE_URL,
    ClaudeTokenExpired,
    collect_claude_code_quota_api,
)


def _write_credentials(path: Path, *, expires_in_seconds: float = 3600.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "live-access-token",
                    "refreshToken": "refresh-token-the-tracker-never-touches",
                    "expiresAt": int((time.time() + expires_in_seconds) * 1000),
                    "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_20x",
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _usage_payload() -> dict:
    return {
        "limits": [
            {
                "kind": "session",
                "group": "usage",
                "percent": 34,
                "severity": "ok",
                "resets_at": "2026-08-29T15:00:00-04:00",
                "scope": None,
                "is_active": True,
            },
            {
                "kind": "weekly_all",
                "group": "usage",
                "percent": 47,
                "severity": "ok",
                "resets_at": "2026-09-02T20:00:00-04:00",
                "scope": None,
                "is_active": True,
            },
            {
                "kind": "weekly_scoped",
                "group": "usage",
                "percent": 81,
                "severity": "warning",
                "resets_at": "2026-09-02T20:00:00-04:00",
                "scope": {"model": {"display_name": "Fable"}},
                "is_active": True,
            },
            # Codenamed experimental windows come and go; they must be
            # ignored, never emitted under an invented quota_name.
            {
                "kind": "aurora_window",
                "group": "experimental",
                "percent": 5,
                "severity": "ok",
                "resets_at": "2026-09-02T20:00:00-04:00",
                "scope": None,
                "is_active": False,
            },
        ],
        "five_hour": {"utilization": 34.2, "resets_at": "2026-08-29T15:00:00-04:00"},
        "seven_day": {"utilization": 47.1, "resets_at": "2026-09-02T20:00:00-04:00"},
        "aurora": {"utilization": 5.0, "resets_at": "2026-09-02T20:00:00-04:00"},
        "extra_usage": {"is_enabled": False},
        "spend": {"currency": "USD"},
    }


class _Response:
    def __init__(self, data, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=None, response=None
            )

    def json(self):
        return self._data


def _install_client(monkeypatch, get):
    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers):
            return get(url, headers)

    monkeypatch.setattr("codex_usage_tracker.quota.httpx.Client", Client)


def _collection_paths(tmp_path):
    return {
        "state_db": tmp_path / "missing-state.db",
        "opencode_dbs": [],
        "codex_ledger": tmp_path / "missing-codex.jsonl",
        "usage_ledger": tmp_path / "usage.sqlite3",
        "quota_ledger": tmp_path / "quota.sqlite3",
    }


def _empty_other_collectors(monkeypatch):
    monkeypatch.setattr(collector_module, "collect_hermes_usage", lambda *a, **k: [])
    monkeypatch.setattr(collector_module, "collect_opencode_usage", lambda *a, **k: [])
    monkeypatch.setattr(collector_module, "codex_quota_observations", lambda *a, **k: [])
    monkeypatch.setattr(collector_module, "derive_opencode_go_quotas", lambda *a, **k: [])


def test_api_collector_maps_limits_to_the_historical_percent_series(tmp_path, monkeypatch):
    credentials = _write_credentials(tmp_path / ".credentials.json")
    calls = []

    def get(url, headers):
        calls.append((url, headers))
        return _Response(_usage_payload())

    _install_client(monkeypatch, get)
    rows = collect_claude_code_quota_api(
        "sol:claude-code-secondary-quota",
        credentials_path=credentials,
        account_ref="claude-code-secondary",
        observed_at="2026-08-29T12:00:00Z",
    )

    assert calls == [
        (CLAUDE_OAUTH_USAGE_URL, {"Authorization": "Bearer live-access-token"})
    ]
    by_name = {row["quota_name"]: row for row in rows}
    # The codenamed experimental window is dropped; the scoped weekly window
    # continues the historical seven_day_fable series.
    assert set(by_name) == {"five_hour", "seven_day", "seven_day_fable"}
    assert by_name["five_hour"]["used_value"] == "34"
    assert by_name["five_hour"]["remaining_value"] == "66"
    assert by_name["seven_day"]["used_value"] == "47"
    assert by_name["seven_day_fable"]["used_value"] == "81"
    assert by_name["five_hour"]["resets_at"] == canonical_timestamp(
        "2026-08-29T15:00:00-04:00"
    )
    assert by_name["seven_day"]["resets_at"] == canonical_timestamp(
        "2026-09-02T20:00:00-04:00"
    )
    for row in rows:
        assert row["harness"] == "claude_code"
        assert row["provider"] == "anthropic"
        assert row["unit"] == "percent"
        assert row["limit_value"] == "100"
        assert row["window_kind"] == "rolling"
        assert row["measurement_confidence"] == "exact"
        assert row["account_ref"] == "claude-code-secondary"
        # Provenance marker: makes the API cutover visible in the data next
        # to the probe's ``claude_code_cli_usage`` rows.
        assert row["x_source_surface"] == "claude_oauth_usage_api"
    assert "live-access-token" not in json.dumps(rows)


def test_api_collector_fails_loudly_when_a_required_window_is_missing(
    tmp_path, monkeypatch
):
    credentials = _write_credentials(tmp_path / ".credentials.json")
    payload = _usage_payload()
    payload["limits"] = [
        entry for entry in payload["limits"] if entry["kind"] != "weekly_all"
    ]
    _install_client(monkeypatch, lambda url, headers: _Response(payload))

    with pytest.raises(ValueError, match="weekly_all"):
        collect_claude_code_quota_api(
            "sol:claude-code-quota", credentials_path=credentials
        )


def test_expired_local_token_raises_before_any_network_call(tmp_path, monkeypatch):
    credentials = _write_credentials(
        tmp_path / ".credentials.json", expires_in_seconds=-60.0
    )

    def get(url, headers):
        raise AssertionError("an expired token must never reach the network")

    _install_client(monkeypatch, get)

    with pytest.raises(ClaudeTokenExpired):
        collect_claude_code_quota_api(
            "sol:claude-code-quota", credentials_path=credentials
        )


def test_live_401_raises_the_same_expired_token_signal(tmp_path, monkeypatch):
    credentials = _write_credentials(tmp_path / ".credentials.json")
    _install_client(
        monkeypatch,
        lambda url, headers: _Response(
            {"type": "error", "error": {"type": "authentication_error"}},
            status_code=401,
        ),
    )

    with pytest.raises(ClaudeTokenExpired):
        collect_claude_code_quota_api(
            "sol:claude-code-quota", credentials_path=credentials
        )


def test_one_transient_transport_error_is_retried_then_succeeds(tmp_path, monkeypatch):
    credentials = _write_credentials(tmp_path / ".credentials.json")
    attempts = []

    def get(url, headers):
        attempts.append(url)
        if len(attempts) == 1:
            raise httpx.ConnectError("transient")
        return _Response(_usage_payload())

    _install_client(monkeypatch, get)
    rows = collect_claude_code_quota_api(
        "sol:claude-code-quota", credentials_path=credentials
    )

    assert len(attempts) == 2
    assert {row["quota_name"] for row in rows} == {
        "five_hour",
        "seven_day",
        "seven_day_fable",
    }


def test_a_second_transport_error_propagates_without_more_retries(
    tmp_path, monkeypatch
):
    credentials = _write_credentials(tmp_path / ".credentials.json")
    attempts = []

    def get(url, headers):
        attempts.append(url)
        raise httpx.ConnectError("still down")

    _install_client(monkeypatch, get)

    with pytest.raises(httpx.ConnectError):
        collect_claude_code_quota_api(
            "sol:claude-code-quota", credentials_path=credentials
        )
    assert len(attempts) == 2


def test_instance_credentials_paths_follow_the_config_layout(tmp_path):
    primary = ClaudeInstance("claude-code", tmp_path / "primary")
    secondary = ClaudeInstance("claude-code-secondary", tmp_path / "secondary")

    # The primary account keeps Claude Code's default split layout.
    assert primary.credentials_path == Path("~/.claude/.credentials.json").expanduser()
    assert secondary.credentials_path == tmp_path / "secondary" / ".credentials.json"


def test_collector_falls_back_to_the_tmux_probe_only_on_token_expiry(
    tmp_path, monkeypatch
):
    _empty_other_collectors(monkeypatch)

    def expired_api(namespace, *, credentials_path, account_ref=None, **kwargs):
        raise ClaudeTokenExpired("expired")

    probe_calls = []

    def fake_probe(namespace, **kwargs):
        probe_calls.append((namespace, kwargs))
        return []

    monkeypatch.setattr(collector_module, "collect_claude_code_quota_api", expired_api)
    monkeypatch.setattr(collector_module, "collect_claude_code_quota", fake_probe)

    secondary = tmp_path / "secondary"
    result = collect_all(
        claude_instances=[ClaudeInstance("claude-code-secondary", secondary)],
        claude_quota_command="claude",
        claude_probe_dir=tmp_path / "claude-probe",
        environment={},
        live_quota=True,
        scope="quota",
        **_collection_paths(tmp_path),
    )

    # The probe path receives the full tmux wiring so the CLI launch can
    # refresh the token as a side effect.
    assert probe_calls == [
        (
            "sol:claude-code-secondary-quota",
            {
                "claude_command": "claude",
                "probe_dir": tmp_path / "claude-probe-secondary",
                "timeout": 45.0,
                "claude_config_dir": secondary,
                "account_ref": "claude-code-secondary",
            },
        )
    ]
    assert result["sources"]["claude_code_secondary_quota"] == {"discovered": 0}
    assert not result["warnings"]


def test_collector_cedes_the_lane_on_other_api_errors_without_probing(
    tmp_path, monkeypatch
):
    _empty_other_collectors(monkeypatch)

    def broken_api(namespace, *, credentials_path, account_ref=None, **kwargs):
        raise ValueError("Claude OAuth usage payload omitted weekly_all")

    def forbidden_probe(*args, **kwargs):
        raise AssertionError("only an expired token may fall back to the tmux probe")

    monkeypatch.setattr(collector_module, "collect_claude_code_quota_api", broken_api)
    monkeypatch.setattr(collector_module, "collect_claude_code_quota", forbidden_probe)

    result = collect_all(
        claude_instances=[ClaudeInstance("claude-code-secondary", tmp_path / "secondary")],
        claude_quota_command="claude",
        claude_probe_dir=tmp_path / "claude-probe",
        environment={},
        live_quota=True,
        scope="quota",
        **_collection_paths(tmp_path),
    )

    # The lane retried and ceded through the collector's graceful-cede path;
    # the run itself still completed.
    assert result["sources"]["claude_code_secondary_quota"] == {"discovered": 0}
    assert {"source": "claude_code_secondary_quota", "error": "ValueError"} in result[
        "warnings"
    ]
