from __future__ import annotations

import json
import re
import os
import sqlite3
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from codex_usage_tracker.canonical_ledger import (
    append_sqlite_facts,
    canonical_json,
    read_facts,
    read_sqlite_facts,
)
import codex_usage_tracker.collector as collector_module
import codex_usage_tracker.dashboard as dashboard_module
import codex_usage_tracker.quota as quota_module
from codex_usage_tracker.cli import main
from codex_usage_tracker.dashboard import create_app

import frontend_surfaces
from codex_usage_tracker.provider_spend import latest_budget_state
from codex_usage_tracker.quota import (
    capture_claude_code_usage_screen,
    collect_claude_code_quota,
    collect_deepseek_quota,
    collect_kimi_code_quota,
    collect_openrouter_quota,
)
from codex_usage_tracker.collector import collect_all


def _usage_fact(source_event_id="u-1"):
    return {
        "fact_type": "usage_event_v1",
        "schema_version": 1,
        "source_namespace": "test:collect-all",
        "source_event_id": source_event_id,
        "harness": "test",
        "purpose": "main",
        "record_kind": "api_attempt",
        "occurred_at": "2026-07-12T12:00:00Z",
        "recorded_at": "2026-07-12T12:00:01Z",
        "usage_source": "provider_reported",
        "usage_completeness": "complete",
        "measurement_confidence": "exact",
        "cost_status": "included",
    }


def _collector_quota_rows(provider, harness, namespace, names):
    rows = []
    for name in names:
        rows.append(
            quota_module._quota(
                namespace=namespace,
                observation_id=f"collector-test:{name}",
                harness=harness,
                observed_at="2026-08-02T05:25:00Z",
                provider=provider,
                quota_name=name,
                window_kind="rolling" if name == "five_hour" else "fixed",
                unit="requests" if name == "web_search_month" else "percent",
                limit=100,
                used=25,
                remaining=75,
                x_source_surface="collector_test",
            )
        )
    return rows


def _collect_quota_only(tmp_path, *, environment, dotenv=None, strict_sources=False):
    quota = tmp_path / "quota.sqlite3"
    result = collect_all(
        state_db=tmp_path / "missing-state.db",
        claude_root=tmp_path / "missing-claude",
        opencode_dbs=[],
        codex_ledger=tmp_path / "missing-codex.jsonl",
        usage_ledger=tmp_path / "out-of-scope-usage.sqlite3",
        quota_ledger=quota,
        dotenv=dotenv,
        claude_quota_command=None,
        live_quota=True,
        environment=environment,
        scope="quota",
        strict_sources=strict_sources,
    )
    return result, quota


def test_live_provider_fetches_are_structured_and_secret_free(monkeypatch):
    class Response:
        def __init__(self, data): self.data=data
        def raise_for_status(self): pass
        def json(self): return self.data
    class Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def get(self,url,headers):
            if url.endswith("credits"):
                return Response({"data":{"total_credits":100,"total_usage":25}})
            if url.endswith("key"):
                return Response({"data":{"limit":50,"limit_remaining":40,"usage":10}})
            return Response({"balance_infos":[{"currency":"USD","total_balance":"8.25","granted_balance":"10","topped_up_balance":"-1.75"}],"is_available":True})
    monkeypatch.setattr("codex_usage_tracker.quota.httpx.Client",Client)
    opened=collect_openrouter_quota("key", "ns:or", observed_at="2026-07-12T12:00:00Z")
    deep=collect_deepseek_quota("key", "ns:ds", observed_at="2026-07-12T12:00:00Z")
    assert {r["quota_name"] for r in opened} >= {"credit_balance","api_key_quota"}
    assert deep[0]["remaining_value"]=="8.25"
    assert deep[0]["x_topped_up_balance"] == "-1.75"
    assert '"key"' not in json.dumps(opened+deep).lower()


def test_deepseek_negative_account_balance_clamps_remaining_and_preserves_signed_value(monkeypatch):
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {
                "balance_infos": [{
                    "currency": "USD",
                    "total_balance": "-1.75",
                    "granted_balance": "0",
                    "topped_up_balance": "-1.75",
                }],
                "is_available": False,
            }

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, headers): return Response()

    monkeypatch.setattr("codex_usage_tracker.quota.httpx.Client", Client)
    rows = collect_deepseek_quota("private-key", "ns:deepseek")

    assert rows[0]["remaining_value"] == "0"
    assert rows[0]["x_provider_balance"] == "-1.75"
    assert rows[0]["x_topped_up_balance"] == "-1.75"
    assert rows[0]["x_is_available"] is False


def test_z_ai_quota_collects_exact_recognized_windows(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self): captured["status_checked"] = True
        def json(self):
            return {
                "success": True,
                "code": 200,
                "data": {
                    "limits": [
                        {
                            "type": "TOKENS_LIMIT",
                            "unit": 3,
                            "number": 5,
                            "percentage": "12.5",
                            "nextResetTime": 1785656751000,
                            "level": "MAX",
                        },
                        {
                            "name": "TOKENS_LIMIT",
                            "unit": 6,
                            "number": 1,
                            "percentage": 40,
                            "nextResetTime": "2026-08-09T02:45:51Z",
                        },
                        {
                            "type": "TIME_LIMIT",
                            "unit": 5,
                            "number": 1,
                            "currentValue": "30",
                            "usage": "100",
                            "remaining": "70",
                            "nextResetTime": 1788220800000,
                        },
                    ]
                },
            }

    class Client:
        def __init__(self, *args, **kwargs): captured["timeout"] = kwargs.get("timeout")
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, headers):
            captured["url"] = url
            captured["accept"] = headers.get("Accept")
            captured["authorized"] = headers.get("Authorization") == "Bearer private-z-ai-key"
            return Response()

    monkeypatch.setattr("codex_usage_tracker.quota.httpx.Client", Client)
    rows = quota_module.collect_z_ai_quota(
        "private-z-ai-key",
        "ns:z-ai",
        observed_at="2026-08-02T05:25:00Z",
    )

    assert captured == {
        "timeout": 30.0,
        "url": "https://api.z.ai/api/monitor/usage/quota/limit",
        "accept": "application/json",
        "authorized": True,
        "status_checked": True,
    }
    by_name = {row["quota_name"]: row for row in rows}
    assert set(by_name) == {"five_hour", "week", "web_search_month"}
    assert (
        by_name["five_hour"]["used_value"],
        by_name["five_hour"]["remaining_value"],
        by_name["five_hour"]["resets_at"],
    ) == ("12.5", "87.5", "2026-08-02T07:45:51Z")
    assert by_name["five_hour"]["window_kind"] == "rolling"
    assert by_name["week"]["used_value"] == "40"
    assert by_name["week"]["remaining_value"] == "60"
    assert by_name["week"]["resets_at"] == "2026-08-09T02:45:51Z"
    web = by_name["web_search_month"]
    assert web["window_kind"] == "fixed" and web["unit"] == "requests"
    assert (web["limit_value"], web["used_value"], web["remaining_value"]) == (
        "100", "30", "70"
    )
    assert web["resets_at"] == "2026-09-01T00:00:00Z"
    assert all(
        row["provider"] == "z-ai"
        and row["harness"] == "z_ai_api"
        and row["measurement_confidence"] == "exact"
        for row in rows
    )
    assert by_name["five_hour"]["x_provider_unit"] == 3
    assert by_name["five_hour"]["x_provider_number"] == 5
    assert by_name["five_hour"]["x_provider_level"] == "MAX"
    serialized = json.dumps(rows)
    assert "private-z-ai-key" not in serialized
    assert "data" not in by_name["five_hour"]


@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "code": 500, "data": {"limits": []}},
        {
            "success": True,
            "code": 200,
            "data": {"limits": [
                {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 10},
                {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 11},
            ]},
        },
        {
            "success": True,
            "code": 200,
            "data": {"limits": [
                {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 101},
            ]},
        },
        {
            "success": True,
            "code": 200,
            "data": {"limits": [{
                "type": "TIME_LIMIT", "unit": 5, "number": 1,
                "currentValue": 30, "usage": 100, "remaining": 69,
            }]},
        },
        {
            "success": True,
            "code": 200,
            "data": {"limits": [
                {"type": "TOKENS_LIMIT", "unit": 4, "number": 1, "percentage": 10},
            ]},
        },
    ],
)
def test_z_ai_quota_rejects_unsuccessful_malformed_or_ambiguous_payloads(monkeypatch, payload):
    class Response:
        def raise_for_status(self): pass
        def json(self): return payload

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, headers): return Response()

    monkeypatch.setattr("codex_usage_tracker.quota.httpx.Client", Client)
    with pytest.raises(ValueError, match="Z.AI"):
        quota_module.collect_z_ai_quota("private-z-ai-key", "ns:z-ai")


def _complete_z_ai_limits():
    return [
        {
            "type": "TOKENS_LIMIT",
            "unit": 3,
            "number": 5,
            "percentage": 10,
            "nextResetTime": 1785656751000,
        },
        {
            "type": "TOKENS_LIMIT",
            "unit": 6,
            "number": 1,
            "percentage": 20,
            "nextResetTime": 1786262400000,
        },
        {
            "type": "TIME_LIMIT",
            "unit": 5,
            "number": 1,
            "currentValue": 30,
            "usage": 100,
            "remaining": 70,
            "nextResetTime": 1788220800000,
        },
    ]


def _collect_z_ai_limits(monkeypatch, limits):
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {"success": True, "code": 200, "data": {"limits": limits}}

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, headers): return Response()

    monkeypatch.setattr("codex_usage_tracker.quota.httpx.Client", Client)
    return quota_module.collect_z_ai_quota("private-z-ai-key", "ns:z-ai")


@pytest.mark.parametrize("recognized_count", [1, 2])
def test_z_ai_quota_rejects_partial_known_quota_surface(monkeypatch, recognized_count):
    with pytest.raises(ValueError, match="Z.AI"):
        _collect_z_ai_limits(monkeypatch, _complete_z_ai_limits()[:recognized_count])


@pytest.mark.parametrize("malformed_position", [0, 3])
def test_z_ai_quota_rejects_non_object_limits_regardless_of_order(
    monkeypatch, malformed_position
):
    limits: list[Any] = _complete_z_ai_limits()
    limits.insert(malformed_position, "malformed-limit")

    with pytest.raises(ValueError, match="Z.AI"):
        _collect_z_ai_limits(monkeypatch, limits)


def test_z_ai_quota_clamps_web_search_overshoot_to_the_cap(monkeypatch):
    """Live 2026-09-10: Z.AI reported currentValue 4001 against usage 4000
    with remaining 0 once the monthly search cap tripped. The old strict
    check ceded the lane on that payload for the rest of the month, which
    marked the whole subscriptions surface stale and hid every provider's
    charts. The canonical row clamps at the cap; the raw figure is kept."""
    limits = _complete_z_ai_limits()
    limits[2] = {
        "type": "TIME_LIMIT", "unit": 5, "number": 1,
        "currentValue": 4001, "usage": 4000, "remaining": 0, "percentage": 100,
        "nextResetTime": 1790190588997,
    }

    rows = _collect_z_ai_limits(monkeypatch, limits)

    web = next(row for row in rows if row["quota_name"] == "web_search_month")
    assert (web["limit_value"], web["used_value"], web["remaining_value"]) == (
        "4000", "4000", "0"
    )
    assert web["x_provider_used"] == "4001"
    assert web["measurement_confidence"] == "exact"
    # An inconsistent remaining figure is still rejected after the clamp.
    limits[2] = dict(limits[2], remaining=5)
    with pytest.raises(ValueError, match="inconsistent"):
        _collect_z_ai_limits(monkeypatch, limits)


def test_z_ai_quota_ignores_unknown_well_formed_limits(monkeypatch):
    limits = _complete_z_ai_limits()
    limits.insert(1, {"type": "FUTURE_LIMIT", "providerField": "preserved-upstream"})

    rows = _collect_z_ai_limits(monkeypatch, limits)

    assert {row["quota_name"] for row in rows} == {
        "five_hour", "week", "web_search_month"
    }


@pytest.mark.parametrize(
    ("entry_index", "field"),
    [
        (0, "percentage"),
        (2, "currentValue"),
        (2, "usage"),
        (2, "remaining"),
    ],
)
@pytest.mark.parametrize(
    "invalid_value",
    [-1, True, float("nan"), float("inf")],
    ids=["negative", "bool", "nan", "infinity"],
)
def test_z_ai_quota_rejects_invalid_nonnegative_values(
    monkeypatch, entry_index, field, invalid_value
):
    limits = _complete_z_ai_limits()
    limits[entry_index][field] = invalid_value

    with pytest.raises(ValueError, match="Z.AI"):
        _collect_z_ai_limits(monkeypatch, limits)


def test_opencode_go_quota_collects_exact_server_function_windows(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self): captured["status_checked"] = True
        def json(self):
            return {
                "rollingUsage": {"usagePercent": "12.5", "resetInSec": 3600},
                "weeklyUsage": {"usagePercent": 25, "resetInSec": 7200},
                "monthlyUsage": {"usagePercent": 80, "resetInSec": 0},
            }

    class Client:
        def __init__(self, *args, **kwargs): captured["timeout"] = kwargs.get("timeout")
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, params, headers):
            captured["url"] = url
            captured["params"] = params
            captured["accept"] = headers.get("Accept")
            captured["cookie"] = headers.get("Cookie")
            captured["referer"] = headers.get("Referer")
            captured["server_id"] = headers.get("X-Server-Id")
            captured["server_instance"] = headers.get("X-Server-Instance")
            return Response()

    monkeypatch.setattr("codex_usage_tracker.quota.httpx.Client", Client)
    rows = quota_module.collect_opencode_go_quota(
        "fake-workspace-id",
        "fake-auth-cookie",
        "ns:opencode-go",
        observed_at="2026-08-02T05:25:00Z",
    )

    assert captured == {
        "timeout": 30.0,
        "url": "https://opencode.ai/_server/",
        "params": {"id": "lite.subscription.get", "args": '["fake-workspace-id"]'},
        "accept": "application/json",
        "cookie": "auth=fake-auth-cookie",
        "referer": "https://opencode.ai/workspace/fake-workspace-id/go",
        "server_id": "lite.subscription.get",
        "server_instance": "codex-usage-tracker",
        "status_checked": True,
    }
    by_name = {row["quota_name"]: row for row in rows}
    assert set(by_name) == {"five_hour", "week", "month"}
    assert (
        by_name["five_hour"]["used_value"],
        by_name["five_hour"]["remaining_value"],
        by_name["five_hour"]["resets_at"],
        by_name["five_hour"]["window_kind"],
    ) == ("12.5", "87.5", "2026-08-02T06:25:00Z", "rolling")
    assert by_name["week"]["resets_at"] == "2026-08-02T07:25:00Z"
    assert by_name["week"]["window_kind"] == "fixed"
    assert by_name["month"]["resets_at"] == "2026-08-02T05:25:00Z"
    assert by_name["month"]["window_kind"] == "fixed"
    assert all(
        row["provider"] == "opencode-go"
        and row["harness"] == "opencode_go_api"
        and row["unit"] == "percent"
        and row["limit_value"] == "100"
        and row["measurement_confidence"] == "exact"
        for row in rows
    )
    serialized = json.dumps(rows)
    assert "fake-workspace-id" not in serialized
    assert "fake-auth-cookie" not in serialized


def test_opencode_go_quota_parses_server_function_text_without_evaluating_it(monkeypatch):
    class Response:
        def raise_for_status(self): pass
        def json(self): raise json.JSONDecodeError("not JSON", "x", 0)
        def text(self):
            return (
                ';0x000000ef;(globalThis.$R={},$R[0]={'
                'rollingUsage:$R[1]={usagePercent:7,resetInSec:18000},'
                'weeklyUsage:$R[2]={resetInSec:540000,usagePercent:2},'
                'monthlyUsage:$R[3]={usagePercent:16,resetInSec:2480000}})'
            )

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, params, headers): return Response()

    monkeypatch.setattr("codex_usage_tracker.quota.httpx.Client", Client)
    rows = quota_module.collect_opencode_go_quota(
        "fake-workspace-id",
        "fake-auth-cookie",
        "ns:opencode-go",
        observed_at="2026-08-02T05:25:00Z",
    )
    assert [row["used_value"] for row in rows] == ["7", "2", "16"]
    assert "globalThis" not in json.dumps(rows)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "rollingUsage": {"usagePercent": 10, "resetInSec": 1},
            "weeklyUsage": {"usagePercent": 20, "resetInSec": 2},
        },
        {
            "rollingUsage": {"usagePercent": 101, "resetInSec": 1},
            "weeklyUsage": {"usagePercent": 20, "resetInSec": 2},
            "monthlyUsage": {"usagePercent": 30, "resetInSec": 3},
        },
        {
            "rollingUsage": {"usagePercent": 10, "resetInSec": -1},
            "weeklyUsage": {"usagePercent": 20, "resetInSec": 2},
            "monthlyUsage": {"usagePercent": 30, "resetInSec": 3},
        },
    ],
)
def test_opencode_go_quota_rejects_partial_or_invalid_windows_without_secret_leak(
    monkeypatch, payload
):
    class Response:
        def raise_for_status(self): pass
        def json(self): return payload

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, params, headers): return Response()

    monkeypatch.setattr("codex_usage_tracker.quota.httpx.Client", Client)
    with pytest.raises(ValueError, match="OpenCode Go") as error:
        quota_module.collect_opencode_go_quota(
            "fake-workspace-id", "fake-auth-cookie", "ns:opencode-go"
        )
    assert "fake-workspace-id" not in str(error.value)
    assert "fake-auth-cookie" not in str(error.value)


def test_opencode_go_quota_sanitizes_transport_errors(monkeypatch):
    class Response:
        def raise_for_status(self):
            raise RuntimeError("fake-workspace-id fake-auth-cookie")

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, params, headers): return Response()

    monkeypatch.setattr("codex_usage_tracker.quota.httpx.Client", Client)
    with pytest.raises(ValueError, match="OpenCode Go quota request failed") as error:
        quota_module.collect_opencode_go_quota(
            "fake-workspace-id", "fake-auth-cookie", "ns:opencode-go"
        )
    assert "fake-workspace-id" not in str(error.value)
    assert "fake-auth-cookie" not in str(error.value)


def test_kimi_code_quota_uses_official_windows_and_derives_missing_remaining(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {
                "usage": {
                    "limit": "100",
                    "used": "21",
                    "remaining": "79",
                    "resetTime": "2026-08-09T02:45:51.442531Z",
                },
                "limits": [{
                    "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                    "detail": {
                        "limit": "100",
                        "used": "100",
                        "resetTime": "2026-08-02T07:45:51.442531Z",
                    },
                }],
            }

    class Client:
        def __init__(self, *args, **kwargs): captured["timeout"] = kwargs.get("timeout")
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, headers):
            captured["url"] = url
            captured["authorized"] = headers.get("Authorization") == "Bearer private-key"
            return Response()

    monkeypatch.setattr("codex_usage_tracker.quota.httpx.Client", Client)
    rows = collect_kimi_code_quota(
        "private-key",
        "ns:kimi-code",
        observed_at="2026-08-02T05:25:00Z",
    )

    assert captured == {
        "timeout": 30.0,
        "url": "https://api.kimi.com/coding/v1/usages",
        "authorized": True,
    }
    assert {row["quota_name"] for row in rows} == {"five_hour", "week"}
    five_hour = next(row for row in rows if row["quota_name"] == "five_hour")
    weekly = next(row for row in rows if row["quota_name"] == "week")
    assert five_hour["window_kind"] == "rolling"
    assert five_hour["limit_value"] == "100"
    assert five_hour["used_value"] == "100"
    assert five_hour["remaining_value"] == "0"
    assert five_hour["resets_at"] == "2026-08-02T07:45:51.442531Z"
    assert weekly["window_kind"] == "fixed"
    assert weekly["used_value"] == "21" and weekly["remaining_value"] == "79"
    assert weekly["resets_at"] == "2026-08-09T02:45:51.442531Z"
    assert all(
        row["provider"] == "kimi-coding"
        and row["harness"] == "kimi_code"
        and row["unit"] == "provider_unit"
        and row["measurement_confidence"] == "exact"
        for row in rows
    )
    assert "private-key" not in json.dumps(rows)


def _collect_kimi_payload(monkeypatch, *, usage, five_hour_detail):
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {
                "usage": usage,
                "limits": [{
                    "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                    "detail": five_hour_detail,
                }],
            }

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, headers): return Response()

    monkeypatch.setattr("codex_usage_tracker.quota.httpx.Client", Client)
    return collect_kimi_code_quota(
        "private-key",
        "ns:kimi-code",
        observed_at="2026-08-02T05:25:00Z",
    )


def test_kimi_code_quota_accepts_live_two_of_three_value_variant(monkeypatch):
    rows = _collect_kimi_payload(
        monkeypatch,
        usage={
            "limit": "100",
            "used": "100",
            "resetTime": "2026-08-09T02:45:51.442531Z",
        },
        five_hour_detail={
            "limit": "100",
            "remaining": "100",
            "resetTime": "2026-08-02T07:45:51.442531Z",
        },
    )

    by_name = {row["quota_name"]: row for row in rows}
    five_hour = by_name["five_hour"]
    weekly = by_name["week"]
    assert (
        five_hour["limit_value"],
        five_hour["used_value"],
        five_hour["remaining_value"],
    ) == ("100", "0", "100")
    assert five_hour["resets_at"] == "2026-08-02T07:45:51.442531Z"
    assert (
        weekly["limit_value"],
        weekly["used_value"],
        weekly["remaining_value"],
    ) == ("100", "100", "0")
    assert weekly["resets_at"] == "2026-08-09T02:45:51.442531Z"
    assert all(row["measurement_confidence"] == "exact" for row in rows)


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ({"used": "25", "remaining": "75"}, ("100", "25", "75")),
        ({"limit": "100", "remaining": "75"}, ("100", "25", "75")),
        ({"limit": "100", "used": "25"}, ("100", "25", "75")),
    ],
    ids=["derive-limit", "derive-used", "derive-remaining"],
)
def test_kimi_code_quota_derives_each_missing_core_value_exactly(
    monkeypatch, detail, expected
):
    rows = _collect_kimi_payload(
        monkeypatch,
        usage={"limit": "100", "used": "25", "remaining": "75"},
        five_hour_detail=detail,
    )

    five_hour = next(row for row in rows if row["quota_name"] == "five_hour")
    assert (
        five_hour["limit_value"],
        five_hour["used_value"],
        five_hour["remaining_value"],
    ) == expected
    assert five_hour["measurement_confidence"] == "exact"


@pytest.mark.parametrize(
    "detail",
    [
        {"limit": "100", "used": "25", "remaining": "80"},
        {"limit": "-1", "used": "0"},
        {"limit": "100", "used": "-1"},
        {"limit": "100", "remaining": "-1"},
        {"limit": "100"},
        {"used": "25"},
        {"remaining": "75"},
        {"limit": "100", "used": True},
        {"limit": float("nan"), "used": "25"},
        {"limit": "100", "remaining": float("inf")},
        {"limit": "100", "used": "101"},
        {"limit": "100", "remaining": "101"},
    ],
    ids=[
        "inconsistent",
        "negative-limit",
        "negative-used",
        "negative-remaining",
        "only-limit",
        "only-used",
        "only-remaining",
        "bool",
        "nan",
        "infinity",
        "used-exceeds-limit",
        "remaining-exceeds-limit",
    ],
)
def test_kimi_code_quota_rejects_invalid_or_ambiguous_core_values(monkeypatch, detail):
    with pytest.raises(ValueError):
        _collect_kimi_payload(
            monkeypatch,
            usage={"limit": "100", "used": "25", "remaining": "75"},
            five_hour_detail=detail,
        )


def test_kimi_code_quota_retains_unknown_row_when_all_core_values_are_omitted(monkeypatch):
    rows = _collect_kimi_payload(
        monkeypatch,
        usage={"limit": "100", "used": "25", "remaining": "75"},
        five_hour_detail={},
    )

    five_hour = next(row for row in rows if row["quota_name"] == "five_hour")
    assert (
        five_hour["limit_value"],
        five_hour["used_value"],
        five_hour["remaining_value"],
        five_hour["resets_at"],
    ) == (None, None, None, None)
    assert five_hour["measurement_confidence"] == "unknown"
    assert five_hour["x_provider_state"] == "inactive_or_not_reported"


def test_kimi_code_quota_preserves_zero_week_and_inactive_five_hour_window(monkeypatch):
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {
                "usage": {
                    "limit": 0,
                    "used": 0,
                    "remaining": 0,
                    "resetTime": "2026-08-09T02:45:51.442531Z",
                },
                "limits": [{
                    "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                    "detail": {
                        "limit": None,
                        "used": None,
                        "remaining": None,
                        "resetTime": None,
                    },
                }],
            }

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, headers): return Response()

    monkeypatch.setattr("codex_usage_tracker.quota.httpx.Client", Client)
    rows = collect_kimi_code_quota(
        "private-key",
        "ns:kimi-code",
        observed_at="2026-08-02T05:25:00Z",
    )

    by_name = {row["quota_name"]: row for row in rows}
    weekly = by_name["week"]
    inactive = by_name["five_hour"]
    assert (weekly["limit_value"], weekly["used_value"], weekly["remaining_value"]) == (
        "0", "0", "0"
    )
    assert weekly["measurement_confidence"] == "exact"
    assert (
        inactive["limit_value"], inactive["used_value"], inactive["remaining_value"],
        inactive["resets_at"],
    ) == (None, None, None, None)
    assert inactive["measurement_confidence"] == "unknown"
    assert inactive["x_provider_state"] == "inactive_or_not_reported"
    assert "private-key" not in json.dumps(rows)


def test_kimi_code_quota_rejects_missing_five_hour_window(monkeypatch):
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {
                "usage": {"limit": "100", "used": "21", "remaining": "79"},
                "limits": [],
            }

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, headers): return Response()

    monkeypatch.setattr("codex_usage_tracker.quota.httpx.Client", Client)
    with pytest.raises(ValueError, match="five-hour"):
        collect_kimi_code_quota("private-key", "ns:kimi-code")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "usage": {"limit": "100", "used": "21", "remaining": "80"},
                "limits": [{
                    "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                    "detail": {"limit": "100", "used": "10", "remaining": "90"},
                }],
            },
            "inconsistent",
        ),
        (
            {
                "usage": {"limit": "100", "used": "21", "remaining": "79"},
                "limits": [
                    {
                        "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                        "detail": {"limit": "100", "used": "10", "remaining": "90"},
                    },
                    {
                        "window": {"duration": 5, "timeUnit": "TIME_UNIT_HOUR"},
                        "detail": {"limit": "100", "used": "10", "remaining": "90"},
                    },
                ],
            },
            "ambiguous",
        ),
        (
            {
                "usage": {"limit": "100", "used": "21", "remaining": "79"},
                "limits": [
                    {
                        "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                        "detail": {"limit": "100", "used": "10", "remaining": "90"},
                    },
                    {
                        "window": {"duration": 5, "timeUnit": "TIME_UNIT_HOUR"},
                        "detail": None,
                    },
                ],
            },
            "ambiguous",
        ),
    ],
)
def test_kimi_code_quota_rejects_contradictory_or_ambiguous_payloads(
    monkeypatch, payload, message
):
    class Response:
        def raise_for_status(self): pass
        def json(self): return payload

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, headers): return Response()

    monkeypatch.setattr("codex_usage_tracker.quota.httpx.Client", Client)
    with pytest.raises(ValueError, match=message):
        collect_kimi_code_quota("private-key", "ns:kimi-code")


def test_claude_quota_comes_from_claude_code_usage_screen(tmp_path, monkeypatch):
    screen = """
   Current session
   █████████████████                                  34% used
   Resets 3:40pm (America/New_York)

   Current week (all models)
   ███████████████████████▌                           47% used
   Resets Jul 16, 8pm (America/New_York)

   Current week (Fable)
   ████████████████████████████████████████▌          81% used
   Resets Jul 16, 8pm (America/New_York)
"""
    captured = {}

    def fake_capture(**kwargs):
        captured.update(kwargs)
        return screen

    monkeypatch.setattr("codex_usage_tracker.quota.capture_claude_code_usage_screen", fake_capture)
    rows = collect_claude_code_quota(
        "ns:claude-code",
        claude_command="/run/current-system/sw/bin/claude",
        probe_dir=tmp_path,
        observed_at="2026-07-12T12:00:00Z",
    )
    assert captured["claude_command"] == "/run/current-system/sw/bin/claude"
    assert captured["probe_dir"] == tmp_path
    assert {row["quota_name"] for row in rows} == {"five_hour", "seven_day", "seven_day_fable"}
    session = next(row for row in rows if row["quota_name"] == "five_hour")
    weekly = next(row for row in rows if row["quota_name"] == "seven_day")
    assert session["used_value"] == "34" and session["remaining_value"] == "66"
    assert session["resets_at"] == "2026-07-12T19:40:00Z"
    assert weekly["used_value"] == "47" and weekly["resets_at"] == "2026-07-17T00:00:00Z"
    assert all(row["harness"] == "claude_code" and row["measurement_confidence"] == "exact" for row in rows)


def test_claude_usage_parser_rejects_partial_cross_section_render(tmp_path, monkeypatch):
    partial = """
   Current session
   Loading limits...

   Current week (all models)
   ███████████████████████▌                           47% used
   Resets Jul 16, 8pm (America/New_York)

   Current week (Fable)
   ████████████████████████████████████████▌          81% used
   Resets Jul 16, 8pm (America/New_York)
"""
    monkeypatch.setattr(
        "codex_usage_tracker.quota.capture_claude_code_usage_screen",
        lambda **kwargs: partial,
    )
    with pytest.raises(ValueError, match="Current session"):
        collect_claude_code_quota(
            "ns:claude-code",
            probe_dir=tmp_path,
            observed_at="2026-07-12T12:00:00Z",
        )


def test_claude_usage_probe_rejects_symlinked_lock(tmp_path):
    probe = tmp_path / "probe"
    probe.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("do not chmod", encoding="utf-8")
    lock = tmp_path / ".probe.lock"
    lock.symlink_to(victim)
    with pytest.raises(ValueError, match="lock must not be a symlink"):
        capture_claude_code_usage_screen(probe_dir=probe, timeout=0.1)


def test_claude_usage_probe_refuses_nonempty_working_directory(tmp_path):
    (tmp_path / "untrusted.txt").write_text("data", encoding="utf-8")
    with pytest.raises(ValueError, match="must be empty"):
        capture_claude_code_usage_screen(probe_dir=tmp_path, timeout=0.1)


def test_unified_private_aggregate_endpoints_and_filters(tmp_path):
    usage=tmp_path/"usage.jsonl"; quotas=tmp_path/"quota.jsonl"
    # The day window is measured from the wall clock, so the fixture has to be
    # anchored to it. A pinned date silently rots the moment it leaves the
    # window: this test asserted a 2026-07-12 fixture through days=30 and began
    # failing on 2026-08-11 without a line of production code changing.
    recent=(datetime.now(timezone.utc)-timedelta(days=1)).replace(microsecond=0)
    recent_at=recent.isoformat().replace("+00:00","Z")
    base={"fact_type":"usage_event_v1","schema_version":1,"source_namespace":"n","harness":"hermes","surface":None,"purpose":"main","record_kind":"api_attempt","occurred_at":recent_at,"recorded_at":recent_at,"session_id":None,"logical_call_id":None,"attempt_no":None,"provider_request_id":None,"upstream_provider":None,"model_reported":None,"api_mode":None,"billing_mode":None,"request_status":"ok","error_class":None,"latency_ms":None,"cache_write_tokens":0,"reasoning_tokens":2,"usage_source":"provider_reported","usage_completeness":"complete","measurement_confidence":"exact","missing_fields":[],"attribution_gaps":[],"estimated_cost_usd":None,"actual_cost_usd":None,"cost_status":"included","cost_source":None,"pricing_version":None,"reconstructed_call_count":None,"corrects_source_namespace":None,"corrects_source_event_id":None}
    rows=[dict(base,source_event_id="1",provider="anthropic",model_requested="claude",api_mode="anthropic_messages",input_tokens=10,cache_read_tokens=5,output_tokens=4),dict(base,source_event_id="2",provider="openrouter",model_requested="m",harness="opencode",purpose="subagent",record_kind="historical_aggregate",measurement_confidence="reconstructed",input_tokens=20,cache_read_tokens=0,output_tokens=8,reconstructed_call_count=3)]
    usage.write_text("\n".join(json.dumps(x) for x in rows)+"\n")
    q={"fact_type":"quota_observation_v1","schema_version":1,"source_namespace":"q","source_observation_id":"1","harness":"claude_code","observed_at":recent_at,"provider":"anthropic","account_ref":"private-account","quota_name":"five_hour","quota_scope":"account","window_kind":"rolling","window_started_at":None,"window_ends_at":None,"resets_at":None,"limit_value":"100","remaining_value":"80","used_value":"20","unit":"percent","measurement_confidence":"exact","provider_payload_ref":"private-ref","x_private":"do-not-project"}
    newer=dict(q,source_observation_id="2",observed_at=recent_at.replace("Z",".1Z"),remaining_value="79",used_value="21")
    quotas.write_text(json.dumps(q)+"\n"+json.dumps(newer)+"\n")
    client=create_app(atrium_root=str(tmp_path),ledger=str(tmp_path/"codex.jsonl"),unified_usage_ledger=str(usage),quota_ledger=str(quotas)).test_client()
    data=client.get("/api/unified-usage?provider=anthropic&harness=hermes&days=30").get_json()
    assert data["totals"]["input_tokens"]==10 and data["totals"]["total_tokens"]==19
    assert data["totals"]["estimated_cost_usd"] == "0"
    assert data["coverage"]=={"exact_events":1,"reconstructed_events":0,"reconstructed_calls":0}
    assert data["window"] == {
        "first_occurred_at": recent_at,
        "last_occurred_at": recent_at,
        "event_count": 1,
        "days": 30,
    }
    assert data["by_harness"] == [{
        "harness": "hermes", "events": 1,
        "input_tokens": 10, "cache_read_tokens": 5, "cache_write_tokens": 0,
        "output_tokens": 4, "reasoning_tokens": 2, "total_tokens": 19,
        "estimated_cost_usd": "0", "actual_cost_usd": "0",
    }]
    assert list(data["by_provider_model"])[0]["provider"]=="anthropic"
    # Endpoint attribution surfaces per (provider, model); NULL groups as
    # "unknown" (pre-2026-08-29 Hermes rows are NULL forever by decision).
    assert data["by_provider_model"][0]["api_modes"] == {"anthropic_messages": 1}
    subscriptions=client.get("/api/subscriptions").get_json()
    assert subscriptions["observations"][0]["used_value"]=="21"
    assert subscriptions["latest"][0]["used_value"] == "21"
    assert "source_namespace" not in subscriptions["latest"][0]
    assert "account_ref" not in subscriptions["latest"][0]
    assert "provider_payload_ref" not in subscriptions["latest"][0]
    assert "x_private" not in subscriptions["latest"][0]
    assert client.get("/api/unified-usage?days=-1").status_code == 400


def test_private_dashboard_labels_stale_usage_and_quota_ledgers(tmp_path, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 14, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)
    usage = tmp_path / "usage.jsonl"
    quota = tmp_path / "quota.jsonl"
    usage.write_text(canonical_json(dict(
        _usage_fact("old"),
        occurred_at="2026-07-14T06:00:00Z",
        recorded_at="2026-07-14T06:00:00Z",
    )) + "\n", encoding="utf-8")
    quota.write_text(canonical_json({
        "fact_type": "quota_observation_v1",
        "schema_version": 1,
        "source_namespace": "quota",
        "source_observation_id": "old",
        "harness": "test",
        "observed_at": "2026-07-14T06:00:00Z",
        "provider": "example",
        "quota_name": "weekly",
        "quota_scope": "account",
        "window_kind": "rolling",
        "unit": "percent",
        "measurement_confidence": "exact",
    }) + "\n", encoding="utf-8")
    stale_mtime = datetime(2026, 7, 14, 6, 0, 0, tzinfo=timezone.utc).timestamp()
    os.utime(usage, (stale_mtime, stale_mtime))
    os.utime(quota, (stale_mtime, stale_mtime))
    client = create_app(unified_usage_ledger=str(usage), quota_ledger=str(quota)).test_client()

    usage_payload = client.get("/api/unified-usage?hours=3").get_json()
    quota_payload = client.get("/api/subscriptions?days=30&history=0").get_json()
    expected = {
        "status": "stale",
        "heartbeat_status": "stale",
        "latest_at": "2026-07-14T06:00:00Z",
        "max_age_seconds": 10800,
    }
    assert usage_payload["freshness"] == expected
    assert quota_payload["freshness"] == expected

    # React + Vite retarget: the stale notices render client-side from the
    # freshness payload asserted above; the notice strings live in the
    # frontend data hook.
    hook = frontend_surfaces.source("hooks/useDashboardData.ts")
    assert "Canonical usage is stale" in hook
    assert "Subscription observations are stale" in hook


def test_private_dashboard_uses_success_heartbeats_for_noop_collection_freshness(tmp_path, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 14, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)
    usage = tmp_path / "usage.jsonl"
    quota = tmp_path / "quota.jsonl"
    usage.write_text(canonical_json(_usage_fact("old")) + "\n", encoding="utf-8")
    quota.write_text("", encoding="utf-8")
    old = datetime(2026, 7, 14, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    os.utime(usage, (old, old))
    os.utime(quota, (old, old))
    health = tmp_path / "health"
    health.mkdir()
    usage_ok = health / "usage-collect.ok"
    quota_ok = health / "quota-collect.ok"
    usage_ok.touch()
    quota_ok.touch()
    recent = datetime(2026, 7, 14, 11, 50, 0, tzinfo=timezone.utc).timestamp()
    os.utime(usage_ok, (recent, recent))
    os.utime(quota_ok, (recent, recent))
    client = create_app(unified_usage_ledger=str(usage), quota_ledger=str(quota)).test_client()

    # With a collector heartbeat the write threshold is one ledger tick
    # (15 minutes plus 2 of timer slack; keeper 2026-09-25); the provider
    # cards keep their three-hour observation threshold.
    expected = {
        "status": "fresh",
        "heartbeat_status": "fresh",
        "latest_at": "2026-07-14T11:50:00Z",
        "max_age_seconds": 1020,
        "threshold_seconds": 10800,
        "run_in_progress_since": None,
    }
    assert client.get("/api/unified-usage?hours=3").get_json()["freshness"] == expected
    assert client.get("/api/subscriptions?days=30&history=0").get_json()["freshness"] == expected


def _freshness_client(tmp_path, monkeypatch, *, lane_health=None, heartbeat_at=None):
    """A dashboard over one usage ledger, optionally with collector lane health."""

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 14, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)
    usage = tmp_path / "usage.jsonl"
    usage.write_text(canonical_json(_usage_fact("old")) + "\n", encoding="utf-8")
    old = datetime(2026, 7, 14, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    os.utime(usage, (old, old))
    health = tmp_path / "health"
    health.mkdir(exist_ok=True)
    heartbeat = health / "usage-collect.ok"
    heartbeat.touch()
    stamp = (heartbeat_at or datetime(2026, 7, 14, 11, 50, 0, tzinfo=timezone.utc)).timestamp()
    os.utime(heartbeat, (stamp, stamp))
    if lane_health is not None:
        (health / "lanes-usage.json").write_text(
            json.dumps(lane_health), encoding="utf-8"
        )
    return create_app(unified_usage_ledger=str(usage)).test_client()


def _lane_health(**last_success_at):
    return {
        "schema_version": 1,
        "scope": "usage",
        "updated_at": "2026-07-14T11:50:00Z",
        "lanes": {
            lane: {
                "last_run_at": "2026-07-14T11:50:00Z",
                "last_success_at": value,
                "last_error_class": None if value else "IdentityConflictError",
                "rows_written": 3,
                "quarantined_count": 0 if value else 46,
                "discovered": 12,
            }
            for lane, value in last_success_at.items()
        },
    }


def test_freshness_reports_every_lane_when_health_is_present_and_current(tmp_path, monkeypatch):
    client = _freshness_client(
        tmp_path,
        monkeypatch,
        lane_health=_lane_health(
            hermes="2026-07-14T11:50:00Z",
            claude_code="2026-07-14T11:50:00Z",
        ),
    )

    freshness = client.get("/api/unified-usage?hours=3").get_json()["freshness"]

    assert freshness["status"] == "fresh"
    assert freshness["stale_lanes"] == []
    assert [lane["lane"] for lane in freshness["lanes"]] == ["claude_code", "hermes"]
    assert all(lane["status"] == "fresh" for lane in freshness["lanes"])
    assert freshness["lanes"][0]["age_seconds"] == 600


def test_freshness_names_the_stale_lane_behind_a_healthy_heartbeat(tmp_path, monkeypatch):
    # The exact state the pipeline used to hide: writes land on schedule while
    # one lane has not succeeded in days.
    client = _freshness_client(
        tmp_path,
        monkeypatch,
        lane_health=_lane_health(
            hermes="2026-07-14T11:50:00Z",
            claude_code_secondary=None,
            opencode="2026-07-11T11:30:00Z",
        ),
    )

    freshness = client.get("/api/unified-usage?hours=3").get_json()["freshness"]

    assert freshness["status"] == "stale"
    # The write-level state stays visible on its own: the surface is stale
    # because a lane went quiet, not because writes stopped (the subscription
    # charts gate on this field so one dead lane cannot blank them all).
    assert freshness["heartbeat_status"] == "fresh"
    assert freshness["stale_lanes"] == ["opencode"]
    assert freshness["never_lanes"] == ["claude_code_secondary"]
    by_lane = {lane["lane"]: lane for lane in freshness["lanes"]}
    assert by_lane["claude_code_secondary"]["status"] == "never"
    assert by_lane["claude_code_secondary"]["age_seconds"] is None
    assert by_lane["claude_code_secondary"]["quarantined_count"] == 46
    assert by_lane["claude_code_secondary"]["last_error_class"] == "IdentityConflictError"
    assert by_lane["opencode"]["status"] == "stale"
    assert by_lane["hermes"]["status"] == "fresh"


def test_freshness_projects_the_ceded_flag_so_the_ui_can_render_it(tmp_path, monkeypatch):
    # Graceful cede (keeper-ruled 2026-08-29): a lane skipped after exhausted
    # retries must reach the screen — the projection carries `ceded` verbatim
    # and defaults it to False when the collector predates the field.
    lane_health = _lane_health(
        hermes="2026-07-14T11:50:00Z",
        claude_code="2026-07-14T11:50:00Z",
    )
    lane_health["lanes"]["hermes"]["ceded"] = True
    lane_health["lanes"]["hermes"]["last_error_class"] = "TimeoutError"
    client = _freshness_client(tmp_path, monkeypatch, lane_health=lane_health)

    freshness = client.get("/api/unified-usage?hours=3").get_json()["freshness"]

    by_lane = {lane["lane"]: lane for lane in freshness["lanes"]}
    assert by_lane["hermes"]["ceded"] is True
    assert by_lane["hermes"]["last_error_class"] == "TimeoutError"
    assert by_lane["claude_code"]["ceded"] is False


def test_freshness_falls_back_to_the_heartbeat_when_lane_health_is_absent(tmp_path, monkeypatch):
    client = _freshness_client(tmp_path, monkeypatch)

    freshness = client.get("/api/unified-usage?hours=3").get_json()["freshness"]

    assert freshness == {
        "status": "fresh",
        "heartbeat_status": "fresh",
        "latest_at": "2026-07-14T11:50:00Z",
        "max_age_seconds": 1020,
        "threshold_seconds": 10800,
        "run_in_progress_since": None,
    }


def test_freshness_ignores_unreadable_or_unknown_lane_health(tmp_path, monkeypatch):
    client = _freshness_client(
        tmp_path, monkeypatch, lane_health={"schema_version": 2, "lanes": {"x": {}}}
    )
    assert "lanes" not in client.get("/api/unified-usage?hours=3").get_json()["freshness"]

    health = tmp_path / "health" / "lanes-usage.json"
    health.write_text("{ not json", encoding="utf-8")
    assert "lanes" not in client.get("/api/unified-usage?hours=3").get_json()["freshness"]


def _chart_fact(source_id, occurred_at, *, harness, provider, model_requested, model_reported=None, **tokens):
    row = dict(
        _usage_fact(source_id),
        occurred_at=occurred_at,
        recorded_at=occurred_at,
        harness=harness,
        provider=provider,
        model_requested=model_requested,
        model_reported=model_reported,
        input_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
    )
    row.update(tokens)
    return row


def test_unified_usage_charts_split_providers_without_double_counting_reasoning(
    tmp_path, monkeypatch
):
    """The Codex-vs-Claude two-series comparison was retired with its card
    (keeper ruling 2026-08-30); the provider donuts read share from
    ``by_provider`` instead. Same fixture, same window semantics, now pinning
    the projection that replaced it — plus the substitution watch, whose
    NULL-means-no-claim rule these rows exercise directly."""
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 14, 12, 4, 56, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)
    rows = [
        _chart_fact(
            "codex", "2026-07-14T09:00:00Z", harness="hermes", provider="openai-codex",
            model_requested="requested-codex", model_reported="reported-codex",
            input_tokens=10, cache_read_tokens=5, cache_write_tokens=2, output_tokens=3,
            reasoning_tokens=2,
        ),
        _chart_fact(
            "claude", "2026-07-14T10:30:00Z", harness="claude_code", provider="anthropic",
            model_requested="claude-sonnet", model_reported="claude-sonnet",
            input_tokens=7, output_tokens=4,
        ),
        _chart_fact(
            "other-hermes", "2026-07-14T11:00:00Z", harness="hermes", provider="anthropic",
            model_requested="direct-anthropic", input_tokens=2, output_tokens=1,
        ),
        _chart_fact(
            "overlap", "2026-07-14T11:30:00Z", harness="claude_code", provider="openai-codex",
            model_requested="overlap-model", input_tokens=5,
        ),
        _chart_fact(
            "too-old", "2026-07-14T08:59:59Z", harness="claude_code", provider="anthropic",
            model_requested="old", input_tokens=900,
        ),
        _chart_fact(
            "partial-hour", "2026-07-14T12:00:00Z", harness="hermes", provider="openai-codex",
            model_requested="future", input_tokens=800,
        ),
    ]
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text("\n".join(canonical_json(row) for row in rows) + "\n", encoding="utf-8")
    client = create_app(unified_usage_ledger=str(ledger)).test_client()

    response = client.get("/api/unified-usage?hours=3")
    assert response.status_code == 200
    data = response.get_json()
    series = data["time_series"]
    assert series["bucket_starts"] == [
        f"2026-07-14T{hour:02d}:{minute:02d}:00Z"
        for hour in (9, 10, 11)
        for minute in (0, 15, 30, 45)
    ]
    assert series["window_start"] == "2026-07-14T09:00:00Z"
    assert series["window_end"] == "2026-07-14T12:00:00Z"
    assert series["bucket_minutes"] == 15 and series["bucket_count"] == 12
    assert data["totals"]["total_tokens"] == 39
    assert data["totals"]["reasoning_tokens"] == 2
    models = {(row["provider"], row["model"]): row for row in series["model_series"]}
    # 09:00 -> bucket 0; 10:30 -> bucket 6 on the 15-minute grid.
    assert models[("openai-codex", "reported-codex")]["values"] == [20] + [0] * 11
    assert models[("anthropic", "claude-sonnet")]["values"] == [0] * 6 + [11] + [0] * 5
    aggregate_models = {(row["provider"], row["model"]): row for row in data["by_provider_model"]}
    assert aggregate_models[("openai-codex", "reported-codex")]["total_tokens"] == 20
    assert ("openai-codex", "requested-codex") not in aggregate_models
    # The retired two-series comparison is gone from the payload, not merely
    # unrendered — it existed only to feed the deleted card.
    assert "comparison_series" not in series
    assert "comparison_excluded_tokens" not in series

    # by_provider replaces it: provider share with the cached/fresh split the
    # donut draws, ordered by throughput so the arcs are deterministic.
    by_provider = {row["provider"]: row for row in data["by_provider"]}
    assert [row["provider"] for row in data["by_provider"]] == ["openai-codex", "anthropic"]
    assert by_provider["openai-codex"]["events"] == 2
    assert by_provider["openai-codex"]["total_tokens"] == 25
    assert by_provider["openai-codex"]["cached_tokens"] == 5
    assert by_provider["openai-codex"]["fresh_tokens"] == 20
    # Reasoning is diagnostic and is not counted twice here either.
    assert by_provider["openai-codex"]["reasoning_tokens"] == 2
    assert by_provider["anthropic"]["total_tokens"] == 14
    assert by_provider["anthropic"]["cached_tokens"] == 0
    assert by_provider["anthropic"]["fresh_tokens"] == 14
    # The canonical key the donut legend joins a quota row on.
    assert by_provider["openai-codex"]["provider_canonical"] == "openai-codex"
    assert by_provider["anthropic"]["provider_canonical"] == "anthropic"
    assert (
        sum(row["total_tokens"] for row in data["by_provider"])
        == data["totals"]["total_tokens"]
    )

    # Substitution watch: only "codex" reports a served model that differs from
    # the requested one. "claude" reports a matching model, and the two rows
    # with a NULL model_reported make no claim at all and never count.
    assert data["totals"]["substituted_events"] == 1
    assert by_provider["openai-codex"]["substituted_events"] == 1
    assert by_provider["anthropic"]["substituted_events"] == 0
    assert "source_namespace" not in json.dumps(series)


def test_unified_usage_hour_windows_carry_daily_list_price_estimates(tmp_path, monkeypatch):
    """Polish round 2026-08-30 (item 12): hour-window queries carry a
    ``list_price_daily`` array — 24-hour slices of the queried UTC hour
    window, each priced with estimate_list_price over that day's
    (provider, model) aggregates. Empty days price honestly at $0.00 with
    full coverage so the spend sparkline keeps calendar shape; unpriced
    tokens stay per-day coverage, never a guess."""

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 14, 12, 34, 56, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)
    rows = [
        # First day bucket of the 168h window ([07-07T12:00, 07-08T12:00)).
        _chart_fact(
            "sonnet-day0", "2026-07-07T13:00:00Z", harness="claude_code",
            provider="anthropic", model_requested="claude-sonnet-5",
            model_reported="claude-sonnet-5", input_tokens=1_000_000,
        ),
        # Final day bucket: one priced and one unpriced provider.
        _chart_fact(
            "glm-day6", "2026-07-13T13:00:00Z", harness="opencode",
            provider="zai", model_requested="glm-5.3", model_reported="glm-5.3",
            output_tokens=1_000_000,
        ),
        _chart_fact(
            "mystery-day6", "2026-07-13T14:00:00Z", harness="opencode",
            provider="mystery-lab", model_requested="whoknows",
            input_tokens=1_000_000,
        ),
    ]
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text("\n".join(canonical_json(row) for row in rows) + "\n", encoding="utf-8")
    client = create_app(unified_usage_ledger=str(ledger)).test_client()

    payload = client.get("/api/unified-usage?hours=168").get_json()
    daily = payload["list_price_daily"]
    # UTC calendar days: hours=168 ending 12:00Z spans eight dates, the first
    # and last partial. 24-hour slices anchored at the window start were
    # retired with the date labels (2026-09-02).
    assert [entry["day_start"] for entry in daily] == [
        "2026-07-07T00:00:00Z", "2026-07-08T00:00:00Z", "2026-07-09T00:00:00Z",
        "2026-07-10T00:00:00Z", "2026-07-11T00:00:00Z", "2026-07-12T00:00:00Z",
        "2026-07-13T00:00:00Z", "2026-07-14T00:00:00Z",
    ]
    assert [entry["partial"] for entry in daily] == [True] + [False] * 6 + [True]
    # claude-sonnet-5 input lists at $2/MTok; glm-5.3 output at $4.40/MTok.
    assert daily[0]["estimate_usd"] == "2.00"
    assert daily[0]["priced_token_share"] == 1.0
    assert daily[0]["unpriced_providers"] == []
    assert [entry["estimate_usd"] for entry in daily[1:6]] == ["0.00"] * 5
    assert daily[6]["estimate_usd"] == "4.40"
    # The unpriced provider is that day's coverage and never leaks into
    # another day's share.
    assert daily[6]["unpriced_providers"] == ["mystery-lab"]
    assert 0.4 < daily[6]["priced_token_share"] < 0.6
    assert daily[7]["estimate_usd"] == "0.00"
    # The weekly total the tile shows still rides totals.list_price.
    assert payload["totals"]["list_price"]["estimate_usd"] == "6.40"
    # Day-window queries have no hour-anchored series and carry no array.
    assert "list_price_daily" not in client.get("/api/unified-usage?days=30").get_json()


def test_bucket_series_sums_the_five_token_buckets_across_all_providers(
    tmp_path, monkeypatch
):
    """Estate-wide token composition (keeper-ruled 2026-08-30, replacing the
    codex-only Token bucket mix): per 15-minute bucket, the five canonical token
    sums across every provider, on the same UTC grid as model_series and
    provider_status_series. Reasoning stays a separate series here — the
    chart shows composition, and the accounted total still never counts it."""

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 14, 12, 4, 56, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)
    rows = [
        _chart_fact(
            "codex", "2026-07-14T09:00:00Z", harness="hermes", provider="openai-codex",
            model_requested="requested-codex", input_tokens=10, cache_read_tokens=5,
            cache_write_tokens=2, output_tokens=3, reasoning_tokens=2,
        ),
        _chart_fact(
            "claude", "2026-07-14T10:30:00Z", harness="claude_code", provider="anthropic",
            model_requested="claude-sonnet", input_tokens=7, output_tokens=4,
        ),
        _chart_fact(
            "other-hermes", "2026-07-14T11:00:00Z", harness="hermes", provider="anthropic",
            model_requested="direct-anthropic", input_tokens=2, output_tokens=1,
        ),
        _chart_fact(
            "overlap", "2026-07-14T11:30:00Z", harness="claude_code", provider="openai-codex",
            model_requested="overlap-model", input_tokens=5,
        ),
        # Outside the complete-hour window in both directions: never summed.
        _chart_fact(
            "too-old", "2026-07-14T08:59:59Z", harness="claude_code", provider="anthropic",
            model_requested="old", input_tokens=900,
        ),
        _chart_fact(
            "partial-hour", "2026-07-14T12:00:00Z", harness="hermes", provider="openai-codex",
            model_requested="future", input_tokens=800,
        ),
    ]
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text("\n".join(canonical_json(row) for row in rows) + "\n", encoding="utf-8")
    client = create_app(unified_usage_ledger=str(ledger)).test_client()

    response = client.get("/api/unified-usage?hours=3")
    assert response.status_code == 200
    series = response.get_json()["time_series"]
    bucket_series = series["bucket_series"]

    # The five buckets, summed across providers per complete 15-minute UTC
    # bucket: 09:00 -> 0, 10:30 -> 6, 11:00 -> 8, 11:30 -> 10.
    assert bucket_series == {
        "input_tokens": [10, 0, 0, 0, 0, 0, 7, 0, 2, 0, 5, 0],
        "cache_read_tokens": [5, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        "cache_write_tokens": [2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        "output_tokens": [3, 0, 0, 0, 0, 0, 4, 0, 1, 0, 0, 0],
        "reasoning_tokens": [2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    }
    # Same bucket grid as every other series in the payload.
    assert all(
        len(values) == series["bucket_count"] for values in bucket_series.values()
    )
    # The window edges never leak in: neither the 800 nor the 900 appears.
    flat = [value for values in bucket_series.values() for value in values]
    assert 800 not in flat and 900 not in flat


def test_provider_status_series_buckets_traffic_errors_and_dominant_class(
    tmp_path, monkeypatch
):
    """The status strip reads uptime off request_status/error_class, which ride
    every canonical usage event. Only the explicit ``error`` status is a
    failure: ``unknown`` is a source that does not report status and
    ``cancelled`` is the operator stopping a call — neither is the provider
    failing, and neither may dim a cell that carried traffic."""

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 14, 12, 5, 0, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)
    rows = [
        # 09:00 — clean hour for zai.
        _chart_fact("z-ok-1", "2026-07-14T09:10:00Z", harness="hermes", provider="zai",
                    model_requested="glm", input_tokens=4, request_status="ok"),
        _chart_fact("z-ok-2", "2026-07-14T09:20:00Z", harness="hermes", provider="zai",
                    model_requested="glm", input_tokens=4, request_status="unknown"),
        # 10:00 — mixed hour: two failures against three events, two classes,
        # the more frequent one dominating.
        _chart_fact("z-err-1", "2026-07-14T10:05:00Z", harness="hermes", provider="zai",
                    model_requested="glm", request_status="error",
                    error_class="APITimeoutError"),
        _chart_fact("z-err-2", "2026-07-14T10:06:00Z", harness="hermes", provider="zai",
                    model_requested="glm", request_status="error",
                    error_class="APITimeoutError"),
        _chart_fact("z-err-3", "2026-07-14T10:07:00Z", harness="hermes", provider="zai",
                    model_requested="glm", request_status="error",
                    error_class="RateLimitError"),
        _chart_fact("z-ok-3", "2026-07-14T10:40:00Z", harness="hermes", provider="zai",
                    model_requested="glm", input_tokens=1, cache_read_tokens=6,
                    request_status="ok"),
        # 11:00 — a cancel and a classless error: cancelled is not a failure,
        # and an error with no class still counts as a failure.
        _chart_fact("z-cancel", "2026-07-14T11:01:00Z", harness="hermes", provider="zai",
                    model_requested="glm", request_status="cancelled"),
        _chart_fact("z-err-4", "2026-07-14T11:02:00Z", harness="hermes", provider="zai",
                    model_requested="glm", request_status="error", error_class=None),
        # A second, quieter provider — one row per hour, always clean.
        _chart_fact("a-ok", "2026-07-14T11:30:00Z", harness="claude_code",
                    provider="anthropic", model_requested="claude", input_tokens=2,
                    request_status="ok"),
        # Outside the three complete hours in both directions.
        _chart_fact("too-old", "2026-07-14T08:59:59Z", harness="hermes", provider="zai",
                    model_requested="glm", request_status="error",
                    error_class="ShouldNotAppear"),
        _chart_fact("partial-hour", "2026-07-14T12:00:00Z", harness="hermes",
                    provider="zai", model_requested="glm", request_status="error",
                    error_class="ShouldNotAppear"),
    ]
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text("\n".join(canonical_json(row) for row in rows) + "\n", encoding="utf-8")
    client = create_app(unified_usage_ledger=str(ledger)).test_client()

    response = client.get("/api/unified-usage?hours=3")
    assert response.status_code == 200
    series = response.get_json()["time_series"]
    status = {row["provider"]: row for row in series["provider_status_series"]}

    # Busiest provider first, so the strip's row order is deterministic.
    # Rows key on the canonical provider id ("zai" raw -> "z-ai-glm"), so
    # the strip joins to the quota cards without an alias table in the
    # browser (2026-09-02).
    assert [row["provider"] for row in series["provider_status_series"]] == ["z-ai-glm", "anthropic"]

    # Twelve 15-minute buckets from 09:00Z: 09:10/09:20 -> 0/1, the 10:05-10:07
    # cluster -> 4, 10:40 -> 6, 11:01/11:02 -> 8, 11:30 -> 10.
    zai = status["z-ai-glm"]
    assert zai["events"] == [1, 1, 0, 0, 3, 0, 1, 0, 2, 0, 0, 0]
    assert zai["errors"] == [0, 0, 0, 0, 3, 0, 0, 0, 1, 0, 0, 0]
    assert zai["top_error_classes"] == [None] * 4 + ["APITimeoutError"] + [None] * 7
    assert zai["total_events"] == 8 and zai["total_errors"] == 4
    assert zai["dominant_error_class"] == "APITimeoutError"
    assert zai["provider_canonical"] == "z-ai-glm"
    # Per-bucket prompt-side token sums for the estate-wide cache-hit chart
    # (keeper-ruled 2026-08-30): input and cache read ride the same series,
    # and every row's tokens count regardless of its request_status.
    assert zai["input_tokens"] == [4, 4, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0]
    assert zai["cache_read_tokens"] == [0, 0, 0, 0, 0, 0, 6, 0, 0, 0, 0, 0]

    anthropic = status["anthropic"]
    assert anthropic["events"] == [0] * 10 + [1, 0]
    assert anthropic["errors"] == [0] * 12
    assert anthropic["top_error_classes"] == [None] * 12
    assert anthropic["dominant_error_class"] is None
    assert anthropic["input_tokens"] == [0] * 10 + [2, 0]
    assert anthropic["cache_read_tokens"] == [0] * 12

    # The out-of-window rows never reach the strip.
    assert "ShouldNotAppear" not in json.dumps(series)
    # Buckets align with the model series the strip sits beside.
    assert all(
        len(row["events"]) == series["bucket_count"]
        and len(row["errors"]) == series["bucket_count"]
        and len(row["top_error_classes"]) == series["bucket_count"]
        and len(row["input_tokens"]) == series["bucket_count"]
        and len(row["cache_read_tokens"]) == series["bucket_count"]
        for row in series["provider_status_series"]
    )


def test_substituted_events_never_counts_a_null_reported_model(tmp_path, monkeypatch):
    """NULL model_reported is the source declining to name a served model. It is
    not a claim that the served model matched, and it is not a substitution —
    it is simply not evidence, so it may never move this count in either
    direction. (Every pre-2026-08-29 row is NULL forever by decision.)"""

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 14, 12, 5, 0, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)
    rows = [
        # Served != requested: the one true substitution.
        _chart_fact("served-other", "2026-07-14T11:00:00Z", harness="hermes",
                    provider="zai", model_requested="glm-5.3",
                    model_reported="glm-5.3-flash", input_tokens=1),
        # Served == requested: a positive claim of no substitution.
        _chart_fact("served-same", "2026-07-14T11:01:00Z", harness="hermes",
                    provider="zai", model_requested="glm-5.3",
                    model_reported="glm-5.3", input_tokens=1),
        # No claim at all — must not count, even though the requested model is
        # the same one that was substituted away from above.
        _chart_fact("no-claim", "2026-07-14T11:02:00Z", harness="hermes",
                    provider="zai", model_requested="glm-5.3", input_tokens=1),
        # No claim and no requested model either: still not a substitution.
        _chart_fact("no-claim-either-side", "2026-07-14T11:03:00Z", harness="hermes",
                    provider="zai", model_requested=None, input_tokens=1),
    ]
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text("\n".join(canonical_json(row) for row in rows) + "\n", encoding="utf-8")
    client = create_app(unified_usage_ledger=str(ledger)).test_client()

    data = client.get("/api/unified-usage?hours=3").get_json()
    assert data["window"]["event_count"] == 4
    assert data["totals"]["substituted_events"] == 1
    # by_provider groups on the canonical id (raw "zai" -> "z-ai-glm"), so
    # the substitution watch joins to the same provider arc as the donut.
    assert {row["provider"]: row["substituted_events"] for row in data["by_provider"]} == {
        "z-ai-glm": 1
    }


def test_substitution_watch_ignores_moa_council_presets(tmp_path, monkeypatch):
    """A MoA request names a preset ("council"); the served model is always one
    of its members, so it is never a substitution (keeper-approved UI review,
    2026-09-25). A real swap on another provider still counts."""

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 14, 12, 5, 0, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)
    rows = [
        _chart_fact("council-run", "2026-07-14T11:00:00Z", harness="hermes",
                    provider="moa", model_requested="council",
                    model_reported="glm-5.3", input_tokens=1),
        _chart_fact("real-swap", "2026-07-14T11:01:00Z", harness="hermes",
                    provider="zai", model_requested="glm-5.3",
                    model_reported="glm-5.3-flash", input_tokens=1),
    ]
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text("\n".join(canonical_json(row) for row in rows) + "\n", encoding="utf-8")
    client = create_app(unified_usage_ledger=str(ledger)).test_client()

    data = client.get("/api/unified-usage?hours=3").get_json()
    assert data["totals"]["substituted_events"] == 1
    by_provider = {row["provider"]: row["substituted_events"] for row in data["by_provider"]}
    assert by_provider["moa"] == 0
    assert by_provider["z-ai-glm"] == 1


def test_unified_usage_chart_collapses_models_deterministically(tmp_path, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 14, 12, 5, 0, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)
    # Seven models no longer collapse: since 2026-09-06 the API carries every
    # model up to _UNIFIED_MODEL_SERIES_LIMIT and the browser chooses how many
    # to draw, so a low-volume model is reachable instead of pre-merged away.
    rows = [
        _chart_fact(
            f"m{index}", "2026-07-14T11:00:00Z", harness="opencode", provider=f"p{index}",
            model_requested=f"m{index}", input_tokens=10 - index,
        )
        for index in range(7)
    ]
    ledger = tmp_path / "usage.sqlite3"
    append_sqlite_facts(ledger, rows, fact_type="usage_event_v1")
    data = create_app(unified_usage_ledger=str(ledger)).test_client().get(
        "/api/unified-usage?hours=1"
    ).get_json()["time_series"]

    assert [(row["provider"], row["model"]) for row in data["model_series"]] == [
        (f"p{index}", f"m{index}") for index in range(7)
    ]
    assert data["other_model_series"] is None
    assert data["model_series_default_visible"] == 5

    # Past the bounded cap the collapse is still deterministic: strictly the
    # lowest-volume tail, in rank order, merged into one grouping bar.
    limit = dashboard_module._UNIFIED_MODEL_SERIES_LIMIT
    overflow = [
        _chart_fact(
            f"n{index}", "2026-07-14T11:00:00Z", harness="opencode", provider=f"q{index:03d}",
            model_requested=f"n{index:03d}", input_tokens=1000 - index,
        )
        for index in range(limit + 2)
    ]
    big_ledger = tmp_path / "overflow.sqlite3"
    append_sqlite_facts(big_ledger, overflow, fact_type="usage_event_v1")
    data = create_app(unified_usage_ledger=str(big_ledger)).test_client().get(
        "/api/unified-usage?hours=1"
    ).get_json()["time_series"]

    assert [row["model"] for row in data["model_series"]] == [
        f"n{index:03d}" for index in range(limit)
    ]
    assert data["other_model_series"] == {
        "label": "Other models (chart grouping, 2)",
        "member_count": 2,
        # The two smallest: 1000-limit and 999-limit.
        "total_tokens": (1000 - limit) + (999 - limit),
        "values": [(1000 - limit) + (999 - limit), 0, 0, 0],
    }


@pytest.mark.parametrize("query", ["hours=0", "hours=169", "hours=nope", "hours=1.5", "hours=3&days=1"])
def test_unified_usage_rejects_invalid_hourly_chart_windows(tmp_path, query):
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text("", encoding="utf-8")
    response = create_app(unified_usage_ledger=str(ledger)).test_client().get(
        "/api/unified-usage?" + query
    )
    assert response.status_code == 400


def test_private_aggregate_endpoints_require_explicit_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("UNIFIED_USAGE_LEDGER_PATH", raising=False)
    monkeypatch.delenv("QUOTA_LEDGER_PATH", raising=False)
    monkeypatch.delenv("BILLING_LEDGER_PATH", raising=False)
    client = create_app(atrium_root=str(tmp_path), ledger=str(tmp_path / "codex.jsonl")).test_client()
    assert client.get("/api/unified-usage").status_code == 404
    assert client.get("/api/subscriptions").status_code == 404
    assert client.get("/api/billing").status_code == 404


def _billing_fact(source_id, kind, amount, currency="USD", **updates):
    row = {
        "fact_type": "billing_fact_v1",
        "schema_version": 1,
        "source_namespace": "private:test-billing",
        "source_billing_fact_id": source_id,
        "provider": "openrouter",
        "account_ref": "private-account",
        "occurred_at": "2026-07-12T12:00:00Z",
        "billing_period_start": "2026-07-01T00:00:00Z",
        "billing_period_end": "2026-08-01T00:00:00Z",
        "invoice_id": "private-invoice",
        "line_item_id": "private-line",
        "transaction_kind": kind,
        "status": "posted",
        "amount": amount,
        "currency": currency,
        "usage_event_refs": [{"source_namespace": "usage", "source_event_id": "u-1"}],
        "description_code": "api_usage",
        "provider_receipt_id": "private-receipt",
        "x_private": "never-return",
    }
    row.update(updates)
    return row


def test_private_sqlite_endpoints_match_jsonl_and_unknown_suffix_fails_closed(tmp_path):
    usage_rows = [
        dict(_usage_fact("u-1"), provider="openrouter", model_requested="requested", model_reported="reported", input_tokens=3),
        dict(_usage_fact("u-2"), provider="other", harness="other", purpose="subagent", model_requested="other", input_tokens=7),
    ]
    quota_rows = [{
        "fact_type": "quota_observation_v1", "schema_version": 1,
        "source_namespace": "quota", "source_observation_id": "q-1", "harness": "test",
        "observed_at": "2026-07-12T12:00:00Z", "provider": "openrouter", "quota_name": "weekly",
        "quota_scope": "account", "window_kind": "rolling", "unit": "percent",
        "measurement_confidence": "exact", "used_value": "25",
    }]
    usage_jsonl = tmp_path / "usage.jsonl"
    quota_jsonl = tmp_path / "quota.jsonl"
    usage_jsonl.write_text("\n".join(canonical_json(row) for row in usage_rows) + "\n", encoding="utf-8")
    quota_jsonl.write_text("\n".join(canonical_json(row) for row in quota_rows) + "\n", encoding="utf-8")
    usage_db = tmp_path / "usage.sqlite3"
    quota_db = tmp_path / "quota.db"
    append_sqlite_facts(usage_db, usage_rows, fact_type="usage_event_v1")
    append_sqlite_facts(quota_db, quota_rows, fact_type="quota_observation_v1")

    query = "?provider=openrouter&harness=test&purpose=main&model_requested=requested&days=36500"
    jsonl_client = create_app(unified_usage_ledger=str(usage_jsonl), quota_ledger=str(quota_jsonl)).test_client()
    sqlite_client = create_app(unified_usage_ledger=str(usage_db), quota_ledger=str(quota_db)).test_client()
    assert sqlite_client.get("/api/unified-usage" + query).get_json() == jsonl_client.get("/api/unified-usage" + query).get_json()
    assert sqlite_client.get("/api/subscriptions?provider=openrouter&quota_name=weekly&days=36500").get_json() == jsonl_client.get("/api/subscriptions?provider=openrouter&quota_name=weekly&days=36500").get_json()

    bad = create_app(unified_usage_ledger=str(tmp_path / "usage.txt")).test_client()
    response = bad.get("/api/unified-usage")
    assert response.status_code == 400
    assert response.get_json() == {"error": "unsupported ledger suffix"}


def test_subscriptions_bounded_latest_only_option_filters_sqlite_and_jsonl_history(
    tmp_path, monkeypatch
):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 14, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)
    base = {
        "fact_type": "quota_observation_v1", "schema_version": 1,
        "source_namespace": "quota", "harness": "test", "provider": "example",
        "quota_scope": "account", "window_kind": "rolling", "unit": "percent",
        "measurement_confidence": "exact", "limit_value": "100", "remaining_value": "80",
        "used_value": "20",
    }
    rows = [
        dict(base, source_observation_id="old", quota_name="old_only", observed_at="2026-07-01T12:00:00Z"),
        dict(base, source_observation_id="recent", quota_name="weekly", observed_at="2026-07-13T12:00:00Z"),
    ]
    jsonl = tmp_path / "quota.jsonl"
    sqlite = tmp_path / "quota.sqlite3"
    jsonl.write_text("\n".join(canonical_json(row) for row in rows) + "\n", encoding="utf-8")
    append_sqlite_facts(sqlite, rows, fact_type="quota_observation_v1")

    clients = [create_app(quota_ledger=str(path)).test_client() for path in (jsonl, sqlite)]
    assert all(len(client.get("/api/subscriptions?days=30").get_json()["observations"]) == 2 for client in clients)
    # Unbounded reads default to the latest eight days (keeper ruling
    # 2026-08-29), so the 2026-07-01 observation drops out at frozen
    # 2026-07-14 while the 2026-07-13 one stays.
    defaulted = [client.get("/api/subscriptions").get_json() for client in clients]
    assert all(len(payload["observations"]) == 1 for payload in defaulted)
    assert all(
        payload["window"] == {"days": None, "default_days": 8, "next_before": "2026-07-06T12:00:00Z"}
        for payload in defaulted
    )
    bounded = [client.get("/api/subscriptions?days=2&history=0") for client in clients]

    assert all(response.status_code == 200 for response in bounded)
    assert bounded[0].get_json() == bounded[1].get_json()
    assert bounded[0].get_json()["observations"] == []
    assert [row["quota_name"] for row in bounded[0].get_json()["latest"]] == ["weekly"]


@pytest.mark.parametrize("query", ["days=nope&history=0", "days=-1&history=0", "days=2&history=nope"])
def test_subscriptions_rejects_malformed_bounded_options(tmp_path, query):
    quota = tmp_path / "quota.jsonl"
    quota.write_text("", encoding="utf-8")
    response = create_app(quota_ledger=str(quota)).test_client().get("/api/subscriptions?" + query)
    assert response.status_code == 400


def test_billing_api_signed_totals_safe_allowlist_filters_and_separate_costs(tmp_path):
    billing_db = tmp_path / "billing.sqlite"
    append_sqlite_facts(billing_db, [
        _billing_fact("charge", "charge", "12.50"),
        _billing_fact("refund", "refund", "-2.50"),
        _billing_fact("euro", "charge", "4", currency="EUR"),
    ], fact_type="billing_fact_v1")
    usage_db = tmp_path / "usage.sqlite3"
    append_sqlite_facts(usage_db, [dict(
        _usage_fact(), provider="openrouter", input_tokens=1,
        cost_status="actual", actual_cost_usd="99", billing_mode="payg",
    )], fact_type="usage_event_v1")
    client = create_app(unified_usage_ledger=str(usage_db), billing_ledger=str(billing_db)).test_client()

    result = client.get("/api/billing?provider=openrouter&status=posted&days=36500").get_json()
    assert result["totals_by_currency"] == [
        {"amount": "4", "currency": "EUR"},
        {"amount": "10", "currency": "USD"},
    ]
    assert result["totals_by_currency_and_transaction_kind"] == [
        {"amount": "4", "currency": "EUR", "transaction_kind": "charge"},
        {"amount": "12.5", "currency": "USD", "transaction_kind": "charge"},
        {"amount": "-2.5", "currency": "USD", "transaction_kind": "refund"},
    ]
    safe = {"provider", "occurred_at", "billing_period_start", "billing_period_end", "transaction_kind", "status", "amount", "currency", "description_code"}
    assert result["transactions"] and all(set(row) == safe for row in result["transactions"])
    assert "99" not in json.dumps(result)
    assert len(client.get("/api/billing?currency=EUR&transaction_kind=charge&days=36500").get_json()["transactions"]) == 1


@pytest.mark.parametrize("query", ["days=nope", "days=-1"])
def test_billing_api_rejects_malformed_days(tmp_path, query):
    ledger = tmp_path / "billing.jsonl"
    ledger.write_text(canonical_json(_billing_fact("charge", "charge", "1")) + "\n", encoding="utf-8")
    response = create_app(billing_ledger=str(ledger)).test_client().get("/api/billing?" + query)
    assert response.status_code == 400


def test_collect_all_dry_run_compact_summary(tmp_path, monkeypatch, capsys):
    state=tmp_path/"missing.db"; claude=tmp_path/"claude"; claude.mkdir()
    usage=tmp_path/"usage.jsonl"; quota=tmp_path/"quota.jsonl"; billing=tmp_path/"billing.jsonl"
    monkeypatch.setattr("sys.argv",["tracker","collect-all","--state-db",str(state),"--claude-root",str(claude),"--usage-ledger",str(usage),"--quota-ledger",str(quota),"--billing-ledger",str(billing),"--no-live-quota","--dry-run"])
    assert main()==0
    result=json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True and result["paths"]["usage_ledger"]==str(usage)
    assert result["paths"]["billing_ledger"] == str(billing)
    assert result["billing"] == {"appended": 0, "discovered": 0, "replayed": 0}
    assert not usage.exists()
    assert not billing.exists()
    assert not usage.with_name(f"{usage.name}.lock").exists()
    assert set(result["sources"]) >= {"hermes","claude_code","opencode","codex_quota"}


def test_latest_budget_state_filters_to_current_week(monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls,tz=None): return cls(2026,7,15,tzinfo=timezone.utc)
    monkeypatch.setattr("codex_usage_tracker.provider_spend.datetime",Frozen)
    result=latest_budget_state([
      {"billed_usd":99,"started_at":"2026-07-05T12:00:00Z"},
      {"billed_usd":2,"started_at":"2026-07-14T12:00:00Z"},
      {"billed_usd":3,"occurred_at":"2026-07-15T12:00:00+00:00"},
      {"billed_usd":4,"occurred_at":"not-a-time","started_at":"2026-07-15T13:00:00Z"},
    ])
    assert result["direct_provider_spend_usd"]==9


def test_collect_all_loads_and_persists_kimi_code_quota_without_secret_leak(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "KIMI_CODING_API_KEY=private-kimi-key\nUNRELATED_SECRET=must-not-load\n",
        encoding="utf-8",
    )
    captured = {}

    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {
                "usage": {
                    "limit": "100", "used": "21", "remaining": "79",
                    "resetTime": "2026-08-09T02:45:51.442531Z",
                },
                "limits": [{
                    "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                    "detail": {
                        "limit": "100", "used": "100",
                        "resetTime": "2026-08-02T07:45:51.442531Z",
                    },
                }],
            }

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, headers):
            captured["authorized"] = headers.get("Authorization") == "Bearer private-kimi-key"
            return Response()

    monkeypatch.setattr("codex_usage_tracker.quota.httpx.Client", Client)
    quota = tmp_path / "quota.sqlite3"
    result = collect_all(
        state_db=tmp_path / "missing-state.db",
        claude_root=tmp_path / "missing-claude",
        opencode_dbs=[],
        codex_ledger=tmp_path / "missing-codex.jsonl",
        usage_ledger=tmp_path / "out-of-scope-usage.sqlite3",
        quota_ledger=quota,
        dotenv=dotenv,
        claude_quota_command=None,
        live_quota=True,
        environment={},
        scope="quota",
    )

    kimi_rows = [
        row for row in read_sqlite_facts(quota, fact_type="quota_observation_v1")
        if row["provider"] == "kimi-coding"
    ]
    assert captured == {"authorized": True}
    assert result["sources"]["kimi_code_quota"] == {"discovered": 2}
    assert result["warnings"] == []
    assert {row["quota_name"] for row in kimi_rows} == {"five_hour", "week"}
    assert "private-kimi-key" not in json.dumps(result)
    assert "private-kimi-key" not in json.dumps(kimi_rows)
    assert "must-not-load" not in json.dumps(result)


def test_quota_dotenv_allowlist_adds_only_exact_new_inputs(tmp_path):
    dotenv = tmp_path / ".env"
    expected = {
        "OPENROUTER_API_KEY": "openrouter",
        "DEEPSEEK_API_KEY": "deepseek",
        "KIMI_CODING_API_KEY": "kimi",
        "Z_AI_API_KEY": "z-ai-primary",
        "ZAI_API_KEY": "z-ai-alias",
        "OPENCODE_GO_WORKSPACE_ID": "workspace-private",
        "OPENCODE_GO_AUTH_COOKIE": "cookie-private",
        "OPENCODE_GO_API_KEY": "go-key-private",
    }
    dotenv.write_text(
        "".join(f"{key}={value}\n" for key, value in expected.items())
        + "UNRELATED_SECRET=must-not-load\n",
        encoding="utf-8",
    )

    loaded = collector_module.load_allowed_dotenv(dotenv, environ={})

    assert loaded == expected
    assert collector_module.ALLOWED_DOTENV_KEYS == frozenset(expected)


def test_collect_all_prefers_exact_opencode_go_quota_when_both_credentials_exist(
    tmp_path, monkeypatch
):
    workspace_id = "private-opencode-workspace"
    auth_cookie = "private-opencode-cookie"
    captured = {}

    def collect_exact(workspace, cookie, namespace):
        captured.update(workspace=workspace, cookie=cookie, namespace=namespace)
        return _collector_quota_rows(
            "opencode-go", "opencode_go_api", namespace, ("five_hour", "week", "month")
        )

    def forbid_native(*args, **kwargs):
        raise AssertionError("the native reader must not run without OPENCODE_GO_API_KEY")

    monkeypatch.setattr(
        collector_module,
        "collect_exact_opencode_go_quota",
        collect_exact,
        raising=False,
    )
    monkeypatch.setattr(collector_module, "collect_opencode_go_usage_quota", forbid_native)
    result, quota = _collect_quota_only(
        tmp_path,
        environment={
            "OPENCODE_GO_WORKSPACE_ID": workspace_id,
            "OPENCODE_GO_AUTH_COOKIE": auth_cookie,
        },
    )

    facts = list(read_sqlite_facts(quota, fact_type="quota_observation_v1"))
    opencode_rows = [row for row in facts if row["provider"] == "opencode-go"]
    assert captured == {
        "workspace": workspace_id,
        "cookie": auth_cookie,
        "namespace": "sol:opencode-go",
    }
    assert result["sources"]["opencode_go_quota"] == {"discovered": 3}
    assert len(opencode_rows) == 3
    assert all(
        row["harness"] == "opencode_go_api"
        and row["measurement_confidence"] == "exact"
        for row in opencode_rows
    )
    serialized = json.dumps({"result": result, "facts": facts})
    assert workspace_id not in serialized
    assert auth_cookie not in serialized


def test_collect_all_reports_opencode_go_unavailable_without_credentials(
    tmp_path, monkeypatch
):
    """Keeper acceptance 2026-09-14: the first-use USD estimate is retired.
    Without a native key or a cookie pair the lane reports unavailable —
    no network call, no fabricated rows, no warning."""
    exact_calls = []
    local_fact = dict(
        _usage_fact("opencode-go-local"),
        provider="opencode-go",
        harness="opencode",
        occurred_at=datetime.now(timezone.utc).isoformat(),
        x_opencode_quota_cost_usd="1.25",
    )
    monkeypatch.setattr(collector_module, "collect_opencode_usage", lambda *args: [local_fact])
    monkeypatch.setattr(
        collector_module,
        "collect_exact_opencode_go_quota",
        lambda *args, **kwargs: exact_calls.append(args),
        raising=False,
    )
    monkeypatch.setattr(
        collector_module,
        "collect_opencode_go_usage_quota",
        lambda *args, **kwargs: exact_calls.append(args),
    )

    result, quota = _collect_quota_only(tmp_path, environment={})

    rows = [
        row for row in read_sqlite_facts(quota, fact_type="quota_observation_v1")
        if row["provider"] == "opencode-go"
    ]
    assert exact_calls == []
    assert result["sources"]["opencode_go_quota"] == {"discovered": 0}
    assert "opencode_go_quota_estimated" not in result["sources"]
    assert result["warnings"] == []
    assert rows == []


@pytest.mark.parametrize(
    ("partial_environment", "partial_secret"),
    [
        ({"OPENCODE_GO_WORKSPACE_ID": "partial-private-workspace"}, "partial-private-workspace"),
        ({"OPENCODE_GO_AUTH_COOKIE": "partial-private-cookie"}, "partial-private-cookie"),
    ],
)
def test_collect_all_reports_unavailable_without_calling_network_for_partial_opencode_credentials(
    tmp_path, monkeypatch, partial_environment, partial_secret
):
    exact_calls = []
    monkeypatch.setattr(
        collector_module,
        "collect_exact_opencode_go_quota",
        lambda *args, **kwargs: exact_calls.append(args),
        raising=False,
    )
    monkeypatch.setattr(
        collector_module,
        "collect_opencode_go_usage_quota",
        lambda *args, **kwargs: exact_calls.append(args),
    )

    result, quota = _collect_quota_only(tmp_path, environment=partial_environment)

    rows = [
        row for row in read_sqlite_facts(quota, fact_type="quota_observation_v1")
        if row["provider"] == "opencode-go"
    ]
    assert exact_calls == []
    assert result["sources"]["opencode_go_quota"] == {"discovered": 0}
    assert result["warnings"] == []
    assert rows == []
    assert partial_secret not in json.dumps({"result": result, "facts": rows})


@pytest.mark.parametrize(
    ("failure_mode", "expected_error"),
    [("exception", "RuntimeError"), ("empty", "EmptyLaneResult")],
)
def test_collect_all_cedes_the_cookie_lane_without_an_estimated_fallback(
    tmp_path, monkeypatch, failure_mode, expected_error
):
    workspace_id = "private-opencode-workspace"
    auth_cookie = "private-opencode-cookie"
    local_fact = dict(
        _usage_fact("opencode-go-exact-fallback"),
        provider="opencode-go",
        harness="opencode",
        occurred_at=datetime.now(timezone.utc).isoformat(),
        x_opencode_quota_cost_usd="1.25",
    )

    def fail_exact(*args, **kwargs):
        if failure_mode == "exception":
            raise RuntimeError(f"failed for {workspace_id} using {auth_cookie}")
        return []

    monkeypatch.setattr(collector_module, "collect_opencode_usage", lambda *args: [local_fact])
    monkeypatch.setattr(
        collector_module,
        "collect_exact_opencode_go_quota",
        fail_exact,
        raising=False,
    )

    result, quota = _collect_quota_only(
        tmp_path,
        environment={
            "OPENCODE_GO_WORKSPACE_ID": workspace_id,
            "OPENCODE_GO_AUTH_COOKIE": auth_cookie,
        },
    )

    rows = [
        row for row in read_sqlite_facts(quota, fact_type="quota_observation_v1")
        if row["provider"] == "opencode-go"
    ]
    assert result["sources"]["opencode_go_quota"] == {"discovered": 0}
    assert "opencode_go_quota_estimated" not in result["sources"]
    assert result["ceded"] == ["opencode_go_quota"]
    assert result["warnings"] == [
        {"source": "opencode_go_quota", "error": expected_error}
    ]
    assert rows == []
    assert workspace_id not in json.dumps({"result": result, "facts": rows})
    assert auth_cookie not in json.dumps({"result": result, "facts": rows})


def test_collect_all_strict_sources_cedes_the_exact_opencode_lane_without_fallback(
    tmp_path, monkeypatch
):
    """Keeper-ruled 2026-08-29: a failing lane cedes; strict runs still complete."""
    local_fact = dict(
        _usage_fact("opencode-go-strict-fallback"),
        provider="opencode-go",
        harness="opencode",
        occurred_at=datetime.now(timezone.utc).isoformat(),
        x_opencode_quota_cost_usd="1.25",
    )
    monkeypatch.setattr(collector_module, "collect_opencode_usage", lambda *args: [local_fact])
    monkeypatch.setattr(
        collector_module,
        "collect_exact_opencode_go_quota",
        lambda *args: (_ for _ in ()).throw(RuntimeError("private exact failure")),
        raising=False,
    )

    result, quota = _collect_quota_only(
        tmp_path,
        environment={
            "OPENCODE_GO_WORKSPACE_ID": "private-opencode-workspace",
            "OPENCODE_GO_AUTH_COOKIE": "private-opencode-cookie",
        },
        strict_sources=True,
    )

    assert result["ceded"] == ["opencode_go_quota"]
    assert result["warnings"] == [
        {"source": "opencode_go_quota", "error": "RuntimeError"}
    ]
    assert result["lanes"]["opencode_go_quota"]["ceded"] is True
    rows = [
        row for row in read_sqlite_facts(quota, fact_type="quota_observation_v1")
        if row["provider"] == "opencode-go"
    ]
    # No estimated stand-in: the ledger keeps whatever it last held.
    assert rows == []
    assert "private exact failure" not in json.dumps(result)


@pytest.mark.parametrize("key_name", ["Z_AI_API_KEY", "ZAI_API_KEY"])
def test_collect_all_collects_exact_z_ai_quota_for_either_key_alias(
    tmp_path, monkeypatch, key_name
):
    api_key = f"private-{key_name.lower()}"
    captured = {}

    def collect_z_ai(key, namespace):
        captured.update(key=key, namespace=namespace)
        return _collector_quota_rows(
            "z-ai", "z_ai_api", namespace, ("five_hour", "week", "web_search_month")
        )

    monkeypatch.setattr(collector_module, "collect_z_ai_quota", collect_z_ai, raising=False)
    result, quota = _collect_quota_only(tmp_path, environment={key_name: api_key})

    facts = list(read_sqlite_facts(quota, fact_type="quota_observation_v1"))
    z_ai_rows = [row for row in facts if row["provider"] == "z-ai"]
    assert captured == {"key": api_key, "namespace": "sol:z-ai-quota"}
    assert result["sources"]["z_ai_quota"] == {"discovered": 3}
    assert len(z_ai_rows) == 3
    assert all(
        row["harness"] == "z_ai_api" and row["measurement_confidence"] == "exact"
        for row in z_ai_rows
    )
    assert api_key not in json.dumps({"result": result, "facts": facts})


def test_live_quota_failures_are_isolated_and_reported(tmp_path, monkeypatch):
    state=tmp_path/"missing.db"; claude=tmp_path/"claude"; claude.mkdir()
    monkeypatch.setattr(
        "codex_usage_tracker.collector.collect_hermes_usage",
        lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.DatabaseError("bad source")),
    )
    monkeypatch.setattr(
        "codex_usage_tracker.collector.collect_openrouter_quota",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
    )
    monkeypatch.setattr(
        "codex_usage_tracker.collector.collect_deepseek_quota",
        lambda *args, **kwargs: [],
    )
    result = collect_all(
        state_db=state,
        claude_root=claude,
        opencode_dbs=[],
        codex_ledger=tmp_path/"codex.jsonl",
        usage_ledger=tmp_path/"usage.jsonl",
        quota_ledger=tmp_path/"quota.jsonl",
        live_quota=True,
        dry_run=True,
        environment={"OPENROUTER_API_KEY":"secret", "DEEPSEEK_API_KEY":"secret"},
    )
    assert result["sources"]["hermes"]["discovered"] == 0
    assert result["sources"]["deepseek_quota"]["discovered"] == 0
    assert result["warnings"] == [
        {"source":"hermes", "error":"DatabaseError"},
        {"source":"openrouter_quota", "error":"RuntimeError"},
    ]
    assert "secret" not in json.dumps(result)


def test_collect_all_cli_uses_real_sol_defaults(monkeypatch, capsys):
    captured = {}
    monkeypatch.delenv("UNIFIED_USAGE_LEDGER_PATH", raising=False)
    monkeypatch.delenv("QUOTA_LEDGER_PATH", raising=False)
    monkeypatch.delenv("BILLING_LEDGER_PATH", raising=False)

    def fake_collect_all(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr("codex_usage_tracker.collector.collect_all", fake_collect_all)
    monkeypatch.setattr("sys.argv", ["tracker", "collect-all", "--dry-run", "--no-live-quota"])
    assert main() == 0
    assert captured["state_db"] == "/var/lib/hermes/primary/state.db"
    assert captured["dotenv"] == "/var/lib/hermes/primary/.env"
    assert captured["claude_quota_command"] == "claude"
    assert captured["claude_probe_dir"] == "~/.local/state/codex-usage-tracker/claude-probe"
    assert captured["claude_quota_timeout"] == 45.0
    assert captured["scope"] == "all"
    assert captured["codex_quota_history"] is False
    assert captured["codex_quota_history_since"] is None
    assert captured["strict_sources"] is False
    assert captured["usage_ledger"] == "~/.local/state/codex-usage-tracker/usage_events.sqlite3"
    assert captured["quota_ledger"] == "~/.local/state/codex-usage-tracker/quota_observations.sqlite3"
    assert captured["billing_ledger"] == "~/.local/state/codex-usage-tracker/billing_facts.sqlite3"
    assert captured["opencode_dbs"] == [
        "~/.local/share/opencode/opencode-stable.db",
        "~/.local/share/opencode/opencode-local.db",
    ]
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_dashboard_cli_threads_private_ledgers_without_repurposing_legacy_ledger(monkeypatch):
    captured = {}

    def fake_run_dashboard(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("codex_usage_tracker.dashboard.run_dashboard", fake_run_dashboard)
    monkeypatch.setattr("sys.argv", [
        "tracker", "dashboard", "--ledger", "/private/codex.jsonl",
        "--unified-usage-ledger", "/private/usage.sqlite3",
        "--quota-ledger", "/private/quota.sqlite3",
        "--billing-ledger", "/private/billing.sqlite3",
        "--search-ledger", "/private/search.sqlite3",
    ])
    assert main() == 0
    assert captured == {
        "atrium_root": "/Users/luisramirez/Digital_Workspace",
        "ledger": "/private/codex.jsonl",
        "unified_usage_ledger": "/private/usage.sqlite3",
        "quota_ledger": "/private/quota.sqlite3",
        "billing_ledger": "/private/billing.sqlite3",
        "search_ledger": "/private/search.sqlite3",
        "host": "127.0.0.1",
        "port": 5174,
    }


def test_dashboard_page_is_provider_neutral_and_loads_private_panels_independently(tmp_path):
    """React + Vite retarget: the served page is the built entry document
    (title, vendored Chart.js as a classic script, no Codex-specific
    surfaces); the panel wiring — bounded fetches, independent loaders,
    last-good fallbacks, provider-neutral unified surfaces — lives in the
    frontend sources."""
    client = create_app(ledger=str(tmp_path / "codex.jsonl")).test_client()
    page = client.get("/").get_data(as_text=True)
    assert "<title>AI Usage</title>" in page
    assert '<script src="/static/vendor/chart.umd.min.js"></script>' in page
    chart_asset = client.get("/static/vendor/chart.umd.min.js")
    assert chart_asset.status_code == 200
    assert b"Chart.js v4.5.1" in chart_asset.data[:100]
    package_data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]["package-data"]
    assert "static/vendor/*.js" in package_data["codex_usage_tracker"]
    # The served page must name the committed build it ships.
    assert re.search(r'src="/static/app/assets/index-[^"]+\.js"', page)

    joined = frontend_surfaces.frontend_text()
    hook = frontend_surfaces.source("hooks/useDashboardData.ts")
    card = frontend_surfaces.source("components/UnifiedUsageCard.tsx")
    chart_lib = frontend_surfaces.source("lib/chart.ts")

    # The Codex detail compatibility section was removed entirely
    # (keeper-ruled 2026-08-30); the unified surfaces carry the page alone.
    assert "Codex detail" not in page
    assert "Codex detail" not in joined
    assert 'fetch("/api/unified-usage?hours=168", { signal: AbortSignal.timeout(30000) })' in hook
    assert 'canvasId="modelTokenChart"' in card
    # Estate-wide replacements for the codex-only token charts.
    assert 'canvasId="tokenCompositionChart"' in card
    assert 'canvasId="cacheHitRateChart"' in card
    # Retired surfaces (keeper-ratified flow rework, akasan 2026-08-30): the
    # rank-bar comparison cards and the two-party Codex-vs-Claude chart are
    # replaced by the quota donuts, status strip, and reset rail.
    assert "codexClaude" not in joined
    assert "ComparisonBars" not in joined
    assert "renderAggregateComparisons" not in joined
    assert 'svgId="fiveHourDonut"' in card
    assert 'svgId="weeklyDonut"' in card
    assert 'id="resetRailChips"' in frontend_surfaces.source("components/ResetRail.tsx")
    assert 'id="providerStatusRows"' in frontend_surfaces.source("components/ProviderStatus.tsx")
    # Chart.js absence is guarded, not assumed (the vendored UMD global).
    assert 'typeof Chart === "undefined"' in chart_lib
    assert 'labelsId="modelTokenLabels"' in card
    assert "Tokens across provider models" in card
    assert "stacked: true" in card
    assert "legend: { display: false" in chart_lib
    assert "prefers-reduced-motion" in joined
    assert 'timeZone: "UTC"' in frontend_surfaces.source("lib/format.ts")
    assert 'scope="row"' in frontend_surfaces.source("components/SeriesTable.tsx")
    assert "overflow-x: hidden" in frontend_surfaces.app_css()
    assert "overflow-wrap: anywhere" in frontend_surfaces.app_css()
    assert 'align: "inner"' in chart_lib
    assert 'fetch("/api/subscriptions?hours=168&history=0", { signal: AbortSignal.timeout(30000) })' in hook
    assert 'fetch("/api/subscriptions?days=30&history=0")' not in joined
    assert 'fetch("/api/billing?days=30", { signal: AbortSignal.timeout(30000) })' in hook
    assert "Refresh failed — showing last good data." in hook
    assert "if (!document.hidden && booted.current) hydrate();" in hook
    # The five-hour and 24h follow-up windows are optional refinements in
    # their own try blocks, never an all-or-nothing Promise.all — and the
    # four panel loaders settle independently.
    optional_script = hook[hook.index('fetch("/api/unified-usage?hours=168", { signal'):]
    assert "Promise.all([" not in optional_script
    assert "Promise.allSettled([" in hook
    assert 'fetch("/api/unified-usage?hours=5"' in optional_script
    assert 'fetch("/api/unified-usage?hours=24"' in optional_script
    assert "Unified usage unavailable" in hook
    assert "No unified usage events" in hook
    assert "Subscription data unavailable" in hook
    assert "No subscription observations" in hook
    assert "Billing data unavailable" in hook
    assert "No billing transactions" in hook
    # Renamed 2026-09-14 (keeper authorization): the headline is the API
    # list-price equivalent, on the Accounting view's own card.
    assert "Est. spend" not in card
    assert "API list-price equivalent" in frontend_surfaces.source("components/ListPriceHeadline.tsx")
    assert "Billing ledger" in frontend_surfaces.source("components/BillingCard.tsx")
    # Injection safety: React text nodes only, no raw-HTML path.
    assert "dangerouslySetInnerHTML" not in joined


def test_readme_documents_private_ai_dashboard_and_new_projection_compatibility_boundary():
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "write-public-usage-projection" in readme
    assert "--unified-usage-ledger" in readme
    assert "AI Usage" in readme
    assert "hours=1..168" in readme
    # The retired comparison chart pinned these provider/harness keys; the
    # canonical-provider rework (2026-09-02) carries them in the quota paths
    # instead, so they are no longer README phrases.
    assert "harness=claude_code" in readme
    assert "Do not publish" in readme
    assert "write-public-projection" in readme
    assert "compatibility" in readme.casefold()


def test_collect_all_cli_reports_sanitized_failure(monkeypatch, capsys):
    def fail(**kwargs):
        raise RuntimeError("credential and private path details")

    monkeypatch.setattr("codex_usage_tracker.collector.collect_all", fail)
    monkeypatch.setattr("sys.argv", ["tracker", "collect-all", "--dry-run", "--no-live-quota"])
    assert main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "collect-all failed: RuntimeError"


def test_collect_all_cli_names_failing_lanes_on_strict_failure(monkeypatch, capsys):
    from codex_usage_tracker.collector import StrictSourcesError

    def fail(**kwargs):
        raise StrictSourcesError(
            [{"source": "hermes", "error": "OperationalError"}]
        )

    monkeypatch.setattr("codex_usage_tracker.collector.collect_all", fail)
    monkeypatch.setattr("sys.argv", ["tracker", "collect-all", "--dry-run", "--no-live-quota"])
    assert main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.strip().splitlines()
    assert lines[0] == "collect-all failed: StrictSourcesError"
    assert json.loads(lines[1]) == [{"source": "hermes", "error": "OperationalError"}]


def test_collect_all_cli_exits_zero_and_names_a_ceded_lane(tmp_path, monkeypatch, capsys):
    """Keeper-ruled 2026-08-29: a ceded lane degrades the summary, not the exit."""

    def broken(*args, **kwargs):
        raise OSError("private source path")

    monkeypatch.setattr("codex_usage_tracker.collector.collect_hermes_usage", broken)
    monkeypatch.setattr("codex_usage_tracker.collector.collect_claude_usage", lambda *args: [])
    monkeypatch.setattr("codex_usage_tracker.collector.collect_opencode_usage", lambda *args: [])
    monkeypatch.setattr(
        "sys.argv",
        [
            "tracker",
            "collect-all",
            "--scope",
            "usage",
            "--strict-sources",
            "--no-live-quota",
            "--state-db",
            str(tmp_path / "state.db"),
            "--claude-root",
            str(tmp_path / "claude"),
            "--codex-ledger",
            str(tmp_path / "codex.jsonl"),
            "--usage-ledger",
            str(tmp_path / "usage.sqlite3"),
            "--quota-ledger",
            str(tmp_path / "quota.sqlite3"),
        ],
    )

    assert main() == 0
    captured = capsys.readouterr()
    summary = captured.err.strip().splitlines()[0]
    assert summary.startswith("collect-all: scope=usage")
    assert "lanes=2/3" in summary
    assert "degraded=hermes" in summary
    assert "ceded=hermes" in summary
    payload = json.loads(captured.out)
    assert payload["ceded"] == ["hermes"]
    assert "private source path" not in captured.out + captured.err


def _stub_collectors(monkeypatch, usage_rows):
    monkeypatch.setattr("codex_usage_tracker.collector.collect_hermes_usage", lambda *args: list(usage_rows))
    monkeypatch.setattr("codex_usage_tracker.collector.collect_claude_usage", lambda *args: [])
    monkeypatch.setattr("codex_usage_tracker.collector.collect_opencode_usage", lambda *args: [])
    monkeypatch.setattr("codex_usage_tracker.collector.codex_quota_observations", lambda *args: [])


def test_collect_all_sqlite_append_replay_and_three_fact_bindings(tmp_path, monkeypatch):
    _stub_collectors(monkeypatch, [_usage_fact()])
    paths = {
        "usage_ledger": tmp_path / "usage.sqlite3",
        "quota_ledger": tmp_path / "quota.db",
        "billing_ledger": tmp_path / "billing.sqlite",
    }
    kwargs = dict(
        state_db=tmp_path / "state.db",
        claude_root=tmp_path / "claude",
        opencode_dbs=[],
        codex_ledger=tmp_path / "codex.jsonl",
        live_quota=False,
        **paths,
    )
    first = collect_all(**kwargs)
    second = collect_all(**kwargs)

    assert first["usage"] == {"discovered": 1, "appended": 1, "replayed": 0}
    assert second["usage"] == {"discovered": 1, "appended": 0, "replayed": 1}
    # No live quota and no OpenCode Go credentials: nothing to append (the
    # retired Go estimate used to contribute three rows here).
    assert first["quotas"]["appended"] == 0
    assert first["billing"] == {"discovered": 0, "appended": 0, "replayed": 0}
    assert read_sqlite_facts(paths["usage_ledger"], fact_type="usage_event_v1")[0]["source_event_id"] == "u-1"
    for name, expected in (
        ("usage_ledger", "usage_event_v1"),
        ("quota_ledger", "quota_observation_v1"),
        ("billing_ledger", "billing_fact_v1"),
    ):
        with sqlite3.connect(paths[name]) as connection:
            assert connection.execute(
                "SELECT value FROM ledger_metadata WHERE key='fact_type'"
            ).fetchone()[0] == expected
        assert first["paths"][name] == str(paths[name])


def test_collect_all_preserves_jsonl_backend_compatibility(tmp_path, monkeypatch):
    _stub_collectors(monkeypatch, [_usage_fact()])
    usage = tmp_path / "usage.jsonl"
    quota = tmp_path / "quota.jsonl"
    billing = tmp_path / "billing.jsonl"
    result = collect_all(
        state_db=tmp_path / "state.db",
        claude_root=tmp_path / "claude",
        opencode_dbs=[],
        codex_ledger=tmp_path / "codex.jsonl",
        usage_ledger=usage,
        quota_ledger=quota,
        billing_ledger=billing,
        live_quota=False,
    )
    assert result["usage"]["appended"] == 1
    assert read_facts(usage)[0]["source_event_id"] == "u-1"
    assert result["quotas"]["appended"] == 0
    assert read_facts(quota) == []
    assert read_facts(billing) == []


def test_collect_all_quarantines_one_changed_identity_without_blocking_safe_usage(tmp_path, monkeypatch):
    usage = tmp_path / "usage.sqlite3"
    original = dict(_usage_fact("private-source-id"), input_tokens=1)
    changed = dict(original, input_tokens=99)
    safe = dict(_usage_fact("safe"), input_tokens=2)
    append_sqlite_facts(usage, [original], fact_type="usage_event_v1")
    monkeypatch.setattr("codex_usage_tracker.collector.collect_hermes_usage", lambda *args: [changed, safe])
    monkeypatch.setattr("codex_usage_tracker.collector.collect_claude_usage", lambda *args: [])
    monkeypatch.setattr("codex_usage_tracker.collector.collect_opencode_usage", lambda *args: [])

    result = collect_all(
        state_db=tmp_path / "state.db",
        claude_root=tmp_path / "claude",
        opencode_dbs=[],
        codex_ledger=tmp_path / "codex.jsonl",
        usage_ledger=usage,
        quota_ledger=tmp_path / "quota.sqlite3",
        live_quota=False,
        scope="usage",
    )

    rows = {row["source_event_id"]: row for row in read_sqlite_facts(usage)}
    assert rows["private-source-id"]["input_tokens"] == 1
    assert rows["safe"]["input_tokens"] == 2
    assert result["usage"] == {"appended": 1, "discovered": 2, "quarantined": 1, "replayed": 0}
    assert len(result["conflicts"]) == 1
    conflict = result["conflicts"][0]
    assert conflict["source"] == "hermes"
    assert conflict["error"] == "IdentityConflictError"
    assert conflict["changed_fields"] == ["input_tokens"]
    assert len(conflict["identity_sha256"]) == 64
    assert "private-source-id" not in json.dumps(result)


def test_collect_all_reuses_existing_claude_timestamp_as_canonical_replay(tmp_path, monkeypatch):
    usage = tmp_path / "usage.sqlite3"
    original = dict(
        _usage_fact("private-source-id"),
        occurred_at="2026-07-21T19:10:39.593Z",
        recorded_at="2026-07-21T19:10:39.593Z",
    )
    later_receipt = dict(
        original,
        occurred_at="2026-07-21T19:11:26.082Z",
        recorded_at="2026-07-21T19:11:26.082Z",
    )
    append_sqlite_facts(usage, [original], fact_type="usage_event_v1")
    monkeypatch.setattr("codex_usage_tracker.collector.collect_hermes_usage", lambda *args: [])
    monkeypatch.setattr("codex_usage_tracker.collector.collect_claude_usage", lambda *args: [later_receipt])
    monkeypatch.setattr("codex_usage_tracker.collector.collect_opencode_usage", lambda *args: [])

    result = collect_all(
        state_db=tmp_path / "state.db",
        claude_root=tmp_path / "claude",
        opencode_dbs=[],
        codex_ledger=tmp_path / "codex.jsonl",
        usage_ledger=usage,
        quota_ledger=tmp_path / "quota.sqlite3",
        live_quota=False,
        scope="usage",
    )

    assert result["usage"] == {"appended": 0, "discovered": 1, "replayed": 1}
    assert result["warnings"] == []
    assert result["conflicts"] == []
    assert len(result["stabilized_replays"]) == 1
    stabilized = result["stabilized_replays"][0]
    assert stabilized["source"] == "claude_code"
    assert stabilized["resolution"] == "canonical_replay"
    assert stabilized["changed_fields"] == ["occurred_at", "recorded_at"]
    assert "private-source-id" not in json.dumps(result)


def test_collect_all_corrects_finalized_claude_stream_without_rewriting_original(tmp_path, monkeypatch):
    usage = tmp_path / "usage.sqlite3"
    original = dict(
        _usage_fact("private-source-id"),
        harness="claude_code",
        provider="anthropic",
        model_requested="claude-opus",
        model_reported="claude-opus",
        occurred_at="2026-08-01T19:10:47.465Z",
        recorded_at="2026-08-01T19:10:47.465Z",
        request_status="unknown",
        input_tokens=10,
        cache_read_tokens=20,
        cache_write_tokens=0,
        output_tokens=5,
        reasoning_tokens=0,
    )
    finalized = dict(
        original,
        occurred_at="2026-08-01T19:10:50.384Z",
        recorded_at="2026-08-01T19:10:50.384Z",
        request_status="ok",
        output_tokens=1028,
    )
    append_sqlite_facts(usage, [original], fact_type="usage_event_v1")
    monkeypatch.setattr("codex_usage_tracker.collector.collect_hermes_usage", lambda *args: [])
    monkeypatch.setattr("codex_usage_tracker.collector.collect_claude_usage", lambda *args: [finalized])
    monkeypatch.setattr("codex_usage_tracker.collector.collect_opencode_usage", lambda *args: [])
    kwargs = dict(
        state_db=tmp_path / "state.db",
        claude_root=tmp_path / "claude",
        opencode_dbs=[],
        codex_ledger=tmp_path / "codex.jsonl",
        usage_ledger=usage,
        quota_ledger=tmp_path / "quota.sqlite3",
        live_quota=False,
        scope="usage",
        strict_sources=True,
    )

    first = collect_all(**kwargs)
    rows = read_sqlite_facts(usage, fact_type="usage_event_v1")
    original_after = next(row for row in rows if row["source_event_id"] == "private-source-id")
    correction = next(row for row in rows if row["record_kind"] == "correction")

    assert original_after["output_tokens"] == 5
    assert original_after["request_status"] == "unknown"
    assert original_after["occurred_at"] == "2026-08-01T19:10:47.465Z"
    assert correction["output_tokens"] == 1023
    assert correction["input_tokens"] == 0
    assert correction["cache_read_tokens"] == 0
    assert correction["corrects_source_namespace"] == original["source_namespace"]
    assert correction["corrects_source_event_id"] == "private-source-id"
    assert correction["x_final_request_status"] == "ok"
    assert sum(int(row.get("output_tokens") or 0) for row in rows) == 1028
    assert first["warnings"] == [] and first["conflicts"] == []
    assert first["usage"] == {"appended": 1, "discovered": 2, "replayed": 1}
    assert first["generated_corrections"] == 1
    assert first["stabilized_replays"][0]["resolution"] == "canonical_correction"
    assert "private-source-id" not in json.dumps(first)

    second = collect_all(**kwargs)
    assert second["usage"] == {"appended": 0, "discovered": 1, "replayed": 1}
    assert second["generated_corrections"] == 0
    assert len(read_sqlite_facts(usage, fact_type="usage_event_v1")) == 2


def test_collect_all_records_zero_token_claude_finalization_once(tmp_path, monkeypatch):
    usage = tmp_path / "usage.sqlite3"
    original = dict(
        _usage_fact("private-source-id"),
        harness="claude_code",
        provider="anthropic",
        request_status="unknown",
        occurred_at="2026-08-01T19:10:47.465Z",
        recorded_at="2026-08-01T19:10:47.465Z",
        output_tokens=5,
    )
    finalized = dict(
        original,
        request_status="ok",
        occurred_at="2026-08-01T19:10:50.384Z",
        recorded_at="2026-08-01T19:10:50.384Z",
    )
    append_sqlite_facts(usage, [original], fact_type="usage_event_v1")
    monkeypatch.setattr("codex_usage_tracker.collector.collect_hermes_usage", lambda *args: [])
    monkeypatch.setattr(
        "codex_usage_tracker.collector.collect_claude_usage", lambda *args: [finalized]
    )
    monkeypatch.setattr("codex_usage_tracker.collector.collect_opencode_usage", lambda *args: [])
    kwargs: dict[str, Any] = dict(
        state_db=tmp_path / "state.db",
        claude_root=tmp_path / "claude",
        opencode_dbs=[],
        codex_ledger=tmp_path / "codex.jsonl",
        usage_ledger=usage,
        quota_ledger=tmp_path / "quota.sqlite3",
        live_quota=False,
        scope="usage",
        strict_sources=True,
    )

    first = collect_all(**kwargs)
    second = collect_all(**kwargs)
    rows = read_sqlite_facts(usage, fact_type="usage_event_v1")
    corrections = [row for row in rows if row["record_kind"] == "correction"]

    assert len(corrections) == 1
    assert corrections[0]["x_final_request_status"] == "ok"
    assert all(int(corrections[0].get(field) or 0) == 0 for field in (
        "input_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "output_tokens",
        "reasoning_tokens",
    ))
    assert sum(int(row.get("output_tokens") or 0) for row in rows) == 5
    assert first["generated_corrections"] == 1
    assert second["generated_corrections"] == 0
    assert len(rows) == 2


def test_collect_all_does_not_correct_nonmonotonic_claude_mutation(tmp_path, monkeypatch):
    usage = tmp_path / "usage.sqlite3"
    original = dict(
        _usage_fact("private-source-id"),
        harness="claude_code",
        provider="anthropic",
        request_status="unknown",
        output_tokens=50,
    )
    changed = dict(original, request_status="ok", output_tokens=5)
    append_sqlite_facts(usage, [original], fact_type="usage_event_v1")
    monkeypatch.setattr("codex_usage_tracker.collector.collect_hermes_usage", lambda *args: [])
    monkeypatch.setattr("codex_usage_tracker.collector.collect_claude_usage", lambda *args: [changed])
    monkeypatch.setattr("codex_usage_tracker.collector.collect_opencode_usage", lambda *args: [])

    with pytest.raises(RuntimeError, match="strict source policy"):
        collect_all(
            state_db=tmp_path / "state.db",
            claude_root=tmp_path / "claude",
            opencode_dbs=[],
            codex_ledger=tmp_path / "codex.jsonl",
            usage_ledger=usage,
            quota_ledger=tmp_path / "quota.sqlite3",
            live_quota=False,
            scope="usage",
            strict_sources=True,
        )

    rows = read_sqlite_facts(usage, fact_type="usage_event_v1")
    assert len(rows) == 1 and rows[0]["output_tokens"] == 50


def test_collect_all_quarantines_multiple_finalized_claude_variants(tmp_path, monkeypatch):
    usage = tmp_path / "usage.sqlite3"
    original = dict(
        _usage_fact("private-source-id"),
        harness="claude_code",
        provider="anthropic",
        request_status="unknown",
        output_tokens=5,
    )
    append_sqlite_facts(usage, [original], fact_type="usage_event_v1")
    variants = [
        dict(original, request_status="ok", output_tokens=100),
        dict(original, request_status="ok", output_tokens=200),
    ]
    monkeypatch.setattr("codex_usage_tracker.collector.collect_hermes_usage", lambda *args: [])
    monkeypatch.setattr("codex_usage_tracker.collector.collect_claude_usage", lambda *args: variants)
    monkeypatch.setattr("codex_usage_tracker.collector.collect_opencode_usage", lambda *args: [])

    with pytest.raises(RuntimeError, match="strict source policy"):
        collect_all(
            state_db=tmp_path / "state.db",
            claude_root=tmp_path / "claude",
            opencode_dbs=[],
            codex_ledger=tmp_path / "codex.jsonl",
            usage_ledger=usage,
            quota_ledger=tmp_path / "quota.sqlite3",
            live_quota=False,
            scope="usage",
            strict_sources=True,
        )

    rows = read_sqlite_facts(usage, fact_type="usage_event_v1")
    assert len(rows) == 1
    assert rows[0]["output_tokens"] == 5


def test_collect_all_sees_foreign_manual_correction_before_finalizing_claude(
    tmp_path, monkeypatch
):
    usage = tmp_path / "usage.sqlite3"
    original = dict(
        _usage_fact("private-source-id"),
        harness="claude_code",
        provider="anthropic",
        request_status="unknown",
        output_tokens=5,
    )
    manual = dict(
        original,
        source_event_id="operator-correction",
        harness="operator",
        record_kind="correction",
        request_status=None,
        output_tokens=10,
        corrects_source_namespace=original["source_namespace"],
        corrects_source_event_id=original["source_event_id"],
    )
    append_sqlite_facts(usage, [original, manual], fact_type="usage_event_v1")
    finalized = dict(original, request_status="ok", output_tokens=100)
    monkeypatch.setattr("codex_usage_tracker.collector.collect_hermes_usage", lambda *args: [])
    monkeypatch.setattr(
        "codex_usage_tracker.collector.collect_claude_usage", lambda *args: [finalized]
    )
    monkeypatch.setattr("codex_usage_tracker.collector.collect_opencode_usage", lambda *args: [])

    with pytest.raises(RuntimeError, match="strict source policy"):
        collect_all(
            state_db=tmp_path / "state.db",
            claude_root=tmp_path / "claude",
            opencode_dbs=[],
            codex_ledger=tmp_path / "codex.jsonl",
            usage_ledger=usage,
            quota_ledger=tmp_path / "quota.sqlite3",
            live_quota=False,
            scope="usage",
            strict_sources=True,
        )

    rows = read_sqlite_facts(usage, fact_type="usage_event_v1")
    assert len(rows) == 2
    assert sum(row["record_kind"] == "correction" for row in rows) == 1
    assert not any(row.get("x_claude_stream_finalization") for row in rows)


def test_concurrent_claude_finalizations_cannot_double_count(tmp_path, monkeypatch):
    usage = tmp_path / "usage.sqlite3"
    original = dict(
        _usage_fact("private-source-id"),
        harness="claude_code",
        provider="anthropic",
        request_status="unknown",
        output_tokens=5,
    )
    append_sqlite_facts(usage, [original], fact_type="usage_event_v1")
    barrier = threading.Barrier(2)
    assignment_lock = threading.Lock()
    assignments = iter((100, 200))

    def concurrent_receipt(*args):
        with assignment_lock:
            output_tokens = next(assignments)
        barrier.wait(timeout=10)
        return [dict(original, request_status="ok", output_tokens=output_tokens)]

    monkeypatch.setattr("codex_usage_tracker.collector.collect_hermes_usage", lambda *args: [])
    monkeypatch.setattr("codex_usage_tracker.collector.collect_claude_usage", concurrent_receipt)
    monkeypatch.setattr("codex_usage_tracker.collector.collect_opencode_usage", lambda *args: [])
    kwargs: dict[str, Any] = dict(
        state_db=tmp_path / "state.db",
        claude_root=tmp_path / "claude",
        opencode_dbs=[],
        codex_ledger=tmp_path / "codex.jsonl",
        usage_ledger=usage,
        quota_ledger=tmp_path / "quota.sqlite3",
        live_quota=False,
        scope="usage",
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: collect_all(**kwargs), range(2)))

    rows = read_sqlite_facts(usage, fact_type="usage_event_v1")
    corrections = [row for row in rows if row["record_kind"] == "correction"]
    conflicts = sum(len(result["conflicts"]) for result in results)
    # Both interleavings are legal and neither may double count. If the smaller
    # receipt commits first the larger one earns a second monotonic delta; if
    # the larger commits first the smaller is a shrink and is quarantined. The
    # ledger therefore always settles on the largest observation, and each run
    # accounts for itself exactly once, either as a correction or a conflict.
    # (Asserting exactly one correction pinned one interleaving and made this
    # test fail roughly half the time.)
    assert sum(int(row.get("output_tokens") or 0) for row in rows) == 200
    assert len(corrections) + conflicts == 2
    assert sum(result["generated_corrections"] for result in results) == len(corrections)
    assert len({row["source_event_id"] for row in corrections}) == len(corrections)
    assert all(row["corrects_source_event_id"] == "private-source-id" for row in corrections)


def test_collect_all_strict_sources_retries_then_cedes_a_partial_failure(tmp_path, monkeypatch):
    """Keeper-ruled 2026-08-29: three attempts, then cede — the run completes."""
    calls: list[int] = []

    def broken(*args, **kwargs):
        calls.append(1)
        raise OSError("private source path")

    monkeypatch.setattr("codex_usage_tracker.collector.collect_hermes_usage", broken)
    monkeypatch.setattr("codex_usage_tracker.collector.collect_claude_usage", lambda *args: [])
    monkeypatch.setattr("codex_usage_tracker.collector.collect_opencode_usage", lambda *args: [])

    result = collect_all(
        state_db=tmp_path / "state.db",
        claude_root=tmp_path / "claude",
        opencode_dbs=[],
        codex_ledger=tmp_path / "codex.jsonl",
        usage_ledger=tmp_path / "usage.sqlite3",
        quota_ledger=tmp_path / "quota.sqlite3",
        live_quota=False,
        scope="usage",
        strict_sources=True,
    )

    assert len(calls) == collector_module.LANE_ATTEMPTS
    assert result["ceded"] == ["hermes"]
    assert result["counts"]["ceded"] == 1
    assert result["warnings"] == [{"source": "hermes", "error": "OSError"}]
    assert "private source path" not in json.dumps(result)


def test_codex_quota_history_is_source_isolated_and_idempotent(tmp_path, monkeypatch):
    codex = tmp_path / "codex.jsonl"
    codex.write_text(
        "\n".join(
            json.dumps(
                {
                    "id": snapshot_id,
                    "fetched_at": fetched_at,
                    "session_used_pct": used,
                    "weekly_used_pct": used + 10,
                }
            )
            for snapshot_id, fetched_at, used in (
                ("older", "2026-07-11T12:00:00Z", 10),
                ("newer", "2026-07-12T12:00:00Z", 20),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    def unexpected(*args, **kwargs):
        raise AssertionError("non-Codex source must not run during bounded history recovery")

    monkeypatch.setattr("codex_usage_tracker.collector.collect_opencode_usage", unexpected)
    monkeypatch.setattr("codex_usage_tracker.collector.collect_openrouter_quota", unexpected)
    monkeypatch.setattr("codex_usage_tracker.collector.collect_deepseek_quota", unexpected)
    quota = tmp_path / "quota.sqlite3"
    kwargs = {
        "state_db": tmp_path / "state.db",
        "claude_root": tmp_path / "claude",
        "opencode_dbs": [],
        "codex_ledger": codex,
        "usage_ledger": tmp_path / "usage.sqlite3",
        "quota_ledger": quota,
        "live_quota": False,
        "scope": "quota",
        "codex_quota_history": True,
        "codex_quota_history_since": "2026-07-11T18:00:00Z",
    }

    first = collect_all(**kwargs)
    second = collect_all(**kwargs)

    assert set(first["sources"]) == {"codex_quota"}
    assert first["warnings"] == []
    assert first["quotas"] == {"appended": 2, "discovered": 2, "replayed": 0}
    assert second["quotas"] == {"appended": 0, "discovered": 2, "replayed": 2}
    assert len(read_sqlite_facts(quota, fact_type="quota_observation_v1")) == 2


def test_codex_quota_history_rejects_unbounded_or_mixed_scope(tmp_path):
    base = {
        "state_db": tmp_path / "state.db",
        "claude_root": tmp_path / "claude",
        "opencode_dbs": [],
        "codex_ledger": tmp_path / "codex.jsonl",
        "usage_ledger": tmp_path / "usage.sqlite3",
        "quota_ledger": tmp_path / "quota.sqlite3",
        "live_quota": False,
        "codex_quota_history": True,
    }
    with pytest.raises(ValueError, match="history-since"):
        collect_all(scope="quota", **base)
    with pytest.raises(ValueError, match="scope quota"):
        collect_all(scope="all", codex_quota_history_since="2026-07-11T18:00:00Z", **base)


def test_collect_all_scopes_do_not_touch_out_of_scope_ledgers(tmp_path, monkeypatch):
    _stub_collectors(monkeypatch, [_usage_fact()])
    usage = tmp_path / "usage.sqlite3"
    quota = tmp_path / "quota.sqlite3"
    billing = tmp_path / "billing.sqlite3"

    usage_result = collect_all(
        state_db=tmp_path / "state.db",
        claude_root=tmp_path / "claude",
        opencode_dbs=[],
        codex_ledger=tmp_path / "missing-codex.jsonl",
        usage_ledger=usage,
        quota_ledger=quota,
        billing_ledger=billing,
        live_quota=False,
        scope="usage",
    )
    assert usage_result["scope"] == "usage"
    assert "usage" in usage_result and "quotas" not in usage_result and "billing" not in usage_result
    assert usage.exists() and not quota.exists() and not billing.exists()

    quota_result = collect_all(
        state_db=tmp_path / "missing-state.db",
        claude_root=tmp_path / "missing-claude",
        opencode_dbs=[],
        codex_ledger=tmp_path / "missing-codex.jsonl",
        usage_ledger=tmp_path / "out-of-scope-usage.bad",
        quota_ledger=quota,
        billing_ledger=billing,
        live_quota=False,
        scope="quota",
    )
    assert quota_result["scope"] == "quota"
    assert "usage" not in quota_result and "quotas" in quota_result and "billing" not in quota_result
    assert quota.exists() and not billing.exists()
    assert not (tmp_path / "out-of-scope-usage.bad").exists()


def test_collect_all_rejects_unknown_ledger_suffix(tmp_path, monkeypatch):
    _stub_collectors(monkeypatch, [])
    with pytest.raises(ValueError, match="suffix"):
        collect_all(
            state_db=tmp_path / "state.db",
            claude_root=tmp_path / "claude",
            opencode_dbs=[],
            codex_ledger=tmp_path / "codex.jsonl",
            usage_ledger=tmp_path / "usage.txt",
            quota_ledger=tmp_path / "quota.jsonl",
            live_quota=False,
            dry_run=True,
        )


def test_operator_cli_migrate_export_audit_round_trip(tmp_path, monkeypatch, capsys):
    source = tmp_path / "usage.jsonl"
    source.write_text(canonical_json(_usage_fact()) + "\n", encoding="utf-8")
    database = tmp_path / "usage.sqlite3"
    exported = tmp_path / "exported.jsonl"

    monkeypatch.setattr("sys.argv", [
        "tracker", "migrate-ledger", "--source-jsonl", str(source),
        "--destination-sqlite", str(database), "--fact-type", "usage_event_v1",
    ])
    assert main() == 0
    migrated = json.loads(capsys.readouterr().out)
    assert migrated["counts"] == {"appended": 1, "discovered": 1, "replayed": 0}
    assert migrated["paths"] == {"destination_sqlite": str(database), "source_jsonl": str(source)}

    monkeypatch.setattr("sys.argv", [
        "tracker", "export-ledger", "--source-sqlite", str(database),
        "--destination-jsonl", str(exported), "--fact-type", "usage_event_v1",
    ])
    assert main() == 0
    exported_result = json.loads(capsys.readouterr().out)
    assert exported_result["counts"] == {"exported": 1}
    assert read_facts(exported) == read_sqlite_facts(database)

    monkeypatch.setattr("sys.argv", [
        "tracker", "audit-ledger", "--sqlite", str(database),
        "--fact-type", "usage_event_v1",
    ])
    assert main() == 0
    audited = json.loads(capsys.readouterr().out)
    assert audited == {
        "counts": {"audited": 1},
        "fact_type": "usage_event_v1",
        "paths": {"sqlite": str(database)},
    }


def test_migrate_ledger_cli_dry_run_has_no_artifacts(tmp_path, monkeypatch, capsys):
    source = tmp_path / "usage.jsonl"
    source.write_text(canonical_json(_usage_fact()) + "\n", encoding="utf-8")
    destination = tmp_path / "absent" / "usage.sqlite3"
    monkeypatch.setattr("sys.argv", [
        "tracker", "migrate-ledger", "--source-jsonl", str(source),
        "--destination-sqlite", str(destination), "--fact-type", "usage_event_v1", "--dry-run",
    ])
    assert main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True and result["counts"]["appended"] == 1
    assert not destination.parent.exists()
    assert not source.with_name(f"{source.name}.lock").exists()


def test_operator_cli_sanitizes_fatal_errors(tmp_path, monkeypatch, capsys):
    private = tmp_path / "secret-source.jsonl"
    monkeypatch.setattr("sys.argv", [
        "tracker", "migrate-ledger", "--source-jsonl", str(private),
        "--destination-sqlite", str(tmp_path / "out.sqlite3"),
        "--fact-type", "usage_event_v1",
    ])
    assert main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "migrate-ledger failed: MalformedLedgerError"
    assert str(private) not in captured.err


def test_collect_all_rejects_reused_or_aliased_destination_paths_before_writes(tmp_path, monkeypatch):
    _stub_collectors(monkeypatch, [_usage_fact()])
    shared = tmp_path / "shared.jsonl"
    with pytest.raises(ValueError, match="distinct"):
        collect_all(
            state_db=tmp_path / "state.db",
            claude_root=tmp_path / "claude",
            opencode_dbs=[],
            codex_ledger=tmp_path / "codex.jsonl",
            usage_ledger=shared,
            quota_ledger=shared,
            billing_ledger=shared,
            live_quota=False,
        )
    assert not shared.exists()


def test_collect_all_jsonl_dry_run_does_not_lock_or_chmod_existing_ledgers(tmp_path, monkeypatch):
    _stub_collectors(monkeypatch, [_usage_fact()])
    usage = tmp_path / "usage.jsonl"
    quota = tmp_path / "quota.jsonl"
    usage.write_text(canonical_json(_usage_fact("existing")) + "\n", encoding="utf-8")
    quota.write_text("", encoding="utf-8")
    os.chmod(usage, 0o644)
    os.chmod(quota, 0o644)
    before = {path: path.stat() for path in (usage, quota)}
    collect_all(
        state_db=tmp_path / "state.db",
        claude_root=tmp_path / "claude",
        opencode_dbs=[],
        codex_ledger=tmp_path / "codex.jsonl",
        usage_ledger=usage,
        quota_ledger=quota,
        live_quota=False,
        dry_run=True,
    )
    for path in (usage, quota):
        after = path.stat()
        prior = before[path]
        assert (after.st_mode, after.st_mtime_ns, after.st_size) == (prior.st_mode, prior.st_mtime_ns, prior.st_size)
        assert not path.with_name(path.name + ".lock").exists()


@pytest.mark.parametrize("endpoint", ["/api/unified-usage", "/api/billing"])
def test_private_api_rejects_overflowing_days_with_400(tmp_path, endpoint):
    usage = tmp_path / "usage.jsonl"
    billing = tmp_path / "billing.jsonl"
    usage.write_text(canonical_json(_usage_fact()) + "\n", encoding="utf-8")
    billing.write_text(canonical_json(_billing_fact("b", "charge", "1")) + "\n", encoding="utf-8")
    client = create_app(unified_usage_ledger=str(usage), billing_ledger=str(billing)).test_client()
    response = client.get(endpoint + "?days=999999999999999999999")
    assert response.status_code == 400


def test_private_api_enforces_request_wide_row_bound(tmp_path, monkeypatch):
    usage = tmp_path / "usage.sqlite3"
    append_sqlite_facts(usage, [_usage_fact(f"u-{index}") for index in range(3)], fact_type="usage_event_v1")
    monkeypatch.setattr(dashboard_module, "_PRIVATE_API_MAX_ROWS", 2)
    response = create_app(unified_usage_ledger=str(usage)).test_client().get("/api/unified-usage?days=36500")
    assert response.status_code == 413
    assert response.get_json() == {"error": "ledger query exceeds private API row limit"}


def test_private_api_maps_non_utf8_jsonl_to_generic_503(tmp_path):
    usage = tmp_path / "usage.jsonl"
    usage.write_bytes(b"\xff\xfe\n")
    response = create_app(unified_usage_ledger=str(usage)).test_client().get("/api/unified-usage")
    assert response.status_code == 503
    assert response.get_json() == {"error": "configured private ledger is unavailable"}


def test_collect_all_dry_run_skips_mutating_claude_quota_probe(tmp_path, monkeypatch):
    _stub_collectors(monkeypatch, [])
    probe = tmp_path / "probe"

    def forbidden_probe(*args, **kwargs):
        raise AssertionError("Claude quota probe must not run during dry-run")

    monkeypatch.setattr("codex_usage_tracker.collector.collect_claude_code_quota", forbidden_probe)
    monkeypatch.setattr(
        "codex_usage_tracker.collector.collect_claude_code_quota_api", forbidden_probe
    )
    result = collect_all(
        state_db=tmp_path / "state.db",
        claude_root=tmp_path / "claude",
        opencode_dbs=[],
        codex_ledger=tmp_path / "codex.jsonl",
        usage_ledger=tmp_path / "usage.sqlite3",
        quota_ledger=tmp_path / "quota.sqlite3",
        billing_ledger=tmp_path / "billing.sqlite3",
        claude_quota_command="missing-claude",
        claude_probe_dir=probe,
        environment={},
        live_quota=True,
        dry_run=True,
    )
    assert result["sources"]["claude_code_quota"] == {"discovered": 0}
    assert not probe.exists()


def test_normal_collect_preflight_accepts_valid_active_wal(tmp_path, monkeypatch):
    _stub_collectors(monkeypatch, [_usage_fact()])
    usage = tmp_path / "usage.sqlite3"
    quota = tmp_path / "quota.sqlite3"
    billing = tmp_path / "billing.sqlite3"
    append_sqlite_facts(usage, [_usage_fact()], fact_type="usage_event_v1")
    append_sqlite_facts(quota, [], fact_type="quota_observation_v1")
    append_sqlite_facts(billing, [], fact_type="billing_fact_v1")
    reader = sqlite3.connect(usage)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM facts").fetchone()
    try:
        result = collect_all(
            state_db=tmp_path / "state.db",
            claude_root=tmp_path / "claude",
            opencode_dbs=[],
            codex_ledger=tmp_path / "codex.jsonl",
            usage_ledger=usage,
            quota_ledger=quota,
            billing_ledger=billing,
            live_quota=False,
        )
    finally:
        reader.close()
    assert result["usage"]["replayed"] == 1


def test_sqlite_private_api_uses_one_stable_query_snapshot(tmp_path, monkeypatch):
    usage = tmp_path / "usage.sqlite3"
    append_sqlite_facts(
        usage,
        [_usage_fact(f"u-{index}") for index in range(3)],
        fact_type="usage_event_v1",
    )
    # Contract v2 (keeper ratified 2026-09-25): usage reads go through the
    # physical-request election, which reads rows and identity stats in one
    # SQLite snapshot.
    original = dashboard_module.query_sqlite_physical_requests
    calls = 0
    observed_kwargs = None

    def observed_query(*args, **kwargs):
        nonlocal calls, observed_kwargs
        calls += 1
        observed_kwargs = kwargs
        rows, stats = original(*args, **kwargs)
        if calls == 1:
            append_sqlite_facts(usage, [_usage_fact("late")], fact_type="usage_event_v1")
        return rows, stats

    monkeypatch.setattr(dashboard_module, "_PRIVATE_API_SQLITE_LIMIT", 2)
    monkeypatch.setattr(dashboard_module, "query_sqlite_physical_requests", observed_query)
    response = create_app(unified_usage_ledger=str(usage)).test_client().get("/api/unified-usage?days=36500")
    assert response.status_code == 200
    assert calls == 1
    assert observed_kwargs is not None
    assert observed_kwargs["contract_validation"] is False
    payload = response.get_json()
    assert sum(bucket["events"] for bucket in payload["by_provider_model"]) == 3
    assert payload["identity"]["identity_rule"] == "v2.1"
    assert payload["identity"]["observations"] == payload["identity"]["physical_requests"] == 3


def test_private_subscriptions_reads_skip_full_contract_revalidation(tmp_path, monkeypatch):
    """Measured 2026-09-10 on the live quota ledger: a 168-hour subscriptions
    read spent 46 of 50 profiled seconds re-running the full contract and
    secret scanner over 11k immutable rows and sat at the frontend's
    30-second abort. Dashboard reads integrity-check rows (hash, identity,
    typed columns); full contract validation is the audit verb's job."""
    quota = tmp_path / "quota.sqlite3"
    append_sqlite_facts(
        quota,
        _collector_quota_rows("z-ai", "z_ai_api", "ns:z-ai", ("five_hour", "week")),
        fact_type="quota_observation_v1",
    )
    original = dashboard_module.query_sqlite_facts
    observed_kwargs = None

    def observed_query(*args, **kwargs):
        nonlocal observed_kwargs
        observed_kwargs = kwargs
        return original(*args, **kwargs)

    monkeypatch.setattr(dashboard_module, "query_sqlite_facts", observed_query)
    response = create_app(quota_ledger=str(quota)).test_client().get("/api/subscriptions?days=36500")
    assert response.status_code == 200
    assert observed_kwargs is not None
    assert observed_kwargs["contract_validation"] is False


def _pinned_usage_fact(source_event_id, occurred_at, provider):
    return dict(
        _usage_fact(source_event_id),
        occurred_at=occurred_at,
        recorded_at=occurred_at,
        provider=provider,
    )


def test_unified_usage_defaults_unbounded_reads_to_latest_eight_days(tmp_path):
    # Keeper ruling 2026-08-29: a query with no window returns the latest rows
    # an 8-day reconstruction needs instead of tripping the row-cap 413.
    recent_at = (
        (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z")
    )
    old_at = (
        (datetime.now(timezone.utc) - timedelta(days=30)).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z")
    )
    rows = [
        _pinned_usage_fact("recent", recent_at, "recent-provider"),
        _pinned_usage_fact("old", old_at, "old-provider"),
    ]
    jsonl = tmp_path / "usage.jsonl"
    jsonl.write_text("\n".join(canonical_json(row) for row in rows) + "\n", encoding="utf-8")
    sqlite_path = tmp_path / "usage.sqlite3"
    append_sqlite_facts(sqlite_path, rows, fact_type="usage_event_v1")
    for ledger in (jsonl, sqlite_path):
        client = create_app(unified_usage_ledger=str(ledger)).test_client()
        response = client.get("/api/unified-usage")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["window"]["event_count"] == 1
        assert payload["window"]["days"] is None
        assert payload["window"]["default_days"] == 8
        assert payload["window"]["next_before"].endswith("Z")
        assert [bucket["provider"] for bucket in payload["by_provider_model"]] == ["recent-provider"]
        # Explicit days keeps its now-anchored, upper-unbounded semantics.
        explicit = client.get("/api/unified-usage?days=36500").get_json()
        assert explicit["window"]["event_count"] == 2
        assert explicit["window"]["days"] == 36500
        assert "default_days" not in explicit["window"]
        assert "next_before" not in explicit["window"]


def test_unified_usage_before_cursor_pages_are_disjoint_and_complete(tmp_path):
    # Cursor paging is timestamp-bounded: each page is the half-open window
    # [before - days, before), so a fact on the shared boundary lands in
    # exactly one page for both ledger backends.
    facts = [
        _pinned_usage_fact("a", "2026-07-12T12:00:00Z", "provider-a"),
        _pinned_usage_fact("b", "2026-07-11T00:00:00Z", "provider-b"),  # page boundary
        _pinned_usage_fact("c", "2026-07-10T06:00:00Z", "provider-c"),
        _pinned_usage_fact("d", "2026-07-09T00:00:00Z", "provider-d"),  # page-2 lower bound
        _pinned_usage_fact("e", "2026-07-13T00:00:00Z", "provider-e"),  # == before, excluded
    ]
    jsonl = tmp_path / "usage.jsonl"
    jsonl.write_text("\n".join(canonical_json(row) for row in facts) + "\n", encoding="utf-8")
    sqlite_path = tmp_path / "usage.sqlite3"
    append_sqlite_facts(sqlite_path, facts, fact_type="usage_event_v1")

    def providers(payload):
        return {bucket["provider"] for bucket in payload["by_provider_model"]}

    for ledger in (jsonl, sqlite_path):
        client = create_app(unified_usage_ledger=str(ledger)).test_client()
        first = client.get("/api/unified-usage?days=2&before=2026-07-13T00:00:00Z").get_json()
        assert first["window"]["before"] == "2026-07-13T00:00:00Z"
        assert first["window"]["next_before"] == "2026-07-11T00:00:00Z"
        assert providers(first) == {"provider-a", "provider-b"}
        second = client.get(
            "/api/unified-usage?days=2&before=" + first["window"]["next_before"]
        ).get_json()
        assert providers(second) == {"provider-c", "provider-d"}
        assert second["window"]["next_before"] == "2026-07-09T00:00:00Z"
        # Without days, a cursor page keeps the default eight-day length.
        defaulted = client.get("/api/unified-usage?before=2026-07-13T00:00:00Z").get_json()
        assert defaulted["window"]["default_days"] == 8
        assert defaulted["window"]["next_before"] == "2026-07-05T00:00:00Z"
        assert providers(defaulted) == {"provider-a", "provider-b", "provider-c", "provider-d"}


def test_private_api_rejects_malformed_before_and_before_with_hours(tmp_path):
    usage = tmp_path / "usage.jsonl"
    usage.write_text("", encoding="utf-8")
    client = create_app(unified_usage_ledger=str(usage)).test_client()
    for query in ("before=nope", "before=1783857600"):
        response = client.get("/api/unified-usage?" + query)
        assert response.status_code == 400
        assert response.get_json() == {"error": "before must be an RFC 3339 timestamp"}
    mixed = client.get("/api/unified-usage?hours=3&before=2026-07-13T00:00:00Z")
    assert mixed.status_code == 400
    assert "hours" in mixed.get_json()["error"]


def test_billing_defaults_unbounded_reads_to_latest_eight_days(tmp_path):
    recent_at = (
        (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z")
    )
    ledger = tmp_path / "billing.jsonl"
    ledger.write_text(
        "\n".join(
            canonical_json(row)
            for row in (
                _billing_fact("recent", "charge", "5", occurred_at=recent_at),
                _billing_fact("old", "charge", "7"),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    client = create_app(billing_ledger=str(ledger)).test_client()
    defaulted = client.get("/api/billing").get_json()
    assert [row["amount"] for row in defaulted["transactions"]] == ["5"]
    assert defaulted["window"]["days"] is None
    assert defaulted["window"]["default_days"] == 8
    explicit = client.get("/api/billing?days=36500").get_json()
    assert len(explicit["transactions"]) == 2
    assert explicit["window"] == {"days": 36500}
