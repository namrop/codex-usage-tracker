"""Independent-review regressions for the OpenCode Go native cutover
(2026-09-15): three bounded backend corrections.

1. ``--quota-lanes`` gates the Go lane like every other lane: an unselected
   ``opencode_go_quota`` reports zero rows and never probes either reader.
2. ``/api/opencode-go-usage`` is read-only in fact, not only in its
   docstring: JSONL reads take the side-effect-free snapshot (no chmod, no
   ``.lock``), and a non-regular ledger path is a JSON 503, not an HTML 500.
3. Burn rate after the cookie → native source cutover is measured only
   within the contiguous native epoch; cookie history and current values
   are preserved everywhere else.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import stat

import pytest

import codex_usage_tracker.collector as collector_module
import codex_usage_tracker.dashboard as dashboard_module
import codex_usage_tracker.quota as quota_module
from codex_usage_tracker.canonical_ledger import append_sqlite_facts, canonical_json, normalize_fact, read_sqlite_facts
from codex_usage_tracker.collector import collect_all
from codex_usage_tracker.dashboard import _build_quota_burn_projection, create_app


API_KEY = "sk-private-opencode-go-key"


# ── 1. quota_lanes gates the Go lane ─────────────────────────────────────


def _quota_kwargs(tmp_path):
    return dict(
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


def _go_rows(quota):
    return [row for row in read_sqlite_facts(quota, fact_type="quota_observation_v1") if row["provider"] == "opencode-go"]


def _forbid(name):
    def forbidden(*args, **kwargs):
        raise AssertionError(f"{name} must not run")

    return forbidden


def _go_reading(namespace, surface):
    return [
        quota_module._quota(
            namespace=namespace,
            observation_id=f"{surface}:{name}",
            harness="opencode_go_api",
            observed_at="2026-09-15T03:31:21Z",
            provider="opencode-go",
            quota_name=name,
            window_kind="rolling" if name == "five_hour" else "fixed",
            unit="percent",
            limit=100,
            used=25,
            remaining=75,
            x_source_surface=surface,
        )
        for name in ("five_hour", "week", "month")
    ]


NATIVE_ENV = {"OPENCODE_GO_API_KEY": API_KEY}
COOKIE_ENV = {"OPENCODE_GO_WORKSPACE_ID": "private-workspace", "OPENCODE_GO_AUTH_COOKIE": "private-cookie"}


@pytest.mark.parametrize("environment", [NATIVE_ENV, COOKIE_ENV], ids=["native", "cookie"])
def test_unselected_go_lane_never_probes_either_reader(tmp_path, monkeypatch, environment):
    monkeypatch.setattr(collector_module, "collect_opencode_go_usage_quota", _forbid("native reader"))
    monkeypatch.setattr(collector_module, "collect_exact_opencode_go_quota", _forbid("cookie reader"))
    monkeypatch.setattr(collector_module, "collect_opencode_usage", _forbid("opencode usage read"))
    monkeypatch.setattr(collector_module, "codex_quota_observations", lambda *a, **k: [])
    kwargs = _quota_kwargs(tmp_path)

    result = collect_all(**kwargs, environment=environment, quota_lanes=["claude_code_quota"])

    # Stand-down convention: reported as zero rows, neither ceded nor warned.
    assert result["sources"]["opencode_go_quota"] == {"discovered": 0}
    assert result["ceded"] == []
    assert result["warnings"] == []
    assert "opencode_go_quota" not in result["lanes"]
    assert _go_rows(kwargs["quota_ledger"]) == []
    assert "private-" not in json.dumps(result) and API_KEY not in json.dumps(result)


def test_selected_go_lane_still_probes_the_native_reader(tmp_path, monkeypatch):
    calls = []

    def native(api_key, namespace, **kwargs):
        calls.append(namespace)
        return _go_reading(namespace, "opencode_go_usage_api")

    monkeypatch.setattr(collector_module, "collect_opencode_go_usage_quota", native)
    monkeypatch.setattr(collector_module, "collect_exact_opencode_go_quota", _forbid("cookie reader"))
    monkeypatch.setattr(collector_module, "collect_opencode_usage", _forbid("opencode usage read"))
    monkeypatch.setattr(collector_module, "codex_quota_observations", lambda *a, **k: [])
    kwargs = _quota_kwargs(tmp_path)

    result = collect_all(**kwargs, environment=NATIVE_ENV, quota_lanes=["opencode_go_quota"])

    assert calls == ["sol:opencode-go"]
    assert result["sources"]["opencode_go_quota"] == {"discovered": 3}
    assert result["lanes"]["opencode_go_quota"]["succeeded"] is True
    assert len(_go_rows(kwargs["quota_ledger"])) == 3


def test_selected_go_lane_still_probes_the_cookie_reader(tmp_path, monkeypatch):
    calls = []

    def cookie(workspace, auth_cookie, namespace, **kwargs):
        calls.append(namespace)
        return _go_reading(namespace, "opencode_go_server_function")

    monkeypatch.setattr(collector_module, "collect_exact_opencode_go_quota", cookie)
    monkeypatch.setattr(collector_module, "collect_opencode_go_usage_quota", _forbid("native reader"))
    monkeypatch.setattr(collector_module, "collect_opencode_usage", _forbid("opencode usage read"))
    monkeypatch.setattr(collector_module, "codex_quota_observations", lambda *a, **k: [])
    kwargs = _quota_kwargs(tmp_path)

    result = collect_all(**kwargs, environment=COOKIE_ENV, quota_lanes=["opencode_go_quota", "claude_code_quota"])

    assert calls == ["sol:opencode-go"]
    assert result["sources"]["opencode_go_quota"] == {"discovered": 3}
    assert len(_go_rows(kwargs["quota_ledger"])) == 3


def test_unset_lane_filter_keeps_probing_the_go_lane(tmp_path, monkeypatch):
    calls = []

    def native(api_key, namespace, **kwargs):
        calls.append(namespace)
        return _go_reading(namespace, "opencode_go_usage_api")

    monkeypatch.setattr(collector_module, "collect_opencode_go_usage_quota", native)
    monkeypatch.setattr(collector_module, "collect_exact_opencode_go_quota", _forbid("cookie reader"))
    monkeypatch.setattr(collector_module, "collect_opencode_usage", _forbid("opencode usage read"))
    monkeypatch.setattr(collector_module, "codex_quota_observations", lambda *a, **k: [])
    kwargs = _quota_kwargs(tmp_path)

    result = collect_all(**kwargs, environment=NATIVE_ENV, quota_lanes=None)

    assert calls == ["sol:opencode-go"]
    assert result["sources"]["opencode_go_quota"] == {"discovered": 3}


# ── 2. /api/opencode-go-usage is read-only ────────────────────────────────


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 15, 3, 40, 0, tzinfo=tz or timezone.utc)


def _usage_event(identity, when, provider="opencode-go"):
    return normalize_fact(
        {
            "fact_type": "usage_event_v1",
            "schema_version": 1,
            "source_namespace": "private:test",
            "source_event_id": identity,
            "harness": "hermes",
            "purpose": "main",
            "record_kind": "api_attempt",
            "occurred_at": when,
            "recorded_at": when,
            "usage_source": "provider_reported",
            "usage_completeness": "complete",
            "measurement_confidence": "exact",
            "cost_status": "included",
            "provider": provider,
            "model_requested": "glm-5.3",
            "model_reported": "glm-5.3",
            "input_tokens": 1000000,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
        }
    )


def _native_quota(observation_id="native-1", observed_at="2026-09-15T03:35:00Z"):
    return quota_module._quota(
        namespace="private:go",
        observation_id=observation_id,
        harness="opencode_go_api",
        observed_at=observed_at,
        provider="opencode-go",
        quota_name="week",
        window_kind="fixed",
        unit="percent",
        limit=100,
        used=100,
        remaining=0,
        resets_at="2026-09-21T00:00:00Z",
        x_source_surface="opencode_go_usage_api",
        x_percent_resolution=1,
        x_provider_state="rate_limited",
    )


def _store(path, rows):
    if path.suffix == ".jsonl":
        path.write_text("".join(canonical_json(row) + "\n" for row in rows), encoding="utf-8")
    else:
        append_sqlite_facts(path, rows)


def _go_app(tmp_path, usage, quotas):
    return create_app(ledger=str(tmp_path / "unused.jsonl"), unified_usage_ledger=str(usage), quota_ledger=str(quotas))


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_go_route_leaves_jsonl_ledgers_untouched(tmp_path, monkeypatch):
    """Mode stays as found (0644 here), no ``.lock`` sibling appears, and
    an empty ledger is a valid empty read."""
    monkeypatch.setattr(dashboard_module, "datetime", _Clock)
    usage, quotas = tmp_path / "usage.jsonl", tmp_path / "quota.jsonl"
    usage.write_text("", encoding="utf-8")
    quotas.write_text("", encoding="utf-8")
    os.chmod(usage, 0o644)
    os.chmod(quotas, 0o644)
    before = sorted(p.name for p in tmp_path.iterdir())

    response = _go_app(tmp_path, usage, quotas).test_client().get("/api/opencode-go-usage")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["native"]["percent"] is None
    assert payload["models"] == []
    assert _mode(usage) == 0o644 and _mode(quotas) == 0o644
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert not (tmp_path / "usage.jsonl.lock").exists() and not (tmp_path / "quota.jsonl.lock").exists()


def test_go_route_populated_jsonl_read_has_no_side_effects_and_matches_sqlite(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_module, "datetime", _Clock)
    rows = [_usage_event("in", "2026-09-14T00:00:00Z"), _usage_event("prior", "2026-09-13T23:59:59Z")]
    payloads = {}
    for suffix in (".jsonl", ".sqlite3"):
        usage, quotas = tmp_path / f"usage{suffix}", tmp_path / f"quota{suffix}"
        _store(usage, rows)
        _store(quotas, [_native_quota()])
        if suffix == ".jsonl":
            os.chmod(usage, 0o644)
            os.chmod(quotas, 0o644)
        response = _go_app(tmp_path, usage, quotas).test_client().get("/api/opencode-go-usage")
        assert response.status_code == 200
        payloads[suffix] = response.get_json()
    assert _mode(tmp_path / "usage.jsonl") == 0o644 and _mode(tmp_path / "quota.jsonl") == 0o644
    assert not (tmp_path / "usage.jsonl.lock").exists()
    assert not (tmp_path / "quota.jsonl.lock").exists()
    for payload in payloads.values():
        assert payload["native"]["percent"] == 100
        assert [model["event_count"] for model in payload["models"]] == [1]
    assert payloads[".jsonl"]["models"] == payloads[".sqlite3"]["models"]


def test_go_route_directory_ledger_is_a_json_503_without_touching_it(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_module, "datetime", _Clock)
    usage, quotas = tmp_path / "usage.jsonl", tmp_path / "quota.jsonl"
    usage.mkdir()
    os.chmod(usage, 0o755)
    _store(quotas, [_native_quota()])
    os.chmod(quotas, 0o644)

    response = _go_app(tmp_path, usage, quotas).test_client().get("/api/opencode-go-usage")

    assert response.status_code == 503
    assert response.is_json
    assert response.get_json() == {"error": "configured private ledger is unavailable"}
    assert _mode(usage) == 0o755 and _mode(quotas) == 0o644
    assert not (tmp_path / "usage.jsonl.lock").exists()
    assert str(tmp_path) not in response.get_data(as_text=True)


def test_go_route_unsupported_suffix_and_missing_file_statuses(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_module, "datetime", _Clock)
    quotas = tmp_path / "quota.jsonl"
    _store(quotas, [_native_quota()])
    odd = tmp_path / "usage.csv"
    odd.write_text("", encoding="utf-8")
    assert _go_app(tmp_path, odd, quotas).test_client().get("/api/opencode-go-usage").status_code == 400
    missing = _go_app(tmp_path, tmp_path / "absent.jsonl", quotas).test_client().get("/api/opencode-go-usage")
    assert missing.status_code == 404 and missing.is_json


def test_go_route_combined_row_cap_still_applies_to_readonly_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_module, "datetime", _Clock)
    usage, quotas = tmp_path / "usage.jsonl", tmp_path / "quota.jsonl"
    _store(usage, [_usage_event("a", "2026-09-14T00:00:00Z")])
    _store(quotas, [_native_quota()])
    monkeypatch.setattr(dashboard_module, "_PRIVATE_API_MAX_ROWS", 1)

    response = _go_app(tmp_path, usage, quotas).test_client().get("/api/opencode-go-usage")

    assert response.status_code == 413
    assert response.get_json()["error"] == "ledger query exceeds private API row limit"


def test_private_facts_default_path_still_uses_the_locking_reader(tmp_path):
    """Other private APIs keep their existing reader (chmod to 0600 and
    the ``.lock`` sibling); only the explicit read-only flag changes it."""
    ledger = tmp_path / "quota.jsonl"
    _store(ledger, [_native_quota()])
    os.chmod(ledger, 0o644)
    rows = dashboard_module._private_facts(
        str(ledger), fact_type="quota_observation_v1", filters={"provider": "opencode-go"}
    )
    assert len(rows) == 1
    assert _mode(ledger) == 0o600
    os.chmod(ledger, 0o644)
    rows = dashboard_module._private_facts(
        str(ledger), fact_type="quota_observation_v1", filters={"provider": "opencode-go"}, readonly_jsonl=True
    )
    assert len(rows) == 1
    assert _mode(ledger) == 0o644


# ── 3. Burn cohort after the cookie → native cutover ─────────────────────


NOW = datetime(2026, 9, 15, 4, 0, 0, tzinfo=timezone.utc)


def _obs(source_id, observed_at, used, surface, **updates):
    row = {
        "fact_type": "quota_observation_v1",
        "schema_version": 1,
        "source_namespace": "sol:opencode-go",
        "source_observation_id": source_id,
        "harness": "opencode_go_api",
        "observed_at": observed_at,
        "provider": "opencode-go",
        "quota_name": "five_hour",
        "quota_scope": "account",
        "window_kind": "rolling",
        "unit": "percent",
        "measurement_confidence": "exact",
        "limit_value": "100",
        "used_value": str(used),
        "remaining_value": str(100 - used),
        "resets_at": "2026-09-15T07:00:00Z",
        "window_started_at": None,
        "window_ends_at": None,
        "account_ref": None,
        "provider_payload_ref": None,
        "x_source_surface": surface,
    }
    if surface == "opencode_go_usage_api":
        row["x_percent_resolution"] = 1
        row["x_provider_state"] = "ok"
    row.update(updates)
    return row


def _cookie(source_id, observed_at, used, **updates):
    return _obs(source_id, observed_at, used, "opencode_go_server_function", **updates)


def _native(source_id, observed_at, used, **updates):
    return _obs(source_id, observed_at, used, "opencode_go_usage_api", **updates)


def test_burn_rate_does_not_straddle_the_cookie_to_native_cutover():
    rows = [_cookie("k1", "2026-09-15T02:00:00Z", 10), _native("n1", "2026-09-15T03:00:00Z", 30)]

    [series] = _build_quota_burn_projection(rows, generated_at=NOW)["series"]

    # One native observation is not a rate; the 10→30 rise across the
    # cutover is a precision/source change, not a measured burn.
    assert series["burn_rate_pct_per_hour"] is None
    assert series["deltas"] == []
    assert series["rate_window_deltas"] == 0
    assert (series["projection_status"], series["projection_reason"]) == ("insufficient_observations", "single_observation")
    # The cookie history and the current exact value are preserved.
    assert [obs["used_pct"] for obs in series["observations"]] == [10.0, 30.0]
    assert series["observation_count"] == 2
    assert (series["latest_used_pct"], series["source_surface"], series["percent_resolution"]) == (30.0, "opencode_go_usage_api", 1)


def test_burn_rate_uses_the_contiguous_native_epoch_after_the_cutover():
    rows = [
        _cookie("k1", "2026-09-15T02:00:00Z", 10),
        _native("n1", "2026-09-15T03:00:00Z", 30),
        _native("n2", "2026-09-15T03:30:00Z", 40),
    ]

    [series] = _build_quota_burn_projection(rows, generated_at=NOW)["series"]

    assert series["burn_rate_pct_per_hour"] == pytest.approx(20.0)
    assert [(d["rise_pct"], d["gap_seconds"]) for d in series["deltas"]] == [(10.0, 1800)]
    assert series["deltas"][0]["from_observed_at"] == "2026-09-15T03:00:00Z"
    assert series["projection_status"] == "projected_before_reset"
    assert [obs["used_pct"] for obs in series["observations"]] == [10.0, 30.0, 40.0]
    assert series["observation_count"] == 3


def test_burn_rate_cohort_restarts_after_a_later_cookie_observation():
    """The epoch is the native run *after the latest* non-native reading:
    an older native reading before a cookie reading does not count."""
    rows = [
        _native("n0", "2026-09-15T01:00:00Z", 5),
        _cookie("k1", "2026-09-15T02:00:00Z", 10),
        _native("n1", "2026-09-15T03:00:00Z", 30),
    ]

    [series] = _build_quota_burn_projection(rows, generated_at=NOW)["series"]

    assert series["burn_rate_pct_per_hour"] is None
    assert series["deltas"] == []
    assert series["projection_reason"] == "single_observation"
    assert series["observation_count"] == 3


def test_cookie_only_burn_series_is_unchanged():
    rows = [_cookie("k1", "2026-09-15T02:00:00Z", 10), _cookie("k2", "2026-09-15T03:00:00Z", 30)]

    [series] = _build_quota_burn_projection(rows, generated_at=NOW)["series"]

    assert series["burn_rate_pct_per_hour"] == pytest.approx(20.0)
    assert series["projection_status"] == "projected_before_reset"
    assert series["source_surface"] == "opencode_go_server_function"


def test_latest_cookie_after_native_keeps_the_full_cohort():
    """The restriction is keyed on the *latest* reading being native; a
    series whose latest reading is the cookie surface measures as before."""
    rows = [_native("n1", "2026-09-15T02:00:00Z", 10), _cookie("k1", "2026-09-15T03:00:00Z", 30)]

    [series] = _build_quota_burn_projection(rows, generated_at=NOW)["series"]

    assert series["burn_rate_pct_per_hour"] == pytest.approx(20.0)


def test_cutover_series_stays_one_account_via_the_route(tmp_path, monkeypatch):
    class _FrozenNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 15, 4, 0, 0, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", _FrozenNow)
    rows = [
        _cookie("k1", "2026-09-15T02:00:00Z", 10),
        _native("n1", "2026-09-15T03:00:00Z", 30),
        _native("n2", "2026-09-15T03:30:00Z", 40),
    ]
    ledger = tmp_path / "quota.jsonl"
    ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    client = create_app(quota_ledger=str(ledger)).test_client()

    burn = client.get("/api/quota-burn?hours=3").get_json()
    [series] = burn["series"]
    assert (series["provider_account_index"], series["provider_account_count"]) == (1, 1)
    assert series["burn_rate_pct_per_hour"] == pytest.approx(20.0)
    assert series["observation_count"] == 3
    # Subscriptions history keeps the cookie reading and its exact value.
    history = [row for row in client.get("/api/subscriptions?days=1").get_json()["observations"] if row["quota_name"] == "five_hour"]
    assert [(row["used_value"], row["source_surface"]) for row in history] == [
        ("40", "opencode_go_usage_api"),
        ("30", "opencode_go_usage_api"),
        ("10", "opencode_go_server_function"),
    ]
