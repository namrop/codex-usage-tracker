from __future__ import annotations

import json
import re

from codex_usage_tracker.dashboard import create_app


def _write_usage_ledger(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_dashboard_exposes_policy_budget_capability_and_scaffold_routes(tmp_path, monkeypatch):
    ledger = tmp_path / "usage.jsonl"
    atrium_root = tmp_path / "atrium"
    _write_usage_ledger(
        ledger,
        [
            {
                "fetched_at": "2026-06-04T01:00:00+00:00",
                "weekly_used_pct": 72.0,
                "session_used_pct": 5.0,
                "weekly_reset_at": 1781137839,
                "allowed": True,
                "limit_reached": False,
            },
            {
                "fetched_at": "2026-06-04T02:00:00+00:00",
                "weekly_used_pct": 74.0,
                "session_used_pct": 7.0,
                "weekly_reset_at": 1781137839,
                "allowed": True,
                "limit_reached": False,
            },
        ],
    )
    spend_ledger = atrium_root / "12_runtime/ledgers/model_routing/direct_provider_spend_ledger.jsonl"
    spend_ledger.parent.mkdir(parents=True)
    spend_ledger.write_text(
        json.dumps({"provider": "deepseek", "model": "deepseek-v4-pro", "billed_usd": 1.25, "started_at": "2026-06-04T02:30:00+00:00"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_STATE_DB_PATH", str(tmp_path / "missing.db"))

    client = create_app(atrium_root=str(atrium_root), ledger=str(ledger)).test_client()

    policy = client.get("/api/policy-state")
    assert policy.status_code == 200
    assert policy.get_json()["policy_mode"] == "caution"

    budget = client.get("/api/budget-state")
    assert budget.status_code == 200
    assert budget.get_json()["direct_provider_spend_usd"] == 1.25

    capability = client.get("/api/capability-matrix")
    assert capability.status_code == 200
    assert any(row["provider"] == "deepseek" for row in capability.get_json())

    provider_spend = client.get("/api/provider-spend")
    assert provider_spend.status_code == 200
    assert provider_spend.get_json()["total_billed_usd"] == 1.25

    accounting = client.get("/api/codex-call-accounting")
    assert accounting.status_code == 200
    assert accounting.get_json()[0]["source"] == "hourly_correlation"

    assert client.get("/api/routing-decisions").status_code == 200
    assert client.get("/api/task-outcomes").status_code == 200
    assert client.get("/api/backtests/latest").status_code == 200


def test_dashboard_reconciles_new_codex_weekly_only_payloads(tmp_path, monkeypatch):
    ledger = tmp_path / "usage.jsonl"
    _write_usage_ledger(
        ledger,
        [
            {
                "fetched_at": "2026-07-14T10:00:01+00:00",
                "weekly_used_pct": None,
                "session_used_pct": 15.0,
                "weekly_reset_at": None,
                "session_reset_at": 1784489942,
                "session_reset_after_seconds": 466741,
                "weekly_reset_after_seconds": 18000,
                "hours_until_session_reset": 129.65,
                "hours_until_weekly_reset": 5.0,
                "allowed": True,
                "limit_reached": False,
                "raw_payload": {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 15,
                            "limit_window_seconds": 604800,
                            "reset_at": 1784489942,
                        },
                        "secondary_window": None,
                    }
                },
            }
        ],
    )
    monkeypatch.setenv("HERMES_STATE_DB_PATH", str(tmp_path / "missing.db"))
    client = create_app(atrium_root=str(tmp_path / "atrium"), ledger=str(ledger)).test_client()

    summary = client.get("/api/summary").get_json()
    assert summary["current_session_used_pct"] is None
    assert summary["session_reset_at"] is None
    assert summary["current_weekly_used_pct"] == 15.0
    assert summary["weekly_reset_at"] == 1784489942
    data_row = client.get("/api/data").get_json()[0]
    assert data_row["hours_until_session_reset"] is None
    assert data_row["session_reset_after_seconds"] is None
    assert data_row["hours_until_weekly_reset"] == 129.65
    assert data_row["weekly_reset_after_seconds"] == 466741
    policy = client.get("/api/policy-state").get_json()
    assert policy["hours_until_weekly_reset"] == 129.65


def test_dashboard_summary_exposes_banked_reset_credits(tmp_path, monkeypatch):
    ledger = tmp_path / "usage.jsonl"
    _write_usage_ledger(
        ledger,
        [
            {
                "fetched_at": "2026-06-04T02:00:00+00:00",
                "weekly_used_pct": 74.0,
                "session_used_pct": 7.0,
                "weekly_reset_at": 1781137839,
                "allowed": True,
                "limit_reached": False,
                "raw_payload": {"rate_limit_reset_credits": {"available_count": 3}},
            },
        ],
    )
    monkeypatch.setenv("HERMES_STATE_DB_PATH", str(tmp_path / "missing.db"))

    client = create_app(atrium_root=str(tmp_path / "atrium"), ledger=str(ledger)).test_client()

    summary = client.get("/api/summary")
    assert summary.status_code == 200
    assert summary.get_json()["rate_limit_reset_credits_available"] == 3

    data = client.get("/api/data")
    assert data.status_code == 200
    assert data.get_json()[0]["rate_limit_reset_credits_available"] == 3

    page = client.get("/")
    assert page.status_code == 200
    assert b"Banked resets" in page.data
    assert b"manual" in page.data


def _quota_observation(source_id: str, quota_name: str, **updates):
    row = {
        "fact_type": "quota_observation_v1",
        "schema_version": 1,
        "source_namespace": "private:test-quota",
        "source_observation_id": source_id,
        "harness": "hermes",
        "observed_at": "2026-08-05T12:00:00Z",
        "provider": "openai-codex",
        "quota_name": quota_name,
        "quota_scope": "account",
        "window_kind": "rolling",
        "unit": "percent",
        "measurement_confidence": "estimated",
        "account_ref": "private-account-reference",
        "provider_payload_ref": "/private/provider/payload",
    }
    row.update(updates)
    return row


def test_subscription_api_projects_only_safe_normalized_extension_presentations(tmp_path):
    quota_ledger = tmp_path / "quota.jsonl"
    _write_usage_ledger(
        quota_ledger,
        [
            _quota_observation(
                "safe",
                "week",
                x_provider_state="inactive_or_not_reported",
                x_coverage={
                    "status": "partial",
                    "harnesses_with_values": ["hermes"],
                    "valued_events": 7,
                    "details": {"internal_note": "private-coverage-marker"},
                },
                x_arbitrary_nested={"details": ["arbitrary-extension-marker"]},
                x_payload_ref="/private/arbitrary/payload",
            ),
            _quota_observation(
                "untrusted",
                "five_hour",
                x_provider_state="operator_supplied_untrusted_state",
                x_coverage={
                    "status": "complete",
                    "details": {"internal_note": "untrusted-coverage-marker"},
                },
            ),
            _quota_observation(
                "non-mapping-coverage",
                "month",
                x_coverage="partial",
            ),
        ],
    )
    client = create_app(
        atrium_root=str(tmp_path / "atrium"),
        ledger=str(tmp_path / "usage.jsonl"),
        quota_ledger=str(quota_ledger),
    ).test_client()

    response = client.get("/api/subscriptions")
    assert response.status_code == 200
    payload = response.get_json()
    expected_fields = {
        "harness",
        "observed_at",
        "provider",
        "quota_name",
        "quota_scope",
        "window_kind",
        "window_started_at",
        "window_ends_at",
        "resets_at",
        "limit_value",
        "remaining_value",
        "used_value",
        "unit",
        "measurement_confidence",
        "provider_state",
        "coverage_status",
    }
    assert all(set(row) == expected_fields for row in payload["latest"])
    assert all(set(row) == expected_fields for row in payload["observations"])

    latest_by_quota = {row["quota_name"]: row for row in payload["latest"]}
    assert latest_by_quota["week"]["provider_state"] == "inactive_or_not_reported"
    assert latest_by_quota["week"]["coverage_status"] == "partial"
    assert latest_by_quota["five_hour"]["provider_state"] is None
    assert latest_by_quota["five_hour"]["coverage_status"] is None
    assert latest_by_quota["month"]["coverage_status"] is None

    serialized = json.dumps(payload)
    for private_value in (
        "private:test-quota",
        "private-account-reference",
        "/private/provider/payload",
        "/private/arbitrary/payload",
        "private-coverage-marker",
        "arbitrary-extension-marker",
        "operator_supplied_untrusted_state",
        "untrusted-coverage-marker",
    ):
        assert private_value not in serialized


def _dashboard_root_html(tmp_path, monkeypatch) -> str:
    monkeypatch.setenv("HERMES_STATE_DB_PATH", str(tmp_path / "missing.db"))
    client = create_app(
        atrium_root=str(tmp_path / "atrium"),
        ledger=str(tmp_path / "usage.jsonl"),
    ).test_client()
    response = client.get("/")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_subscription_provider_overview_and_raw_observation_details_exist(tmp_path, monkeypatch):
    html = _dashboard_root_html(tmp_path, monkeypatch)

    assert 'id="subscriptionProviderGrid"' in html
    assert 'id="subscriptionRawDetails"' in html
    assert 'id="subscriptionsBody"' in html
    assert re.search(r"<th[^>]*>Observed</th>\s*<th[^>]*>Confidence</th>", html)
    for display_name in (
        "OpenAI / Codex",
        "Anthropic / Claude",
        "OpenCode Go",
        "Z.AI / GLM",
        "Kimi K3 Coding",
    ):
        assert display_name in html
    assert (
        "Usage events above, subscription quota observations here, and billing facts below "
        "are separate record classes."
    ) in html
    assert "No quota estimate is a billing fact." in html


def test_subscription_provider_js_preserves_authority_and_freshness(tmp_path, monkeypatch):
    html = _dashboard_root_html(tmp_path, monkeypatch)
    script = html.split("async function loadSubscriptions()", 1)[1].split(
        "async function loadBilling()", 1
    )[0]

    for label in ("Exact", "Estimated / partial", "Unknown"):
        assert label in script
    assert "measurement_confidence" in script
    assert "coverage_status" in script
    assert "provider_state" in script
    assert "x_coverage" not in script
    assert "x_provider_state" not in script
    assert "data.provider_states" not in script
    assert "data.x_provider_states" not in script
    assert "observed_at" in script
    assert "threshold_seconds" in script
    assert "21600" in script
    assert "providerGroups" in script
    assert "document.createElement" in script
    assert ".textContent" in script
    assert ".innerHTML" not in script

    stale_branch = script.split('if (freshness.status === "stale")', 1)[1].split(
        'if (freshness.status === "empty")', 1
    )[0]
    assert "return" not in stale_branch


def test_subscription_freshness_prefers_threshold_then_api_max_age_then_default(
    tmp_path, monkeypatch
):
    html = _dashboard_root_html(tmp_path, monkeypatch)
    script = html.split("async function loadSubscriptions()", 1)[1].split(
        "async function loadBilling()", 1
    )[0]

    threshold_position = script.index("freshness.threshold_seconds")
    max_age_position = script.index("freshness.max_age_seconds")
    default_position = script.index("21600", max_age_position)
    assert threshold_position < max_age_position < default_position
    assert "Number.isFinite" in script[threshold_position:default_position]


def test_subscription_groups_seed_expected_providers_and_keep_unknown_extras(
    tmp_path, monkeypatch
):
    html = _dashboard_root_html(tmp_path, monkeypatch)
    script = html.split("async function loadSubscriptions()", 1)[1].split(
        "async function loadBilling()", 1
    )[0]

    seed_statement = "providerOrder.forEach((provider) => ensureProviderGroup(provider));"
    assert seed_statement in script
    assert script.index(seed_statement) < script.index("rows.forEach((row) =>")
    assert 'providerAliases[normalized] || normalized || "unknown"' in script
    assert "Unknown freshness" in script
    assert "No quota windows available for this provider." in script
    assert "if (!providerGroups.size)" not in script


def test_subscription_provider_cards_are_mobile_contained(tmp_path, monkeypatch):
    html = _dashboard_root_html(tmp_path, monkeypatch)

    card_rule = re.search(r"\.subscription-provider-card\s*\{(?P<body>[^}]*)\}", html)
    assert card_rule is not None
    declarations = card_rule.group("body")
    assert re.search(r"min-width:\s*0\s*;", declarations)
    assert re.search(r"max-width:\s*100%\s*;", declarations)
    assert re.search(r"overflow:\s*hidden\s*;", declarations)
    assert re.search(
        r"@media \(max-width: 640px\).*?\.subscription-provider-grid\s*\{"
        r"[^}]*grid-template-columns:\s*1fr\s*;",
        html,
        re.DOTALL,
    )


def test_subscription_unavailable_path_renders_expected_provider_placeholders(
    tmp_path, monkeypatch
):
    html = _dashboard_root_html(tmp_path, monkeypatch)
    script = html.split("function renderUnavailableSubscriptionPlaceholders()", 1)
    assert len(script) == 2
    helper, loader = script[1].split("async function loadSubscriptions()", 1)

    for display_name in (
        "OpenAI / Codex",
        "Anthropic / Claude",
        "OpenCode Go",
        "Z.AI / GLM",
        "Kimi K3 Coding",
    ):
        assert display_name in helper
    assert 'grid.textContent = ""' in helper
    assert '"Unknown freshness"' in helper
    assert '"No quota windows available for this provider."' in helper
    assert 'state.textContent = "Subscription data unavailable."' in helper
    assert "state.hidden = false" in helper
    assert "content.hidden = false" in helper
    assert "document.createElement" in helper
    assert ".textContent" in helper
    assert ".innerHTML" not in helper

    catch_branch = loader.split("async function loadBilling()", 1)[0].split(
        "} catch (error) {", 1
    )[1]
    assert "renderUnavailableSubscriptionPlaceholders();" in catch_branch
    assert "showPanel(" not in catch_branch
