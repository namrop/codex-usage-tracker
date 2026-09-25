"""OpenCode Go native quota cutover (keeper acceptance 2026-09-14, Discord
1531875202860974264 message 1549262223074009089: native quota authority,
separate cost vs allowance, invalid-history exclusion).

Contract: ``/tmp/ai-usage-go-native-contract.md``. Three layers:

* the API-key reader for ``GET https://opencode.ai/zen/go/v1/usage`` parses
  the actual response shape defensively and never leaks the key;
* ``collect_all`` prefers the native reader, keeps the cookie reader as the
  only fallback, and never emits the retired first-use USD estimate;
* every operator projection (subscriptions latest/history/time series, burn,
  weekly current/completed) excludes the retired estimated Go rows while the
  raw ledger and the account roster keep them.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json

import httpx
import pytest

import codex_usage_tracker.collector as collector_module
import codex_usage_tracker.dashboard as dashboard_module
import codex_usage_tracker.quota as quota_module
from codex_usage_tracker.collector import collect_all
from codex_usage_tracker.canonical_ledger import read_sqlite_facts
from codex_usage_tracker.dashboard import (
    _build_quota_burn_projection,
    _build_subscription_time_series,
    _is_retired_opencode_go_estimate,
    create_app,
)
from codex_usage_tracker.weekly_utilization import build_weekly_utilization


API_KEY = "sk-private-opencode-go-key"
NAMESPACE = "sol:opencode-go"

# Sanitized copy of the live provider response (/tmp/opencode-go-live-provider-usage.json).
LIVE_PAYLOAD = {
    "usage": {
        "rolling": {"status": "ok", "percent": 0, "resetsAt": "2026-09-15T08:31:21.456Z"},
        "weekly": {"status": "rate-limited", "percent": 100, "resetsAt": "2026-09-21T00:00:00.456Z"},
        "monthly": {"status": "ok", "percent": 78, "resetsAt": "2026-10-08T04:51:16.456Z"},
    },
    "error_type": None,
}


def _payload(**overrides):
    payload = json.loads(json.dumps(LIVE_PAYLOAD))
    for window, updates in overrides.items():
        if updates is None:
            del payload["usage"][window]
        else:
            payload["usage"][window].update(updates)
    return payload


def _install_client(monkeypatch, *, payload=None, status_code=200, raise_transport=None, captured=None):
    class Response:
        def __init__(self):
            self.status_code = status_code

        def raise_for_status(self):
            if status_code >= 400:
                request = httpx.Request("GET", f"https://opencode.ai/zen/go/v1/usage?key={API_KEY}")
                raise httpx.HTTPStatusError(
                    f"{status_code} for key {API_KEY}", request=request, response=httpx.Response(status_code, request=request)
                )

        def json(self):
            if isinstance(payload, Exception):
                raise payload
            return payload

    class Client:
        def __init__(self, *args, **kwargs):
            if captured is not None:
                captured["timeout"] = kwargs.get("timeout")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, headers):
            if captured is not None:
                captured["url"] = url
                captured["headers"] = dict(headers)
            if raise_transport is not None:
                raise raise_transport
            return Response()

    monkeypatch.setattr("codex_usage_tracker.quota.httpx.Client", Client)


# ── Reader ───────────────────────────────────────────────────────────────


def test_native_reader_projects_the_live_response_shape(monkeypatch):
    captured = {}
    _install_client(monkeypatch, payload=LIVE_PAYLOAD, captured=captured)

    rows = quota_module.collect_opencode_go_usage_quota(
        API_KEY, NAMESPACE, observed_at="2026-09-15T03:31:21Z"
    )

    assert captured["url"] == "https://opencode.ai/zen/go/v1/usage"
    assert captured["headers"] == {
        "Accept": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }
    assert captured["timeout"] == 30.0
    by_name = {row["quota_name"]: row for row in rows}
    assert list(by_name) == ["five_hour", "week", "month"]
    for row in rows:
        assert row["source_namespace"] == NAMESPACE
        assert row["provider"] == "opencode-go"
        assert row["harness"] == "opencode_go_api"
        assert row["unit"] == "percent"
        assert row["limit_value"] == "100"
        assert row["measurement_confidence"] == "exact"
        assert row["x_source_surface"] == "opencode_go_usage_api"
        assert row["x_percent_resolution"] == 1
        assert row["account_ref"] is None
        assert row["window_started_at"] is None
    five, week, month = by_name["five_hour"], by_name["week"], by_name["month"]
    assert (five["used_value"], five["remaining_value"]) == ("0", "100")
    assert (week["used_value"], week["remaining_value"]) == ("100", "0")
    assert (month["used_value"], month["remaining_value"]) == ("78", "22")
    assert five["x_provider_state"] == "ok"
    assert week["x_provider_state"] == "rate_limited"
    assert month["x_provider_state"] == "ok"
    # resets_at is the provider's absolute timestamp, not observed + seconds.
    assert five["resets_at"] == "2026-09-15T08:31:21.456Z"
    assert week["resets_at"] == "2026-09-21T00:00:00.456Z"
    assert month["resets_at"] == "2026-10-08T04:51:16.456Z"
    # Window kinds and bounds: the calendar windows end at their reset; the
    # rolling window claims no bound (an idle account reads now + 5h).
    assert (five["window_kind"], five["window_ends_at"]) == ("rolling", None)
    assert (week["window_kind"], week["window_ends_at"]) == ("fixed", week["resets_at"])
    assert (month["window_kind"], month["window_ends_at"]) == ("fixed", month["resets_at"])
    assert [row["source_observation_id"] for row in rows] == [
        "2026-09-15T03:31:21Z:five_hour",
        "2026-09-15T03:31:21Z:week",
        "2026-09-15T03:31:21Z:month",
    ]
    serialized = json.dumps(rows)
    assert API_KEY not in serialized
    assert "opencode.ai" not in serialized


def test_native_reader_shares_the_cookie_readers_series_identity(monkeypatch):
    """One account, one series per window across the cookie → native cutover:
    the canonical identity fields match and only the surface tag differs."""
    _install_client(monkeypatch, payload=LIVE_PAYLOAD)
    native = quota_module.collect_opencode_go_usage_quota(API_KEY, NAMESPACE, observed_at="2026-09-15T03:31:21Z")

    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {
                "rollingUsage": {"usagePercent": 1, "resetInSec": 10},
                "weeklyUsage": {"usagePercent": 2, "resetInSec": 20},
                "monthlyUsage": {"usagePercent": 3, "resetInSec": 30},
            }

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, params, headers): return Response()

    monkeypatch.setattr("codex_usage_tracker.quota.httpx.Client", Client)
    cookie = quota_module.collect_opencode_go_quota("ws", "cookie", NAMESPACE, observed_at="2026-09-15T02:31:21Z")

    identity = dashboard_module._quota_series_identity
    assert [identity(row) for row in native] == [identity(row) for row in cookie]
    assert {row["x_source_surface"] for row in cookie} == {"opencode_go_server_function"}
    assert {row["x_source_surface"] for row in native} == {"opencode_go_usage_api"}


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"weekly": None}, "week window is missing"),
        ({"rolling": {"percent": True}}, "five_hour percent is malformed"),
        ({"rolling": {"percent": "12"}}, "five_hour percent is malformed"),
        ({"rolling": {"percent": float("nan")}}, "five_hour percent is malformed"),
        ({"rolling": {"percent": float("inf")}}, "five_hour percent is malformed"),
        ({"rolling": {"percent": None}}, "five_hour percent is malformed"),
        ({"monthly": {"percent": 101}}, "month percent is outside"),
        ({"monthly": {"percent": -1}}, "month percent is outside"),
        # The server floors to whole points; a fraction is not this contract.
        ({"monthly": {"percent": 78.5}}, "month percent is not a whole"),
        ({"weekly": {"status": "throttled"}}, "week status is malformed"),
        ({"weekly": {"status": None}}, "week status is malformed"),
        ({"weekly": {"status": True}}, "week status is malformed"),
        ({"weekly": {"resetsAt": "next monday"}}, "week resetsAt is malformed"),
        ({"weekly": {"resetsAt": 1790000000}}, "week resetsAt is malformed"),
        ({"weekly": {"resetsAt": None}}, "week resetsAt is malformed"),
        ({"weekly": {"resetsAt": "2026-09-21T00:00:00"}}, "week resetsAt is malformed"),
    ],
)
def test_native_reader_rejects_malformed_windows_without_leaking_the_key(monkeypatch, overrides, match):
    _install_client(monkeypatch, payload=_payload(**overrides))
    with pytest.raises(ValueError, match=f"OpenCode Go {match}") as error:
        quota_module.collect_opencode_go_usage_quota(API_KEY, NAMESPACE)
    assert API_KEY not in str(error.value)


@pytest.mark.parametrize(
    "payload",
    [[], "usage", {"usage": []}, {"usage": None}, {"usage": {"rolling": 5, "weekly": {}, "monthly": {}}}],
    ids=["list", "string", "usage-list", "usage-null", "window-scalar"],
)
def test_native_reader_rejects_non_object_payloads(monkeypatch, payload):
    _install_client(monkeypatch, payload=payload)
    with pytest.raises(ValueError, match="OpenCode Go") as error:
        quota_module.collect_opencode_go_usage_quota(API_KEY, NAMESPACE)
    assert API_KEY not in str(error.value)


@pytest.mark.parametrize("status_code", [401, 403, 500])
def test_native_reader_sanitizes_http_errors(monkeypatch, status_code):
    _install_client(monkeypatch, payload=LIVE_PAYLOAD, status_code=status_code)
    with pytest.raises(ValueError, match="OpenCode Go usage request failed") as error:
        quota_module.collect_opencode_go_usage_quota(API_KEY, NAMESPACE)
    message = str(error.value)
    assert API_KEY not in message
    assert "opencode.ai" not in message
    assert error.value.__cause__ is None


def test_native_reader_sanitizes_transport_and_decode_errors(monkeypatch):
    _install_client(
        monkeypatch,
        raise_transport=httpx.ConnectError(f"dns failed for {API_KEY}", request=httpx.Request("GET", "https://opencode.ai")),
    )
    with pytest.raises(ValueError, match="OpenCode Go usage request failed") as error:
        quota_module.collect_opencode_go_usage_quota(API_KEY, NAMESPACE)
    assert API_KEY not in str(error.value)

    _install_client(monkeypatch, payload=json.JSONDecodeError(f"bad {API_KEY}", "x", 0))
    with pytest.raises(ValueError, match="OpenCode Go usage request failed") as error:
        quota_module.collect_opencode_go_usage_quota(API_KEY, NAMESPACE)
    assert API_KEY not in str(error.value)


# ── Collector ────────────────────────────────────────────────────────────


def _quota_kwargs(tmp_path, **overrides):
    kwargs = dict(
        state_db=tmp_path / "missing-state.db",
        claude_root=tmp_path / "missing-claude",
        opencode_dbs=[tmp_path / "must-not-open.db"],
        codex_ledger=tmp_path / "missing-codex.jsonl",
        usage_ledger=tmp_path / "out-of-scope-usage.sqlite3",
        quota_ledger=tmp_path / "quota.sqlite3",
        claude_quota_command=None,
        live_quota=True,
        scope="quota",
    )
    kwargs.update(overrides)
    return kwargs


def _go_rows(quota):
    return [
        row
        for row in read_sqlite_facts(quota, fact_type="quota_observation_v1")
        if row["provider"] == "opencode-go"
    ]


def _forbid(name):
    def forbidden(*args, **kwargs):
        raise AssertionError(f"{name} must not run")

    return forbidden


def test_dotenv_allowlist_admits_the_native_key(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"OPENCODE_GO_API_KEY={API_KEY}\nUNRELATED=must-not-load\n", encoding="utf-8")
    assert collector_module.load_allowed_dotenv(dotenv, environ={}) == {"OPENCODE_GO_API_KEY": API_KEY}
    assert "OPENCODE_GO_API_KEY" in collector_module.ALLOWED_DOTENV_KEYS


def test_collect_all_prefers_the_native_reader_over_the_cookie_reader(tmp_path, monkeypatch):
    captured = {}

    def native(api_key, namespace, **kwargs):
        captured.update(api_key=api_key, namespace=namespace)
        _install_client(monkeypatch, payload=LIVE_PAYLOAD)
        return quota_module.collect_opencode_go_usage_quota(api_key, namespace, observed_at="2026-09-15T03:31:21Z")

    monkeypatch.setattr(collector_module, "collect_opencode_go_usage_quota", native)
    monkeypatch.setattr(collector_module, "collect_exact_opencode_go_quota", _forbid("cookie reader"))
    monkeypatch.setattr(collector_module, "collect_opencode_usage", _forbid("opencode usage read"))
    monkeypatch.setattr(collector_module, "codex_quota_observations", lambda *a, **k: [])
    kwargs = _quota_kwargs(tmp_path)

    result = collect_all(
        **kwargs,
        environment={
            "OPENCODE_GO_API_KEY": API_KEY,
            "OPENCODE_GO_WORKSPACE_ID": "private-workspace",
            "OPENCODE_GO_AUTH_COOKIE": "private-cookie",
        },
    )

    assert captured == {"api_key": API_KEY, "namespace": "sol:opencode-go"}
    assert result["sources"]["opencode_go_quota"] == {"discovered": 3}
    assert result["warnings"] == []
    assert result["lanes"]["opencode_go_quota"]["succeeded"] is True
    assert "opencode" not in result["lanes"]
    rows = _go_rows(kwargs["quota_ledger"])
    assert [row["quota_name"] for row in rows] == ["five_hour", "week", "month"]
    assert {row["source_namespace"] for row in rows} == {"sol:opencode-go"}
    assert {row["x_source_surface"] for row in rows} == {"opencode_go_usage_api"}
    serialized = json.dumps({"result": result, "rows": rows})
    for secret in (API_KEY, "private-workspace", "private-cookie"):
        assert secret not in serialized


@pytest.mark.parametrize(
    "failure",
    [ValueError("OpenCode Go usage request failed"), RuntimeError(f"boom {API_KEY}")],
    ids=["sanitized", "unexpected"],
)
def test_collect_all_cedes_the_native_lane_without_any_estimate_or_cookie_fallback(tmp_path, monkeypatch, failure):
    calls = []
    slept = []

    def native(*args, **kwargs):
        calls.append(1)
        raise failure

    monkeypatch.setattr(collector_module, "collect_opencode_go_usage_quota", native)
    monkeypatch.setattr(collector_module, "collect_exact_opencode_go_quota", _forbid("cookie reader"))
    monkeypatch.setattr(collector_module, "collect_opencode_usage", _forbid("opencode usage read"))
    monkeypatch.setattr(collector_module, "codex_quota_observations", lambda *a, **k: [])
    kwargs = _quota_kwargs(tmp_path)

    result = collect_all(
        **kwargs,
        environment={
            "OPENCODE_GO_API_KEY": API_KEY,
            "OPENCODE_GO_WORKSPACE_ID": "private-workspace",
            "OPENCODE_GO_AUTH_COOKIE": "private-cookie",
        },
        lane_sleep=slept.append,
    )

    assert len(calls) == collector_module.LANE_ATTEMPTS and len(slept) == collector_module.LANE_ATTEMPTS - 1
    assert result["ceded"] == ["opencode_go_quota"]
    assert result["warnings"] == [{"source": "opencode_go_quota", "error": type(failure).__name__}]
    assert result["sources"]["opencode_go_quota"] == {"discovered": 0}
    assert "opencode_go_quota_estimated" not in result["sources"]
    assert _go_rows(kwargs["quota_ledger"]) == []
    assert API_KEY not in json.dumps(result)
    # The run still completes and the heartbeat is honest.
    assert (tmp_path / "health" / "quota-collect.ok").exists()


def test_collect_all_retains_last_good_native_rows_when_the_lane_cedes(tmp_path, monkeypatch):
    monkeypatch.setattr(collector_module, "codex_quota_observations", lambda *a, **k: [])
    monkeypatch.setattr(collector_module, "collect_opencode_usage", _forbid("opencode usage read"))
    kwargs = _quota_kwargs(tmp_path)
    environment = {"OPENCODE_GO_API_KEY": API_KEY}

    def good(api_key, namespace, **_):
        _install_client(monkeypatch, payload=LIVE_PAYLOAD)
        return quota_module.collect_opencode_go_usage_quota(api_key, namespace, observed_at="2026-09-15T03:31:21Z")

    monkeypatch.setattr(collector_module, "collect_opencode_go_usage_quota", good)
    collect_all(**kwargs, environment=environment)
    before = _go_rows(kwargs["quota_ledger"])
    assert len(before) == 3

    def bad(*args, **kwargs):
        raise ValueError("OpenCode Go usage request failed")

    monkeypatch.setattr(collector_module, "collect_opencode_go_usage_quota", bad)
    result = collect_all(**kwargs, environment=environment, lane_sleep=lambda _: None)

    assert result["ceded"] == ["opencode_go_quota"]
    assert _go_rows(kwargs["quota_ledger"]) == before
    lanes = json.loads((tmp_path / "health" / "lanes-quota.json").read_text(encoding="utf-8"))
    assert lanes["lanes"]["opencode_go_quota"]["ceded"] is True
    assert lanes["lanes"]["opencode_go_quota"]["last_success_at"] is not None


def test_collect_all_keeps_the_cookie_reader_only_without_a_native_key(tmp_path, monkeypatch):
    captured = {}

    def cookie(workspace, auth_cookie, namespace, **kwargs):
        captured.update(workspace=workspace, cookie=auth_cookie, namespace=namespace)
        return [
            quota_module._quota(
                namespace=namespace,
                observation_id=f"cookie:{name}",
                harness="opencode_go_api",
                observed_at="2026-09-15T03:31:21Z",
                provider="opencode-go",
                quota_name=name,
                window_kind="rolling" if name == "five_hour" else "fixed",
                unit="percent",
                limit=100,
                used=25,
                remaining=75,
                x_source_surface="opencode_go_server_function",
            )
            for name in ("five_hour", "week", "month")
        ]

    monkeypatch.setattr(collector_module, "collect_exact_opencode_go_quota", cookie)
    monkeypatch.setattr(collector_module, "collect_opencode_go_usage_quota", _forbid("native reader"))
    monkeypatch.setattr(collector_module, "collect_opencode_usage", _forbid("opencode usage read"))
    monkeypatch.setattr(collector_module, "codex_quota_observations", lambda *a, **k: [])
    kwargs = _quota_kwargs(tmp_path)

    result = collect_all(
        **kwargs,
        environment={"OPENCODE_GO_WORKSPACE_ID": "private-workspace", "OPENCODE_GO_AUTH_COOKIE": "private-cookie"},
    )

    assert captured == {"workspace": "private-workspace", "cookie": "private-cookie", "namespace": "sol:opencode-go"}
    assert result["sources"]["opencode_go_quota"] == {"discovered": 3}
    assert result["warnings"] == []
    assert len(_go_rows(kwargs["quota_ledger"])) == 3


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"OPENCODE_GO_WORKSPACE_ID": "partial-private-workspace"},
        {"OPENCODE_GO_AUTH_COOKIE": "partial-private-cookie"},
        {"OPENCODE_GO_API_KEY": ""},
    ],
    ids=["none", "workspace-only", "cookie-only", "empty-key"],
)
def test_collect_all_reports_go_unavailable_without_credentials_instead_of_estimating(tmp_path, monkeypatch, environment):
    monkeypatch.setattr(collector_module, "collect_opencode_go_usage_quota", _forbid("native reader"))
    monkeypatch.setattr(collector_module, "collect_exact_opencode_go_quota", _forbid("cookie reader"))
    monkeypatch.setattr(collector_module, "collect_opencode_usage", _forbid("opencode usage read"))
    monkeypatch.setattr(collector_module, "codex_quota_observations", lambda *a, **k: [])
    kwargs = _quota_kwargs(tmp_path)

    result = collect_all(**kwargs, environment=environment)

    assert result["sources"]["opencode_go_quota"] == {"discovered": 0}
    assert "opencode_go_quota_estimated" not in result["sources"]
    assert result["warnings"] == []
    assert result["ceded"] == []
    assert "opencode_go_quota" not in result["lanes"]
    assert "opencode" not in result["lanes"]
    assert _go_rows(kwargs["quota_ledger"]) == []
    assert "partial-private" not in json.dumps(result)
    assert not (tmp_path / "out-of-scope-usage.sqlite3").exists()


def test_quota_scope_no_longer_reads_opencode_usage_or_the_usage_ledger(tmp_path, monkeypatch):
    """Scope isolation: the estimate was the only reason the quota scope
    opened OpenCode databases and the usage ledger. Neither is touched now."""
    monkeypatch.setattr(collector_module, "collect_opencode_usage", _forbid("opencode usage read"))
    monkeypatch.setattr(collector_module, "codex_quota_observations", lambda *a, **k: [])
    kwargs = _quota_kwargs(tmp_path, live_quota=False)

    result = collect_all(**kwargs, environment={"OPENCODE_GO_API_KEY": API_KEY})

    assert result["sources"]["opencode_go_quota"] == {"discovered": 0}
    assert result["warnings"] == []
    assert not (tmp_path / "out-of-scope-usage.sqlite3").exists()
    lanes = json.loads((tmp_path / "health" / "lanes-quota.json").read_text(encoding="utf-8"))
    assert "opencode" not in lanes["lanes"]
    assert not hasattr(collector_module, "derive_opencode_go_quotas")
    assert not hasattr(collector_module, "_opencode_go_estimate_inputs")


# ── Projections ──────────────────────────────────────────────────────────


NOW = datetime(2026, 9, 15, 4, 0, 0, tzinfo=timezone.utc)


class _FrozenNow(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 15, 4, 0, 0, tzinfo=tz)


def _row(source_id, observed_at, **updates):
    row = {
        "fact_type": "quota_observation_v1",
        "schema_version": 1,
        "source_namespace": "sol:opencode-go",
        "source_observation_id": source_id,
        "harness": "opencode_go_api",
        "observed_at": observed_at,
        "provider": "opencode-go",
        "quota_name": "week",
        "quota_scope": "account",
        "window_kind": "fixed",
        "unit": "percent",
        "measurement_confidence": "exact",
        "limit_value": "100",
        "used_value": "40",
        "remaining_value": "60",
        "resets_at": "2026-09-21T00:00:00.456Z",
        "window_started_at": None,
        "window_ends_at": "2026-09-21T00:00:00.456Z",
        "account_ref": None,
        "provider_payload_ref": None,
    }
    row.update(updates)
    return row


def _estimate(source_id, observed_at, used="8.03", quota_name="week", **updates):
    """The retired first-use USD replay row exactly as the ledger holds it."""
    values = dict(
        harness="opencode",
        quota_name=quota_name,
        window_kind="rolling",
        unit="usd",
        measurement_confidence="estimated",
        limit_value="30",
        used_value=used,
        remaining_value=str(30 - float(used)),
        resets_at="2026-09-19T10:00:00Z",
        window_started_at="2026-09-12T10:00:00Z",
        window_ends_at="2026-09-19T10:00:00Z",
        x_coverage={"status": "partial", "anchor": "first_use_estimate", "valued_events": 3},
    )
    values.update(updates)
    return _row(source_id, observed_at, **values)


def _native(source_id, observed_at, used=100, quota_name="week", state="rate_limited", **updates):
    values = dict(
        quota_name=quota_name,
        used_value=str(used),
        remaining_value=str(100 - used),
        x_source_surface="opencode_go_usage_api",
        x_percent_resolution=1,
        x_provider_state=state,
    )
    values.update(updates)
    return _row(source_id, observed_at, **values)


def _cookie(source_id, observed_at, used=25, quota_name="week", **updates):
    values = dict(
        quota_name=quota_name,
        used_value=str(used),
        remaining_value=str(100 - used),
        x_source_surface="opencode_go_server_function",
    )
    values.update(updates)
    return _row(source_id, observed_at, **values)


def _claude(source_id, observed_at, used):
    return _row(
        source_id,
        observed_at,
        source_namespace="sol:claude-code-quota",
        harness="claude_code",
        provider="anthropic",
        quota_name="seven_day",
        window_kind="rolling",
        used_value=str(used),
        remaining_value=str(100 - used),
        resets_at="2026-09-18T00:00:00Z",
        window_ends_at=None,
        x_source_surface="claude_oauth_usage_api",
    )


def _client(tmp_path, rows, monkeypatch):
    monkeypatch.setattr(dashboard_module, "datetime", _FrozenNow)
    ledger = tmp_path / "quota.jsonl"
    ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return create_app(quota_ledger=str(ledger)).test_client()


def test_retired_estimate_predicate_is_provider_scoped_and_explicit():
    assert _is_retired_opencode_go_estimate(_estimate("e", "2026-09-15T03:00:00Z"))
    # Either marker alone is enough: the unweighted USD unit, or the estimated label.
    assert _is_retired_opencode_go_estimate(_row("u", "2026-09-15T03:00:00Z", unit="usd"))
    assert _is_retired_opencode_go_estimate(_row("c", "2026-09-15T03:00:00Z", measurement_confidence="estimated"))
    # Legitimate provider observations of either surface stay.
    assert not _is_retired_opencode_go_estimate(_native("n", "2026-09-15T03:00:00Z"))
    assert not _is_retired_opencode_go_estimate(_cookie("k", "2026-09-15T03:00:00Z"))
    # Other providers' estimates are untouched by this predicate.
    assert not _is_retired_opencode_go_estimate(
        _claude("a", "2026-09-15T03:00:00Z", 10) | {"measurement_confidence": "estimated"}
    )
    assert not _is_retired_opencode_go_estimate(_claude("b", "2026-09-15T03:00:00Z", 10) | {"unit": "usd"})


def test_subscriptions_exclude_retired_go_estimates_from_every_cut_but_keep_the_roster(tmp_path, monkeypatch):
    rows = [
        _estimate("e-week", "2026-09-15T03:45:00Z"),
        _estimate("e-five", "2026-09-15T03:45:00Z", quota_name="five_hour", used="0"),
        _estimate("e-old", "2026-09-14T05:00:00Z", used="6"),
        _claude("c1", "2026-09-15T03:30:00Z", 55),
    ]
    client = _client(tmp_path, rows, monkeypatch)

    payload = client.get("/api/subscriptions?hours=24").get_json()

    assert [row["provider_canonical"] for row in payload["latest"]] == ["anthropic"]
    assert all(row["provider_canonical"] != "opencode-go" for row in payload["observations"])
    assert payload["exclusions"] == {"retired_opencode_go_estimate_rows": 3}
    chart = payload["time_series"]
    go = next(entry for entry in chart["providers"] if entry["provider"] == "opencode-go")
    assert (go["status"], go["series"], go["unavailable_series"], go["observation_count"]) == (
        "no_observations", [], [], 0
    )
    assert {"provider": "opencode-go", "label": "OpenCode Go", "quota_name": "week", "status": "primary_weekly_quota_not_reported"} in chart["unified_weekly"]["no_data_providers"]
    assert "8.03" not in json.dumps(payload)
    # Account presence survives for the roster every surface shares.
    burn = client.get("/api/quota-burn?hours=24").get_json()
    assert [series["provider"] for series in burn["series"]] == ["anthropic"]
    assert burn["exclusions"] == {"retired_opencode_go_estimate_rows": 3}
    # Weekly allowance use leaves Go out entirely (keeper msg 1553002864555593851).
    weekly = client.get("/api/weekly-utilization?weeks=2").get_json()
    assert all(window["provider"] != "opencode-go" for window in weekly["windows"])
    assert weekly["coverage"]["omitted_provider_rows"] == 3


def test_subscriptions_project_native_metadata_and_keep_cookie_history_in_one_series(tmp_path, monkeypatch):
    rows = [
        _cookie("k1", "2026-09-15T02:00:00Z", used=25),
        _estimate("e1", "2026-09-15T02:30:00Z"),
        _native("n1", "2026-09-15T03:00:00Z", used=100),
        _native("n1-five", "2026-09-15T03:00:00Z", used=0, quota_name="five_hour", state="ok", window_kind="rolling", window_ends_at=None, resets_at="2026-09-15T08:31:21.456Z"),
    ]
    client = _client(tmp_path, rows, monkeypatch)

    payload = client.get("/api/subscriptions?hours=3").get_json()

    latest = {row["quota_name"]: row for row in payload["latest"]}
    assert set(latest) == {"week", "five_hour"}
    week = latest["week"]
    assert (week["used_value"], week["provider_state"], week["source_surface"], week["percent_resolution"]) == (
        "100", "rate_limited", "opencode_go_usage_api", 1
    )
    assert (week["provider_account_index"], week["provider_account_count"]) == (1, 1)
    assert latest["five_hour"]["provider_state"] == "ok"
    # hours= windows carry no history; the unbounded read does.
    unbounded = client.get("/api/subscriptions?days=1").get_json()
    assert unbounded["exclusions"] == {"retired_opencode_go_estimate_rows": 1}
    history = [row for row in unbounded["observations"] if row["quota_name"] == "week"]
    assert [(row["used_value"], row["source_surface"], row["percent_resolution"]) for row in history] == [
        ("100", "opencode_go_usage_api", 1),
        ("25", "opencode_go_server_function", None),
    ]
    go = next(entry for entry in payload["time_series"]["providers"] if entry["provider"] == "opencode-go")
    assert [series["quota_name"] for series in go["series"]] == ["five_hour", "week"]
    week_series = next(series for series in go["series"] if series["quota_name"] == "week")
    # Cookie then native readings are one account, one series: no
    # "· opencode_go_api · account 1" split and one unified weekly entry.
    assert week_series["label"] == "week"
    assert week_series["sample_count"] == 2
    # hours=3 from 01:00: the 02:00 cookie sample lands in bucket 4, the
    # 03:00 native sample in bucket 8.
    assert week_series["values"] == [None] * 4 + [25.0] + [None] * 3 + [100.0] + [None] * 3
    assert (week_series["source_surface"], week_series["percent_resolution"], week_series["provider_state"]) == (
        "opencode_go_usage_api", 1, "rate_limited"
    )
    unified = [entry for entry in payload["time_series"]["unified_weekly"]["series"] if entry["provider"] == "opencode-go"]
    assert len(unified) == 1
    assert payload["exclusions"] == {"retired_opencode_go_estimate_rows": 1}
    serialized = json.dumps(payload)
    assert "8.03" not in serialized and "first_use_estimate" not in serialized
    assert "x_source_surface" not in serialized and "x_percent_resolution" not in serialized


def test_subscriptions_only_pass_allowlisted_metadata(tmp_path, monkeypatch):
    rows = [
        _native(
            "odd",
            "2026-09-15T03:00:00Z",
            x_source_surface="/private/path/surface",
            x_percent_resolution=True,
            x_provider_state="operator_supplied",
        ),
        _native("bad-res", "2026-09-15T03:00:00Z", quota_name="five_hour", x_percent_resolution="1"),
    ]
    client = _client(tmp_path, rows, monkeypatch)
    latest = {row["quota_name"]: row for row in client.get("/api/subscriptions?hours=3").get_json()["latest"]}
    assert (latest["week"]["source_surface"], latest["week"]["percent_resolution"], latest["week"]["provider_state"]) == (None, None, None)
    assert latest["five_hour"]["percent_resolution"] is None
    assert "/private/path" not in json.dumps(latest)


def test_burn_uses_only_native_whole_point_samples(tmp_path, monkeypatch):
    rows = [
        _estimate("e1", "2026-09-15T02:00:00Z", quota_name="five_hour", used="1"),
        _estimate("e2", "2026-09-15T03:00:00Z", quota_name="five_hour", used="5"),
        _native("n1", "2026-09-15T02:30:00Z", used=10, quota_name="five_hour", state="ok", window_kind="rolling", window_ends_at=None, resets_at="2026-09-15T07:00:00Z"),
        _native("n2", "2026-09-15T03:30:00Z", used=30, quota_name="five_hour", state="ok", window_kind="rolling", window_ends_at=None, resets_at="2026-09-15T07:00:00Z"),
    ]
    client = _client(tmp_path, rows, monkeypatch)

    payload = client.get("/api/quota-burn?hours=3").get_json()

    [series] = payload["series"]
    assert (series["provider"], series["quota_name"], series["harness"], series["unit"]) == ("opencode-go", "five_hour", "opencode_go_api", "percent")
    assert series["observation_count"] == 2
    assert [obs["used_pct"] for obs in series["observations"]] == [10.0, 30.0]
    assert series["burn_rate_pct_per_hour"] == pytest.approx(20.0)
    assert series["projection_status"] == "projected_before_reset"
    assert (series["source_surface"], series["percent_resolution"], series["provider_state"]) == ("opencode_go_usage_api", 1, "ok")
    assert payload["exclusions"] == {"retired_opencode_go_estimate_rows": 2}
    assert "usd" not in json.dumps(payload)


def test_direct_builders_cannot_smuggle_retired_go_estimates():
    retired = [
        _estimate("e1", "2026-09-15T02:00:00Z"),
        _estimate("e2", "2026-09-15T03:00:00Z", used="9"),
        _estimate("e3", "2026-09-15T03:00:00Z", quota_name="five_hour", used="0"),
    ]
    chart = _build_subscription_time_series(
        retired,
        window_start=datetime(2026, 9, 15, 1, tzinfo=timezone.utc),
        window_end=datetime(2026, 9, 15, 4, tzinfo=timezone.utc),
        generated_at=NOW,
    )
    go = next(entry for entry in chart["providers"] if entry["provider"] == "opencode-go")
    assert (go["status"], go["observation_count"]) == ("no_observations", 0)
    assert chart["truncation"]["series_total"] == 0

    burn = _build_quota_burn_projection(retired, generated_at=NOW)
    assert burn["series"] == []

    roster = {"opencode-go": {(False, ""): 1}}
    weekly = build_weekly_utilization(retired, generated_at=NOW, weeks=4, account_indexes=roster)
    assert weekly["windows"] == []
    assert weekly["coverage"]["omitted_provider_rows"] == 3
    assert weekly["coverage"]["source_rows"] == 0
    assert "8.03" not in json.dumps(weekly) and "first_use" not in json.dumps(weekly)


def test_weekly_allowance_use_leaves_opencode_go_out(tmp_path, monkeypatch):
    """Keeper, 2026-09-25, Discord unified usage ledger thread msg
    1553002864555593851: "I think opencode go should be omitted from the
    weekly allowance use calculations. It's weird enough. Right now it says
    that I've only used 47% when it's actually capped." Go's weekly percent
    read 42 while its monthly limit was at 100 (rate_limited). No Go window,
    no Go share of any average, whatever its readings say."""
    rows = [
        _estimate("e1", "2026-09-05T09:00:00Z", used="8", resets_at="2026-09-05T10:00:00Z", window_ends_at="2026-09-05T10:00:00Z"),
        _estimate("e2", "2026-09-12T09:00:00Z", used="8", resets_at="2026-09-12T10:00:00Z", window_ends_at="2026-09-12T10:00:00Z"),
        _estimate("e3", "2026-09-15T03:45:00Z", used="7.53"),
        _cookie("k1", "2026-09-15T02:00:00Z", used=25),
        _native("n1", "2026-09-15T03:00:00Z", used=42, state="ok"),
        _native("n2", "2026-09-15T03:30:00Z", used=100, quota_name="month"),
        _claude("c1", "2026-09-15T03:30:00Z", 50),
    ]
    client = _client(tmp_path, rows, monkeypatch)

    payload = client.get("/api/weekly-utilization?weeks=4").get_json()

    assert [window["provider"] for window in payload["windows"]] == ["anthropic"]
    for summary in (payload["current"], payload["completed"]):
        assert all(entry["provider"] != "opencode-go" for entry in summary["providers"])
    assert (payload["current"]["used_pct"], payload["current"]["account_count"]) == ("50.0", 1)
    assert payload["completed"]["excluded_account_count"] == 1
    assert payload["coverage"]["omitted_providers"] == ["opencode-go"]
    assert payload["coverage"]["omitted_provider_rows"] == 6
    go_notes = [note for note in payload["notes"] if "OpenCode Go" in note]
    assert len(go_notes) == 1 and "left out" in go_notes[0]
    assert "each of the 1 tracked primary weekly accounts" in payload["notes"][0]
    serialized = json.dumps(payload)
    assert "42.0" not in serialized and "7.53" not in serialized and "first_use" not in serialized
    assert "x_" not in serialized and "sol:" not in serialized
    # Go stays on every other surface: its subscriptions rows are untouched.
    subs = client.get("/api/subscriptions?hours=24").get_json()
    assert {row["quota_name"] for row in subs["latest"] if row["provider_canonical"] == "opencode-go"} == {"week", "month"}


def test_weekly_go_note_appears_only_when_go_is_tracked_or_excluded():
    roster = {"anthropic": {(False, ""): 1}}
    payload = build_weekly_utilization(
        [_claude("c1", "2026-09-15T03:30:00Z", 50)], generated_at=NOW, weeks=4, account_indexes=roster
    )
    assert not any("OpenCode Go" in note for note in payload["notes"])
    assert payload["coverage"]["omitted_provider_rows"] == 0
    assert "source_surface" in payload["windows"][0]["current"]
    assert payload["windows"][0]["current"]["source_surface"] == "claude_oauth_usage_api"
    assert payload["windows"][0]["current"]["percent_resolution"] is None
