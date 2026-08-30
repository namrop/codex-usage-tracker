from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

import pytest

from codex_usage_tracker.canonical_ledger import append_sqlite_facts, canonical_json
import codex_usage_tracker.dashboard as dashboard_module
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

    # The Codex detail section is gone (keeper-ruled 2026-08-30); banked
    # resets are an accepted loss from the page, but the API keeps serving
    # them for non-dashboard consumers.
    page = client.get("/")
    assert page.status_code == 200
    assert b"Banked resets" not in page.data


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

    # The fixture observations are pinned dates, so an explicit window keeps
    # them visible now that unbounded reads default to the latest eight days.
    response = client.get("/api/subscriptions?days=36500")
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
        # Reset-rail additions: the canonical provider key the donut legend
        # joins on, and an account ORDINAL. account_ref itself stays private —
        # the serialized-payload scan at the end of this test is the gate.
        "provider_canonical",
        "provider_account_index",
        "provider_account_count",
    }
    assert all(set(row) == expected_fields for row in payload["latest"])
    assert all(set(row) == expected_fields for row in payload["observations"])
    assert all(
        row["provider_canonical"] == "openai-codex"
        and row["provider_account_index"] == 1
        and row["provider_account_count"] == 1
        for row in payload["latest"]
    )

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
    # The raw observation ledger moved from <details> to the fold system
    # (keeper-ruled 2026-08-29: ledger tables collapsed by default).
    assert 'data-fold="subscriptionsRaw"' in html
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


FOLD_KEYS = (
    "modelTokenSeries",
    # Estate-wide charts round (keeper-ruled 2026-08-30): the token
    # composition and cache-hit-rate charts carry the same chart+fold-table
    # pattern as the model token series.
    "tokenCompositionSeries",
    "cacheHitRateSeries",
    "harnessUsage",
    "providerModelUsage",
    "subscriptionWeeklySeries",
    "subscriptionFiveHourSeries",
    "subscriptionsRaw",
    "billingLedger",
)

COLUMN_TIERED_FOLDS = (
    ("harnessUsage", "tier-harness"),
    ("providerModelUsage", "tier-provider-model"),
    ("subscriptionsRaw", "tier-quota"),
    ("billingLedger", "tier-billing"),
)


def test_every_ledger_table_ships_collapsed_behind_an_accessible_fold(tmp_path, monkeypatch):
    """Keeper-ruled 2026-08-29: the ledger tables and columns are collapsed by
    default. Fold is a class/attribute toggle, never a removal — the tbodies
    ship in the DOM even while folded, so the hydrate cycle keeps writing into
    them and nothing is lazy-rendered on expand."""
    html = _dashboard_root_html(tmp_path, monkeypatch)

    for key in FOLD_KEYS:
        fold = re.search(
            rf'<section class="fold" data-fold="{key}"(?P<attrs>[^>]*)>', html
        )
        assert fold, f"{key}: no fold wrapper"
        assert 'data-open="false"' in fold.group("attrs"), f"{key}: not collapsed by default"
        toggle = re.search(
            rf'<button type="button" class="fold-toggle" id="{key}FoldToggle"[^>]*>', html
        )
        assert toggle, f"{key}: fold toggle is not a real button"
        assert 'aria-expanded="false"' in toggle.group(0), f"{key}: missing collapsed aria-expanded"
        assert f'aria-controls="{key}FoldBody"' in toggle.group(0)
        assert (
            f'<div class="fold-body" id="{key}FoldBody" role="region" '
            f'aria-labelledby="{key}FoldToggle">'
        ) in html


def test_wide_ledger_tables_default_to_the_essential_column_tier(tmp_path, monkeypatch):
    html = _dashboard_root_html(tmp_path, monkeypatch)

    for key, tier_class in COLUMN_TIERED_FOLDS:
        fold = re.search(rf'<section class="fold" data-fold="{key}"(?P<attrs>[^>]*)>', html)
        assert fold, f"{key}: no fold wrapper"
        assert 'data-cols="essential"' in fold.group("attrs"), f"{key}: wide table not tiered"
        assert f'class="{tier_class}"' in html, f"{key}: table missing its column-tier class"
        assert re.search(
            rf'<button type="button" class="fold-cols" data-fold-cols aria-pressed="false"'
            rf' aria-controls="{key}FoldBody">All columns</button>',
            html,
        ), f"{key}: no all-columns toggle"
    # Column collapse is visibility, never DOM removal: the hidden columns are
    # still declared in the shipped markup.
    assert re.search(r"\.fold\[data-cols=.essential.\][^{]*\{\s*display: none;", html)


def test_fold_state_is_remembered_and_lives_outside_the_rendered_tbodies(tmp_path, monkeypatch):
    html = _dashboard_root_html(tmp_path, monkeypatch)

    assert 'FOLD_OPEN_KEY = "aiUsage.fold.open"' in html
    assert 'FOLD_COLS_KEY = "aiUsage.fold.cols"' in html
    # Unset preference means collapsed / essential, never remembered-open.
    # ?expand=all is a one-load debug/print affordance; unset prefs still
    # default collapsed and the affordance writes nothing back.
    assert "const isOpen = expandAll || openPrefs[key] === true;" in html
    assert "const allColumns = colPrefs[key] === true;" in html
    # Summaries re-derive from the rows in hand whenever a renderer rewrites a
    # tbody, so the 60s hydrate refreshes them without disturbing fold state.
    assert "new MutationObserver" in html
    assert "function foldSummaryText(fold)" in html


def test_codex_detail_section_is_removed_entirely(tmp_path, monkeypatch):
    """Keeper-ruled 2026-08-30: the Codex detail compatibility section is gone
    outright — hero, trend fold, correlation charts, token correlation ledger,
    and raw ledger rows, with all of their JS. The weekly codex quota signal
    survives in the subscriptions cards and the reset rail; banked resets and
    plan are accepted losses. The server endpoints those views consumed stay
    (other consumers may exist); the page just never fetches them."""
    html = _dashboard_root_html(tmp_path, monkeypatch)

    assert "Codex detail" not in html
    assert "Compatibility views" not in html
    assert "section-label" not in html
    # The hero and its monitor tiles.
    for retired in (
        'id="globalStatus"', 'id="planType"', 'id="currentWeekly"', 'id="resetCredits"',
        'id="freshness"', "Codex usage monitor", "Limits, resets, and headroom.",
        "Banked resets",
    ):
        assert retired not in html
    # The trend fold, correlation charts, and both ledger tables.
    for retired in (
        "codexUsageTrend", 'id="weekChart"', 'id="tokenUsageChart"',
        'id="tokenBucketChart"', 'id="tokenDeltaChart"', 'id="cacheHitChart"',
        # ("Token bucket mix" survives only as a provenance mention in the
        # replacement chart's comment, so it is not asserted absent.)
        "Usage over time", "Usage × tokens over time",
        "Quota movement", "Token correlation ledger", "Ledger rows",
        'data-fold="tokenLedger"', 'data-fold="rawLedger"', "tier-token-ledger",
        "detail-grid",
    ):
        assert retired not in html
    # The dead JS went with the markup — render paths, sorters, and the
    # codex-only chart builders.
    for retired_js in (
        "renderHeader", "renderCharts()", "renderTokenLedger", "renderSortedTable",
        "buildOrUpdateWeekChart", "buildOrUpdateTokenUsageChart",
        "buildOrUpdateTokenBucketChart", "buildOrUpdateTokenDeltaChart",
        "buildOrUpdateCacheHitChart", "thresholdPlugin", "endpointLabelPlugin",
        "state.trend", "state.tokenChart", "state.tokenLedger", "state.sortBy",
    ):
        assert retired_js not in html
    # The page no longer fetches the codex JSONL endpoints at all.
    for retired_fetch in (
        '"/api/summary"', '"/api/trend"', '"/api/data', '"/api/token-ledger"',
        '"/api/token-chart"',
    ):
        assert retired_fetch not in html
    # The refresh button survives the section it used to live in, and the
    # refresh cycle drives the unified surfaces.
    assert 'id="refreshUsage"' in html
    assert "els.refresh.addEventListener" in html
    assert "if (!document.hidden) hydrate();" in html


def test_frontend_orphaned_codex_endpoints_are_still_served(tmp_path, monkeypatch):
    """The keeper kept the server side of the removed views: /api/summary,
    /api/trend, /api/data, /api/token-ledger, and /api/token-chart stay
    routable for non-dashboard consumers even though the page stopped
    fetching them."""
    ledger = tmp_path / "usage.jsonl"
    _write_usage_ledger(
        ledger,
        [{"fetched_at": "2026-06-04T02:00:00+00:00", "weekly_used_pct": 74.0, "weekly_reset_at": 1781137839}],
    )
    monkeypatch.setenv("HERMES_STATE_DB_PATH", str(tmp_path / "missing.db"))
    client = create_app(atrium_root=str(tmp_path / "atrium"), ledger=str(ledger)).test_client()

    for endpoint in ("/api/summary", "/api/trend", "/api/data", "/api/token-ledger", "/api/token-chart"):
        assert client.get(endpoint).status_code == 200, endpoint


def test_estate_wide_token_composition_and_cache_hit_charts_exist(tmp_path, monkeypatch):
    """Keeper-ruled 2026-08-30: the codex-only Token bucket mix and Cache hit
    rate charts are replaced by estate-wide charts in the unified section —
    stacked hourly token composition from bucket_series, and per-provider
    cache-hit lines from the provider_status_series token sums."""
    html = _dashboard_root_html(tmp_path, monkeypatch)

    # Token composition: stacked bars, SERIES bucket colors, chart+fold table.
    assert "Token composition" in html
    assert 'id="tokenCompositionChart"' in html
    assert 'id="tokenCompositionLabels"' in html
    assert "bucket_series" in html
    assert "TOKEN_COMPOSITION_BUCKETS" in html
    for bucket_label in ('"Input"', '"Cache read"', '"Cache write"', '"Output"', '"Reasoning"'):
        assert bucket_label in html
    assert 'stack: "composition"' in html

    # Cache hit rate: per-provider lines in provider colors, 0-100 percent,
    # gaps where a provider had no prompt tokens, window-average legend.
    assert "Cache hit rate" in html
    assert 'id="cacheHitRateChart"' in html
    assert 'id="cacheHitRateLabels"' in html
    assert "function renderCacheHitRate" in html
    assert "spanGaps: false" in html
    assert "prompt > 0 ? (cache / prompt) * 100 : null" in html
    assert "windowAverage" in html
    # Provider identity rides the same rank-slot mapping every other unified
    # surface uses.
    assert html.count("subscriptionUnifiedProviderColor({ provider: row.provider_canonical }, index)") >= 3
    # The status strip's low-traffic folding rule applies to the chart too.
    assert 'row.provider !== "unknown" && Number(row.total_events) >= 10' in html
    assert "Low-traffic fold:" in html

    # Both new charts sit in the unified section, after the model token chart
    # and before the harness fold.
    model_at = html.index('id="modelTokenChart"')
    composition_at = html.index('id="tokenCompositionChart"')
    cache_at = html.index('id="cacheHitRateChart"')
    harness_at = html.index('data-fold="harnessUsage"')
    assert model_at < composition_at < cache_at < harness_at


def test_captions_are_single_sentence_with_doctrine_at_section_level(
    tmp_path, monkeypatch
):
    """Keeper polish round 2026-08-30, item 7: card captions say what the
    surface shows in one sentence; shared doctrine lives once at section
    level."""
    html = _dashboard_root_html(tmp_path, monkeypatch)

    # The pool/slices doctrine appears exactly once, on the unified section
    # header, not per-donut.
    assert html.count("never shares of one pool") == 1
    assert html.count("never drawn as slices") == 1
    # The tokens-vs-dollars doctrine appears exactly once, on the
    # Subscriptions header, not on each twin utilization chart.
    assert html.count("never tokens or dollars") == 1
    assert "this does not compare tokens or dollars" not in html
    # The UTC policy note lives once at section level.
    assert html.count("complete UTC hours (countdowns run in wall-clock time)") == 1


def test_subscription_meters_never_draw_a_lying_bar(tmp_path, monkeypatch):
    """Keeper polish round 2026-08-30, items 8-10: lifetime/unknown windows and
    used>limit rows render balance-style (no used/limit bar), null-sided
    usd/credits quotas render as balances with a role accent, and kimi's
    provider_unit limit-100 scale displays as a percentage."""
    html = _dashboard_root_html(tmp_path, monkeypatch)
    script = html.split("async function loadSubscriptions()", 1)[1].split(
        "async function loadBilling()", 1
    )[0]

    # Item 8: the meterless gate covers lifetime, unknown, and used > limit,
    # and the authoritative-bar gate excludes those rows.
    assert 'windowKindText === "lifetime"' in script
    assert 'windowKindText === "unknown"' in script
    assert "usedNumber > limitNumber" in script
    assert "!balanceOnly" in script and "!meterless" in script
    assert '"Spent"' in script
    # Item 9: balance rendering with stability/violation accents.
    assert '"Balance"' in script
    assert 'remainingNumber > 0 ? "stability" : "violation"' in script
    assert ".subscription-quota-value.role-stability" in html
    assert ".subscription-quota-value.role-violation" in html
    # Item 10: provider_unit with limit 100 displays as %, provenance in the
    # tooltip, canonical unit untouched server-side.
    assert 'rawUnit === "provider_unit" && limitNumber === 100' in script
    assert "normalized 0–100 scale reported by the provider" in script


def test_stale_notices_name_the_failing_lane(tmp_path, monkeypatch):
    html = _dashboard_root_html(tmp_path, monkeypatch)

    assert "function staleLaneSuffix(freshness)" in html
    assert "no successful run on record" in html
    # Four callers: the definition, the two panel stale notices, and the hero
    # ops digest's freshness mirror (polish round 2026-08-30, item 2).
    assert html.count("staleLaneSuffix(freshness)") == 4
    assert "renderFreshnessDigest(freshness)" in html
    for notice in (
        "Canonical usage is stale",
        "Subscription observations are stale",
    ):
        assert notice in html


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
    assert "const aliasKey = rawProvider.toLowerCase();" in script
    assert (
        "Object.prototype.hasOwnProperty.call(providerAliases, aliasKey)"
        in script
    )
    assert "providerAliases[normalized]" not in script
    assert "providerAliases[compact]" not in script
    assert "const compact" not in script
    assert '[".", "_", "/", " "]' not in script
    alias_declaration = re.search(
        r"const providerAliases = (?P<aliases>\{.*?\});", script, re.DOTALL
    )
    assert alias_declaration is not None
    frontend_aliases = json.loads(
        re.sub(r",(\s*})$", r"\1", alias_declaration.group("aliases"))
    )
    assert frontend_aliases == dashboard_module._QUOTA_PROVIDER_ALIASES
    assert "return group.key;" in script
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
    assert 'statusEl.textContent = "Subscription data unavailable."' in helper
    assert "statusEl.hidden = false" in helper
    assert "content.hidden = false" in helper
    # Placeholders are visible content, so they must not be mistaken for real data.
    assert "subscriptionsHaveRenderedData = false;" in helper
    assert "document.createElement" in helper
    assert ".textContent" in helper
    assert ".innerHTML" not in helper

    catch_branch = loader.split("async function loadBilling()", 1)[0].split(
        "} catch (error) {", 1
    )[1]
    assert "renderUnavailableSubscriptionPlaceholders();" in catch_branch
    assert "showPanel(" not in catch_branch
    # "showing last good data" is gated on a real render, never on content.hidden.
    assert "if (subscriptionsHaveRenderedData) {" in catch_branch
    assert "content.hidden" not in catch_branch


@pytest.mark.parametrize(
    "query",
    ["hours=0", "hours=169", "hours=nope", "hours=1.5", "hours=3&days=1"],
)
def test_subscription_chart_rejects_invalid_or_ambiguous_hour_windows(tmp_path, query):
    quota_ledger = tmp_path / "quota.jsonl"
    quota_ledger.write_text("", encoding="utf-8")

    response = create_app(quota_ledger=str(quota_ledger)).test_client().get(
        "/api/subscriptions?" + query
    )

    assert response.status_code == 400
    assert "hours" in response.get_json()["error"]


def test_subscription_chart_is_opt_in_and_preserves_days_history_contract(tmp_path):
    quota_ledger = tmp_path / "quota.jsonl"
    quota_ledger.write_text("", encoding="utf-8")

    payload = create_app(quota_ledger=str(quota_ledger)).test_client().get(
        "/api/subscriptions?days=30&history=1"
    ).get_json()

    assert set(payload) == {"freshness", "latest", "observations", "window"}
    assert payload["window"] == {"days": 30}


def test_subscription_hours_default_excludes_history_and_rejects_history_opt_in(tmp_path):
    quota_ledger = tmp_path / "quota.jsonl"
    quota_ledger.write_text("", encoding="utf-8")
    client = create_app(quota_ledger=str(quota_ledger)).test_client()

    chart_response = client.get("/api/subscriptions?hours=1")
    assert chart_response.status_code == 200
    assert chart_response.get_json()["observations"] == []

    rejected = client.get("/api/subscriptions?hours=1&history=1")
    assert rejected.status_code == 400
    assert rejected.get_json() == {
        "error": "history=1 is not supported with hours chart mode"
    }


def test_subscription_chart_seeds_expected_providers_for_empty_and_partial_ledgers(
    tmp_path, monkeypatch
):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 5, 12, 30, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)
    expected = [
        "openai-codex",
        "anthropic",
        "opencode-go",
        "z-ai-glm",
        "kimi-k3-coding",
    ]

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    empty_chart = create_app(quota_ledger=str(empty)).test_client().get(
        "/api/subscriptions?hours=1"
    ).get_json()["time_series"]
    assert [provider["provider"] for provider in empty_chart["providers"]] == expected
    assert all(provider["status"] == "no_observations" for provider in empty_chart["providers"])
    assert all(provider["series"] == [] for provider in empty_chart["providers"])
    assert all(provider["unavailable_series"] == [] for provider in empty_chart["providers"])
    assert empty_chart["unified_five_hour"]["series"] == []
    assert [
        row["provider"]
        for row in empty_chart["unified_five_hour"]["no_data_providers"]
    ] == expected

    partial = tmp_path / "partial.jsonl"
    partial.write_text(
        canonical_json(
            _quota_observation(
                "extra-week",
                "week",
                observed_at="2026-08-05T11:15:00Z",
                provider="example-provider",
                used_value="20",
                limit_value="100",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    partial_chart = create_app(quota_ledger=str(partial)).test_client().get(
        "/api/subscriptions?hours=1"
    ).get_json()["time_series"]
    assert [provider["provider"] for provider in partial_chart["providers"]][:5] == expected
    assert partial_chart["providers"][5]["provider"] == "example-provider"
    assert partial_chart["providers"][5]["status"] == "available"


def test_quota_provider_aliases_are_the_complete_explicit_allowlist():
    assert dashboard_module._QUOTA_PROVIDER_ALIASES == {
        "openai": "openai-codex",
        "openai-codex": "openai-codex",
        "codex": "openai-codex",
        "anthropic": "anthropic",
        "claude": "anthropic",
        "claude-code": "anthropic",
        "opencode-go": "opencode-go",
        "z.ai": "z-ai-glm",
        "z-ai": "z-ai-glm",
        "z-ai-glm": "z-ai-glm",
        "zai": "z-ai-glm",
        "zhipu": "z-ai-glm",
        "zhipuai": "z-ai-glm",
        "glm": "z-ai-glm",
        "kimi": "kimi-k3-coding",
        "kimi-code": "kimi-k3-coding",
        "kimi-coding": "kimi-k3-coding",
        "kimi-k3-coding": "kimi-k3-coding",
        "moonshot": "kimi-k3-coding",
        "moonshot-ai": "kimi-k3-coding",
    }


def test_primary_five_hour_quota_mapping_is_explicit_and_excludes_openai():
    assert dashboard_module._PRIMARY_FIVE_HOUR_QUOTAS == {
        "anthropic": "five_hour",
        "opencode-go": "five_hour",
        "z-ai-glm": "five_hour",
        "kimi-k3-coding": "five_hour",
    }
    assert "openai-codex" not in dashboard_module._PRIMARY_FIVE_HOUR_QUOTAS


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("openai", "openai-codex"),
        ("codex", "openai-codex"),
        ("openai-codex", "openai-codex"),
        ("anthropic", "anthropic"),
        ("claude", "anthropic"),
        ("claude-code", "anthropic"),
        ("opencode-go", "opencode-go"),
        ("z.ai", "z-ai-glm"),
        ("z-ai", "z-ai-glm"),
        ("z-ai-glm", "z-ai-glm"),
        ("zai", "z-ai-glm"),
        ("zhipu", "z-ai-glm"),
        ("zhipuai", "z-ai-glm"),
        ("glm", "z-ai-glm"),
        ("kimi", "kimi-k3-coding"),
        ("kimi-code", "kimi-k3-coding"),
        ("kimi-coding", "kimi-k3-coding"),
        ("kimi-k3-coding", "kimi-k3-coding"),
        ("moonshot", "kimi-k3-coding"),
        ("moonshot-ai", "kimi-k3-coding"),
    ],
)
def test_subscription_provider_canonicalization_preserves_explicit_aliases(
    provider, expected
):
    assert dashboard_module._canonical_quota_provider(provider) == expected
    assert dashboard_module._canonical_quota_provider(
        f"  {provider.swapcase()}\t"
    ) == expected


@pytest.mark.parametrize(
    "provider",
    [
        "open.ai",
        "Open.AI",
        "open/ai",
        "openai codex",
        "openai_codex",
        "openaicodex",
        "claudecode",
        "opencodego",
        "zaiglm",
        "kimicode",
        "kimicoding",
        "kimik3coding",
        "moonshotai",
        "example.foo",
        "example-foo",
        "Example.Foo",
        "opencode-go-preview",
        "z-ai-next",
        "kimi-code-preview",
        "moonshot-enterprise",
    ],
)
def test_subscription_provider_canonicalization_preserves_unknown_ids_losslessly(provider):
    assert dashboard_module._canonical_quota_provider(f"  {provider}  ") == provider


def test_unknown_provider_punctuation_does_not_merge_jsonl_and_sqlite_payloads(
    tmp_path, monkeypatch
):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 5, 12, 30, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(
        dashboard_module,
        "_ledger_freshness",
        lambda *args, **kwargs: {
            "status": "fresh",
            "latest_at": "2026-08-05T11:15:00Z",
            "max_age_seconds": 10_800,
        },
    )
    rows = [
        _quota_observation(
            "private-source-dot",
            "week",
            observed_at="2026-08-05T11:15:00Z",
            provider="example.foo",
            account_ref="private-collision-account",
            used_value="10",
            remaining_value="90",
            limit_value="100",
        ),
        _quota_observation(
            "private-source-dash",
            "week",
            observed_at="2026-08-05T11:15:00Z",
            provider="example-foo",
            account_ref="private-collision-account",
            used_value="90",
            remaining_value="10",
            limit_value="100",
        ),
    ]
    jsonl = tmp_path / "quota.jsonl"
    sqlite = tmp_path / "quota.sqlite3"
    jsonl.write_text(
        "\n".join(canonical_json(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    append_sqlite_facts(
        sqlite,
        list(reversed(rows)),
        fact_type="quota_observation_v1",
    )

    payloads = []
    for ledger in (jsonl, sqlite):
        response = create_app(quota_ledger=str(ledger)).test_client().get(
            "/api/subscriptions?hours=1"
        )
        assert response.status_code == 200
        payloads.append(response.get_json())

    assert payloads[0] == payloads[1]
    payload = payloads[0]
    assert payload["observations"] == []
    assert len(payload["latest"]) == 2
    assert {
        row["provider"]: row["used_value"] for row in payload["latest"]
    } == {
        "example.foo": "10",
        "example-foo": "90",
    }

    chart = payload["time_series"]
    unknown_cards = {
        row["provider"]: row
        for row in chart["providers"]
        if row["provider"] in {"example.foo", "example-foo"}
    }
    assert list(unknown_cards) == ["example-foo", "example.foo"]
    assert len(unknown_cards) == 2
    for provider, expected_pct in (("example.foo", 10.0), ("example-foo", 90.0)):
        card = unknown_cards[provider]
        assert card["label"] == provider
        assert card["raw_provider_ids"] == [provider]
        assert card["observation_count"] == 1
        assert len(card["series"]) == 1
        assert card["series"][0]["provider"] == provider
        assert card["series"][0]["raw_provider_ids"] == [provider]
        assert card["series"][0]["values"] == [expected_pct]

    unified = chart["unified_weekly"]
    assert unified["series"] == []
    assert {row["provider"] for row in unified["no_data_providers"]} == {
        "openai-codex",
        "anthropic",
        "opencode-go",
        "z-ai-glm",
        "kimi-k3-coding",
    }
    assert "example.foo" not in json.dumps(unified)
    assert "example-foo" not in json.dumps(unified)

    serialized = json.dumps(payload)
    for private_value in (
        "private-source-dot",
        "private-source-dash",
        "private-collision-account",
        "private:test-quota",
        "/private/provider/payload",
        "source_namespace",
        "source_observation_id",
        "account_ref",
        "provider_payload_ref",
    ):
        assert private_value not in serialized


def test_subscription_current_partial_hour_is_latest_but_not_charted(tmp_path, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 5, 12, 34, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)
    rows = [
        _quota_observation(
            "complete-hour",
            "week",
            observed_at="2026-08-05T11:45:00Z",
            used_value="10",
            remaining_value="90",
            limit_value="100",
        ),
        _quota_observation(
            "current-hour",
            "week",
            observed_at="2026-08-05T12:05:00Z",
            used_value="90",
            remaining_value="10",
            limit_value="100",
        ),
    ]
    ledger = tmp_path / "quota.jsonl"
    ledger.write_text("\n".join(canonical_json(row) for row in rows) + "\n", encoding="utf-8")

    payload = create_app(quota_ledger=str(ledger)).test_client().get(
        "/api/subscriptions?hours=2"
    ).get_json()

    assert payload["latest"][0]["used_value"] == "90"
    week = payload["time_series"]["providers"][0]["series"][0]
    assert week["values"] == [None, 10.0]
    assert "90.0" not in json.dumps(payload["time_series"])


def test_rfc3339_order_keys_preserve_arbitrary_fraction_and_fail_closed():
    generated_at = datetime(2026, 8, 5, 12, 34, 56, 123_456, tzinfo=timezone.utc)
    generated_at_key = dashboard_module._datetime_rfc3339_order_key(generated_at)
    past_key = dashboard_module._rfc3339_order_key("2026-08-05T12:34:56.1234559Z")
    future_key = dashboard_module._rfc3339_order_key("2026-08-05T12:34:56.1234561Z")

    assert generated_at_key is not None
    assert past_key is not None
    assert future_key is not None
    assert generated_at_key == dashboard_module._rfc3339_order_key(
        "2026-08-05T12:34:56.123456Z"
    )
    assert past_key < generated_at_key < future_key
    assert dashboard_module._rfc3339_order_key(
        "2026-08-05T14:34:56.1234561+02:00"
    ) == future_key

    assert dashboard_module._rfc3339_order_key("not-a-timestamp") is None
    assert dashboard_module._rfc3339_order_key("0001-01-01T00:00:00+23:00") is None
    assert dashboard_module._rfc3339_order_key("9999-12-31T23:59:59-23:00") is None
    assert dashboard_module._datetime_rfc3339_order_key(datetime(2026, 8, 5)) is None


def test_subscription_exact_request_time_is_latest_without_admitting_future_facts(
    tmp_path, monkeypatch
):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 5, 12, 34, 56, 123_456, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(
        dashboard_module,
        "_ledger_freshness",
        lambda *args, **kwargs: {
            "status": "fresh",
            "latest_at": "2026-08-05T12:34:56Z",
            "max_age_seconds": 10_800,
        },
    )
    rows = [
        _quota_observation(
            "private-complete-hour",
            "week",
            observed_at="2026-08-05T11:45:00Z",
            used_value="10",
            remaining_value="90",
            limit_value="100",
            account_ref="private-boundary-account",
        ),
        _quota_observation(
            "private-submicrosecond-past",
            "week",
            observed_at="2026-08-05T12:34:56.1234559Z",
            used_value="60",
            remaining_value="40",
            limit_value="100",
            account_ref="private-boundary-account",
        ),
        _quota_observation(
            "private-exact-now",
            "week",
            observed_at="2026-08-05T12:34:56.123456Z",
            used_value="70",
            remaining_value="30",
            limit_value="100",
            account_ref="private-boundary-account",
        ),
        _quota_observation(
            "private-submicrosecond-future",
            "week",
            observed_at="2026-08-05T12:34:56.1234561Z",
            used_value="99",
            remaining_value="1",
            limit_value="100",
            account_ref="private-boundary-account",
            provider_payload_ref="/private/future-provider-payload",
            x_payload_ref="/private/future-extension-payload",
        ),
    ]
    jsonl = tmp_path / "quota.jsonl"
    sqlite = tmp_path / "quota.sqlite3"
    jsonl.write_text("\n".join(canonical_json(row) for row in rows) + "\n", encoding="utf-8")
    append_sqlite_facts(sqlite, rows, fact_type="quota_observation_v1")

    payloads = []
    for ledger in (jsonl, sqlite):
        response = create_app(quota_ledger=str(ledger)).test_client().get(
            "/api/subscriptions?hours=1"
        )
        assert response.status_code == 200
        payloads.append(response.get_json())

    assert payloads[0] == payloads[1]
    payload = payloads[0]
    assert payload["observations"] == []
    assert len(payload["latest"]) == 1
    assert payload["latest"][0]["observed_at"] == "2026-08-05T12:34:56.123456Z"
    assert payload["latest"][0]["used_value"] == "70"

    provider = payload["time_series"]["providers"][0]
    assert provider["provider"] == "openai-codex"
    assert provider["observation_count"] == 1
    assert provider["series"][0]["values"] == [10.0]
    assert provider["series"][0]["latest_observed_at"] == "2026-08-05T11:45:00Z"

    serialized = json.dumps(payload)
    assert "2026-08-05T12:34:56.1234561Z" not in serialized
    for private_value in (
        "private-boundary-account",
        "private-complete-hour",
        "private-submicrosecond-past",
        "private-exact-now",
        "private-submicrosecond-future",
        "/private/future-provider-payload",
        "/private/future-extension-payload",
        "source_namespace",
        "source_observation_id",
        "account_ref",
        "provider_payload_ref",
        "x_payload_ref",
    ):
        assert private_value not in serialized


def test_subscription_series_identity_and_equal_timestamp_ties_have_jsonl_sqlite_parity(
    tmp_path, monkeypatch
):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 5, 12, 30, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)
    identities = [
        _quota_observation(
            "identity-a",
            "week",
            observed_at="2026-08-05T11:10:00Z",
            harness="alpha-harness",
            account_ref="private-account-a",
            used_value="10",
            limit_value="100",
        ),
        _quota_observation(
            "identity-b",
            "week",
            observed_at="2026-08-05T11:10:00Z",
            harness="beta-harness",
            account_ref="private-account-b",
            used_value="80",
            limit_value="100",
        ),
    ]
    tied = [
        _quota_observation(
            "tie-a",
            "month",
            observed_at="2026-08-05T11:20:00.123456789Z",
            used_value="15",
            limit_value="100",
        ),
        _quota_observation(
            "tie-z",
            "month",
            observed_at="2026-08-05T11:20:00.123456789Z",
            used_value="95",
            limit_value="100",
        ),
    ]
    jsonl = tmp_path / "quota.jsonl"
    sqlite = tmp_path / "quota.sqlite3"
    jsonl.write_text(
        "\n".join(canonical_json(row) for row in [*identities, *reversed(tied)]) + "\n",
        encoding="utf-8",
    )
    append_sqlite_facts(
        sqlite,
        [*reversed(identities), *tied],
        fact_type="quota_observation_v1",
    )

    payloads = []
    for ledger in (jsonl, sqlite):
        response = create_app(quota_ledger=str(ledger)).test_client().get(
            "/api/subscriptions?hours=1"
        )
        assert response.status_code == 200
        payloads.append(response.get_json())

    assert payloads[0]["time_series"] == payloads[1]["time_series"]
    assert payloads[0]["latest"] == payloads[1]["latest"]
    openai = payloads[0]["time_series"]["providers"][0]
    week_series = [row for row in openai["series"] if row["quota_name"] == "week"]
    assert len(week_series) == 2
    assert [row["values"] for row in week_series] == [[10.0], [80.0]]
    assert [row["label"] for row in week_series] == [
        "week · alpha-harness · account 1",
        "week · beta-harness · account 2",
    ]
    month = next(row for row in openai["series"] if row["quota_name"] == "month")
    assert month["values"] == [95.0]
    assert next(row for row in payloads[0]["latest"] if row["quota_name"] == "month")[
        "used_value"
    ] == "95"
    unified_week = payloads[0]["time_series"]["unified_weekly"]
    openai_week = [
        row for row in unified_week["series"] if row["provider"] == "openai-codex"
    ]
    assert [row["label"] for row in openai_week] == [
        "OpenAI / Codex · account 1",
        "OpenAI / Codex · account 2",
    ]
    assert [row["values"] for row in openai_week] == [[10.0], [80.0]]
    assert [row["provider_account_index"] for row in openai_week] == [1, 2]
    assert all(row["provider_account_count"] == 3 for row in openai_week)
    assert month["provider_account_index"] == 3
    assert month["provider_account_count"] == 3
    assert not any(
        row["provider"] == "openai-codex"
        for row in unified_week["no_data_providers"]
    )

    serialized = json.dumps(payloads[0]["time_series"])
    for private_value in (
        "private-account-a",
        "private-account-b",
        "identity-a",
        "identity-b",
        "tie-a",
        "tie-z",
        "source_namespace",
        "source_observation_id",
        "account_ref",
    ):
        assert private_value not in serialized


def test_subscription_null_and_literal_default_accounts_remain_distinct_across_backends(
    tmp_path, monkeypatch
):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 5, 12, 30, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)
    rows = [
        _quota_observation(
            "private-null-account-source",
            "week",
            observed_at="2026-08-05T11:15:00Z",
            account_ref=None,
            used_value="10",
            remaining_value="90",
            limit_value="100",
        ),
        _quota_observation(
            "private-literal-default-source",
            "week",
            observed_at="2026-08-05T11:15:00Z",
            account_ref="default",
            used_value="90",
            remaining_value="10",
            limit_value="100",
        ),
    ]
    jsonl = tmp_path / "quota.jsonl"
    sqlite = tmp_path / "quota.sqlite3"
    jsonl.write_text(
        "\n".join(canonical_json(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    append_sqlite_facts(
        sqlite,
        list(reversed(rows)),
        fact_type="quota_observation_v1",
    )

    payloads = []
    for ledger in (jsonl, sqlite):
        response = create_app(quota_ledger=str(ledger)).test_client().get(
            "/api/subscriptions?hours=1"
        )
        assert response.status_code == 200
        payloads.append(response.get_json())

    assert payloads[0] == payloads[1]
    payload = payloads[0]
    openai = payload["time_series"]["providers"][0]
    assert openai["provider"] == "openai-codex"
    assert [series["label"] for series in openai["series"]] == [
        "week · hermes · account 1",
        "week · hermes · account 2",
    ]
    assert [series["values"] for series in openai["series"]] == [[10.0], [90.0]]

    assert len(payload["latest"]) == 2
    assert {row["used_value"] for row in payload["latest"]} == {"10", "90"}
    assert all("account_ref" not in row for row in payload["latest"])

    unified = payload["time_series"]["unified_weekly"]
    openai_unified = [
        series for series in unified["series"] if series["provider"] == "openai-codex"
    ]
    assert [series["label"] for series in openai_unified] == [
        "OpenAI / Codex · account 1",
        "OpenAI / Codex · account 2",
    ]
    assert [series["values"] for series in openai_unified] == [[10.0], [90.0]]
    assert not any(
        row["provider"] == "openai-codex" for row in unified["no_data_providers"]
    )

    serialized = json.dumps(payload)
    for private_value in (
        "private-null-account-source",
        "private-literal-default-source",
        "private:test-quota",
        "/private/provider/payload",
        "default",
        "source_namespace",
        "source_observation_id",
        "account_ref",
        "provider_payload_ref",
    ):
        assert private_value not in serialized


def test_subscription_hourly_chart_uses_actual_last_samples_aliases_and_safe_ratios(
    tmp_path, monkeypatch
):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 5, 12, 34, 56, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)

    def observation(source_id, quota_name, observed_at, provider, **updates):
        values = {
            "limit_value": "100",
            "remaining_value": "90",
            "used_value": "10",
            "measurement_confidence": "exact",
        }
        values.update(updates)
        return _quota_observation(
            source_id,
            quota_name,
            observed_at=observed_at,
            provider=provider,
            **values,
        )

    rows = [
        observation("openai-first", "week", "2026-08-05T09:05:00Z", "openai"),
        observation(
            "codex-last",
            "week",
            "2026-08-05T09:55:00Z",
            "codex",
            used_value="20",
            remaining_value="80",
            measurement_confidence="estimated",
        ),
        observation(
            "valid-before-impossible",
            "week",
            "2026-08-05T10:10:00Z",
            "openai-codex",
            used_value="30",
            remaining_value="70",
        ),
        observation(
            "impossible-last",
            "week",
            "2026-08-05T10:50:00Z",
            "openai-codex",
            used_value="120",
            remaining_value="0",
        ),
        observation(
            "rounded",
            "week",
            "2026-08-05T11:15:00Z",
            "openai-codex",
            harness="codex-cli",
            limit_value="3",
            used_value="1",
            remaining_value="2",
        ),
        observation(
            "partial-hour-excluded",
            "week",
            "2026-08-05T12:01:00Z",
            "openai",
            used_value="99",
            remaining_value="1",
        ),
        observation(
            "missing-ratio",
            "five_hour",
            "2026-08-05T10:20:00Z",
            "openai",
            used_value=None,
            remaining_value=None,
        ),
        observation(
            "claude-week",
            "seven_day",
            "2026-08-05T10:25:00Z",
            "claude",
            used_value="55",
            remaining_value="45",
        ),
        observation(
            "go-month",
            "month",
            "2026-08-05T10:30:00Z",
            "opencode-go",
            used_value="40",
            remaining_value="60",
        ),
        observation(
            "go-week-missing",
            "week",
            "2026-08-05T10:35:00Z",
            "opencode-go",
            used_value=None,
            remaining_value=None,
        ),
        observation(
            "z-week",
            "week",
            "2026-08-05T10:40:00Z",
            "z.ai",
            used_value="25",
            remaining_value="75",
        ),
        observation(
            "kimi-week",
            "week",
            "2026-08-05T11:20:00Z",
            "moonshot-ai",
            limit_value="4",
            used_value="1",
            remaining_value="3",
        ),
        observation(
            "balance-only",
            "account_balance",
            "2026-08-05T11:30:00Z",
            "deepseek",
            unit="usd",
            limit_value=None,
            used_value=None,
            remaining_value="12.50",
            account_ref="private-chart-account",
            provider_payload_ref="/private/chart-payload",
            x_payload_ref="/private/extension-payload",
        ),
        observation(
            "additional-provider",
            "week",
            "2026-08-05T11:40:00Z",
            "example-provider",
            limit_value="8",
            used_value="2",
            remaining_value="6",
        ),
    ]
    jsonl = tmp_path / "quota.jsonl"
    sqlite = tmp_path / "quota.sqlite3"
    jsonl.write_text("\n".join(canonical_json(row) for row in rows) + "\n", encoding="utf-8")
    append_sqlite_facts(sqlite, rows, fact_type="quota_observation_v1")

    payloads = []
    for ledger in (jsonl, sqlite):
        response = create_app(quota_ledger=str(ledger)).test_client().get(
            "/api/subscriptions?hours=3&history=0"
        )
        assert response.status_code == 200
        payloads.append(response.get_json())
    assert payloads[0]["time_series"] == payloads[1]["time_series"]

    payload = payloads[0]
    assert payload["observations"] == []
    chart = payload["time_series"]
    assert chart["bucket_starts"] == [
        "2026-08-05T09:00:00Z",
        "2026-08-05T10:00:00Z",
        "2026-08-05T11:00:00Z",
    ]
    assert chart["window_end"] == "2026-08-05T12:00:00Z"
    assert chart["bucket_count"] == 3

    providers = {row["provider"]: row for row in chart["providers"]}
    assert list(providers)[:5] == [
        "openai-codex",
        "anthropic",
        "opencode-go",
        "z-ai-glm",
        "kimi-k3-coding",
    ]
    openai = providers["openai-codex"]
    assert openai["raw_provider_ids"] == ["codex", "openai", "openai-codex"]
    assert [series["quota_name"] for series in openai["series"]] == ["week", "week"]
    assert [series["label"] for series in openai["series"]] == [
        "week · codex-cli · account 1",
        "week · hermes · account 1",
    ]
    codex_week, hermes_week = openai["series"]
    assert codex_week["values"] == [None, None, 33.3333]
    assert codex_week["sample_count"] == 1
    assert codex_week["confidence_status"] == "exact"
    assert codex_week["harness"] == "codex-cli"
    assert hermes_week["values"] == [20.0, None, None]
    assert hermes_week["sample_count"] == 1
    assert hermes_week["confidence_status"] == "estimated"
    assert {row["quota_name"] for row in openai["unavailable_series"]} == {"five_hour"}
    five_hour = openai["unavailable_series"][0]
    assert five_hour == {
        "confidence_status": "exact",
        "harness": "hermes",
        "label": "five hour",
        "measurement_confidence": "exact",
        "observation_count": 1,
        "provider_account_count": 1,
        "provider_account_index": 1,
        "quota_name": "five_hour",
        "reason": "no_comparable_utilization",
        "sample_count": 0,
        "unit": "percent",
    }

    assert providers["deepseek"]["status"] == "no_comparable_utilization"
    assert providers["deepseek"]["series"] == []
    assert providers["deepseek"]["unavailable_series"][0]["quota_name"] == "account_balance"
    assert [series["quota_name"] for series in providers["opencode-go"]["series"]] == [
        "month"
    ]
    assert providers["opencode-go"]["unavailable_series"][0]["quota_name"] == "week"

    unified = chart["unified_weekly"]
    assert unified["label"] == "Unified weekly subscription utilization"
    assert [row["provider"] for row in unified["series"]] == [
        "openai-codex",
        "openai-codex",
        "anthropic",
        "z-ai-glm",
        "kimi-k3-coding",
    ]
    assert [row["quota_name"] for row in unified["series"]] == [
        "week",
        "week",
        "seven_day",
        "week",
        "week",
    ]
    assert [row["label"] for row in unified["series"][:2]] == [
        "OpenAI / Codex",
        "OpenAI / Codex",
    ]
    assert [row["provider_account_index"] for row in unified["series"][:2]] == [1, 1]
    assert all(row["provider_account_count"] == 1 for row in unified["series"][:2])
    assert unified["no_data_providers"] == [
        {
            "provider": "opencode-go",
            "label": "OpenCode Go",
            "quota_name": "week",
            "status": "no_comparable_weekly_utilization",
        },
    ]

    serialized = json.dumps(chart)
    for private_value in (
        "private-chart-account",
        "/private/chart-payload",
        "/private/extension-payload",
        "source_namespace",
        "source_observation_id",
        "account_ref",
        "provider_payload_ref",
    ):
        assert private_value not in serialized


def test_subscription_api_unifies_five_hour_quotas_without_substitution_or_account_pooling(
    tmp_path, monkeypatch
):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 5, 12, 30, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)

    def observation(
        source_id, provider, quota_name, used, *, account_ref: str | None = "account"
    ):
        return _quota_observation(
            source_id,
            quota_name,
            observed_at="2026-08-05T11:15:00Z",
            provider=provider,
            account_ref=account_ref,
            used_value=str(used),
            remaining_value=str(100 - used),
            limit_value="100",
            measurement_confidence="exact",
        )

    rows = [
        observation("codex-week", "openai-codex", "week", 91),
        observation("codex-spark-week", "openai-codex", "spark_week", 13),
        observation("claude-primary-five", "anthropic", "five_hour", 11, account_ref=None),
        observation("claude-primary-week", "anthropic", "seven_day", 22, account_ref=None),
        observation(
            "claude-secondary-five",
            "anthropic",
            "five_hour",
            61,
            account_ref="private-secondary-account",
        ),
        observation(
            "claude-secondary-week",
            "anthropic",
            "seven_day",
            72,
            account_ref="private-secondary-account",
        ),
        observation("go-five", "opencode-go", "five_hour", 33),
        observation("go-week", "opencode-go", "week", 44),
        observation("glm-five", "z-ai-glm", "five_hour", 55),
        observation("glm-week", "z-ai-glm", "week", 66),
        observation("kimi-five", "kimi-k3-coding", "five_hour", 77),
        observation("kimi-week", "kimi-k3-coding", "week", 88),
    ]
    ledger = tmp_path / "quota.jsonl"
    ledger.write_text(
        "\n".join(canonical_json(row) for row in reversed(rows)) + "\n",
        encoding="utf-8",
    )

    response = create_app(quota_ledger=str(ledger)).test_client().get(
        "/api/subscriptions?hours=1&history=0"
    )

    assert response.status_code == 200
    payload = response.get_json()
    five_hour = payload["time_series"]["unified_five_hour"]
    assert five_hour["label"] == "Unified five-hour subscription utilization"
    assert "weekly data is never substituted" in five_hour["description"]
    assert [row["provider"] for row in five_hour["series"]] == [
        "anthropic",
        "anthropic",
        "opencode-go",
        "z-ai-glm",
        "kimi-k3-coding",
    ]
    assert [row["quota_name"] for row in five_hour["series"]] == ["five_hour"] * 5
    assert [row["label"] for row in five_hour["series"]] == [
        "Anthropic / Claude · account 1",
        "Anthropic / Claude · account 2",
        "OpenCode Go",
        "Z.AI / GLM",
        "Kimi K3 Coding",
    ]
    assert [row["values"] for row in five_hour["series"]] == [
        [11.0],
        [61.0],
        [33.0],
        [55.0],
        [77.0],
    ]
    assert [row["provider_account_index"] for row in five_hour["series"][:2]] == [1, 2]
    assert all(row["provider_account_count"] == 2 for row in five_hour["series"][:2])
    assert [72.0] not in [row["values"] for row in five_hour["series"]]
    assert [11.0 + 61.0] not in [row["values"] for row in five_hour["series"]]
    assert five_hour["no_data_providers"] == [
        {
            "provider": "openai-codex",
            "label": "OpenAI / Codex",
            "quota_name": None,
            "status": "comparable_five_hour_quota_not_reported",
        }
    ]

    weekly = payload["time_series"]["unified_weekly"]
    assert [row["provider"] for row in weekly["series"]] == [
        "openai-codex",
        "anthropic",
        "anthropic",
        "opencode-go",
        "z-ai-glm",
        "kimi-k3-coding",
    ]
    assert [row["values"] for row in weekly["series"]] == [
        [91.0],
        [22.0],
        [72.0],
        [44.0],
        [66.0],
        [88.0],
    ]

    serialized = json.dumps(payload)
    for private_value in (
        "private-secondary-account",
        "private:test-quota",
        "source_namespace",
        "source_observation_id",
        "account_ref",
        "provider_payload_ref",
    ):
        assert private_value not in serialized


def test_unified_five_hour_handles_one_series_and_missing_or_invalid_primary_data():
    rows = [
        _quota_observation(
            "single-go-five",
            "five_hour",
            provider="opencode-go",
            observed_at="2026-08-05T11:15:00Z",
            used_value="25",
            remaining_value="75",
            limit_value="100",
        ),
        _quota_observation(
            "invalid-claude-five",
            "five_hour",
            provider="anthropic",
            observed_at="2026-08-05T11:20:00Z",
            used_value=None,
            remaining_value=None,
            limit_value="100",
        ),
    ]

    result = dashboard_module._build_subscription_time_series(
        rows,
        window_start=datetime(2026, 8, 5, 11, tzinfo=timezone.utc),
        window_end=datetime(2026, 8, 5, 12, tzinfo=timezone.utc),
        generated_at=datetime(2026, 8, 5, 12, 30, tzinfo=timezone.utc),
    )["unified_five_hour"]

    assert [row["label"] for row in result["series"]] == ["OpenCode Go"]
    assert result["series"][0]["values"] == [25.0]
    assert result["no_data_providers"] == [
        {
            "provider": "openai-codex",
            "label": "OpenAI / Codex",
            "quota_name": None,
            "status": "comparable_five_hour_quota_not_reported",
        },
        {
            "provider": "anthropic",
            "label": "Anthropic / Claude",
            "quota_name": "five_hour",
            "status": "no_comparable_five_hour_utilization",
        },
        {
            "provider": "z-ai-glm",
            "label": "Z.AI / GLM",
            "quota_name": "five_hour",
            "status": "primary_five_hour_quota_not_reported",
        },
        {
            "provider": "kimi-k3-coding",
            "label": "Kimi K3 Coding",
            "quota_name": "five_hour",
            "status": "primary_five_hour_quota_not_reported",
        },
    ]


@pytest.mark.parametrize(
    ("used", "limit"),
    [
        ("-1", "100"),
        ("NaN", "100"),
        ("Infinity", "100"),
        ("1", "0"),
        ("101", "100"),
        (None, "100"),
        ("1", None),
    ],
)
def test_subscription_chart_ratio_normalization_fails_closed(used, limit):
    assert dashboard_module._quota_utilization_pct(
        {"used_value": used, "limit_value": limit, "unit": "percent"}
    ) is None


def test_subscription_chart_cardinality_caps_are_deterministic_and_report_fidelity():
    rows = []
    for provider_index in range(30):
        for quota_index in range(20):
            rows.append(
                _quota_observation(
                    f"source-{provider_index:02d}-{quota_index:02d}",
                    f"quota_{quota_index:02d}",
                    provider=f"extra-{provider_index:02d}",
                    observed_at="2026-08-05T11:15:00Z",
                    used_value=str(quota_index + 1),
                    limit_value="100",
                )
            )
    kwargs = {
        "window_start": datetime(2026, 8, 5, 11, tzinfo=timezone.utc),
        "window_end": datetime(2026, 8, 5, 12, tzinfo=timezone.utc),
        "generated_at": datetime(2026, 8, 5, 12, 30, tzinfo=timezone.utc),
    }

    forward = dashboard_module._build_subscription_time_series(rows, **kwargs)
    reverse = dashboard_module._build_subscription_time_series(list(reversed(rows)), **kwargs)

    assert forward == reverse
    assert len(forward["providers"]) == 32
    assert [provider["provider"] for provider in forward["providers"][:5]] == [
        "openai-codex",
        "anthropic",
        "opencode-go",
        "z-ai-glm",
        "kimi-k3-coding",
    ]
    assert [provider["provider"] for provider in forward["providers"][5:]] == [
        f"extra-{index:02d}" for index in range(27)
    ]
    assert all(
        len(provider["series"]) + len(provider["unavailable_series"]) <= 16
        for provider in forward["providers"]
    )
    assert forward["truncation"] == {
        "provider_limit": 32,
        "providers_included": 32,
        "providers_omitted": 3,
        "providers_total": 35,
        "providers_truncated": True,
        "series_included": 432,
        "series_limit_per_provider": 16,
        "series_omitted": 168,
        "series_total": 600,
        "series_truncated": True,
    }


def test_extreme_quota_timestamps_fail_safely_in_parser_and_api(tmp_path):
    minimum = datetime.min.replace(tzinfo=timezone.utc)
    assert dashboard_module._fact_datetime("0001-01-01T00:00:00+23:00") == minimum
    assert dashboard_module._normalize_timestamp("9999-12-31T23:59:59-23:00") == 0.0

    ledger = tmp_path / "extreme.jsonl"
    ledger.write_text(
        json.dumps(
            _quota_observation(
                "extreme",
                "week",
                observed_at="0001-01-01T00:00:00+23:00",
                used_value="10",
                limit_value="100",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    response = create_app(quota_ledger=str(ledger)).test_client().get(
        "/api/subscriptions?hours=1"
    )

    assert response.status_code == 503
    assert response.is_json
    assert response.get_json() == {"error": "configured private ledger is unavailable"}


def test_subscription_quota_chart_ticks_are_responsive(tmp_path, monkeypatch):
    html = _dashboard_root_html(tmp_path, monkeypatch)
    options = re.search(
        r"function subscriptionQuotaChartOptions\(\) \{(?P<body>.*?)^      \}",
        html,
        re.DOTALL | re.MULTILINE,
    )

    assert options is not None
    assert (
        'maxTicksLimit: window.matchMedia("(max-width: 520px)").matches ? 3 : 9,'
        in options.group("body")
    )


def test_unified_subscription_provider_palette_is_stable_contrasting_and_scoped(
    tmp_path, monkeypatch
):
    # Governed-cognition refactor (kintsugi, 2026-08-29): the literal palette
    # died — providers pin fixed slots of the declared ai-usage.series-rank
    # set instead. Stability = fixed provider->slot map; contrast/distinctness
    # are the set law enforced per-theme in governed-cognition (pairwise
    # distinct members); scoping is unchanged.
    html = _dashboard_root_html(tmp_path, monkeypatch)
    palette_match = re.search(
        r"const SUBSCRIPTION_UNIFIED_PROVIDER_RANK = Object\.freeze\(\{"
        r"(?P<slots>.*?)^      \}\);",
        html,
        re.DOTALL | re.MULTILINE,
    )

    assert palette_match is not None
    slots = json.loads(
        "{" + re.sub(r",\s*$", "", palette_match.group("slots")) + "}"
    )
    assert sorted(slots) == [
        "anthropic",
        "kimi-k3-coding",
        "openai-codex",
        "opencode-go",
        "z-ai-glm",
    ]
    assert all(isinstance(slot, int) and 0 <= slot < 10 for slot in slots.values())
    assert len(set(slots.values())) == len(slots), "provider slots must stay distinct"

    # the style layer is token-pure: --bg resolves through the GC token layer
    background_match = re.search(r"--bg:\s*([^;]+);", html)
    assert background_match is not None
    assert background_match.group(1).strip() == "var(--gc-bg)"

    # Datasets build from chartRows — the chart-window slice (polish round
    # item 11: the five-hour canvas plots the trailing 48 buckets) — while the
    # tables keep rendering from the unsliced rows.
    assert "subscriptionQuotaDatasets(chartRows)" in html
    assert "chartBucketLimit: 48" in html
    assert "const color = subscriptionUnifiedProviderColor(row, index);" in html
    assert "UNIFIED_RANK_COLORS[rank]" in html
    assert "subscriptionUnifiedQuotaDash(row)" in html


def test_subscription_quota_history_html_is_accessible_bounded_and_confidence_explicit(
    tmp_path, monkeypatch
):
    html = _dashboard_root_html(tmp_path, monkeypatch)

    history = html.split('<section class="subscription-quota-history"', 1)[1].split(
        '<div id="subscriptionProviderGrid"', 1
    )[0]
    assert len(re.findall(r"<canvas\b", history)) == 2
    assert "Unified weekly subscription utilization" in history
    assert "Unified five-hour subscription utilization" in history
    assert "normalized 0–100% scale" in history
    # Caption diet (2026-08-30 polish, item 7): the tokens-vs-dollars doctrine
    # moved from the twin chart captions to the Subscriptions section header,
    # and the no-substitution rule is carried by the dynamic
    # missingQuotaMessage summary rather than restated in the caption.
    assert "does not compare tokens or dollars" not in history
    assert "never tokens or dollars" in html
    assert "weekly data is not substituted" in html
    for prefix, period in (
        ("subscriptionUnifiedWeekly", "weekly"),
        ("subscriptionUnifiedFiveHour", "five-hour"),
    ):
        assert f'id="{prefix}Chart"' in history
        assert (
            f'aria-label="Unified {period} subscription quota utilization from 0 to 100 percent"'
            in history
        )
        assert (
            f'role="img" aria-describedby="{prefix}Description {prefix}Summary"'
            in history
        )
        assert f'id="{prefix}Description"' in history
        assert f'id="{prefix}Labels"' in history
        assert f'id="{prefix}TableHead"' in history
        assert f'id="{prefix}TableBody"' in history
    assert "Provider quota history" not in html
    assert "subscriptionProviderChartGrid" not in html
    assert "subscriptionProviderChart-" not in html
    assert "subscriptionProviderTableHead-" not in html
    assert "subscriptionProviderTableBody-" not in html
    assert "subscriptionProviderCharts" not in html
    assert "destroySubscriptionProviderCharts" not in html
    assert "renderSubscriptionProviderCharts" not in html
    assert "subscriptionProviderAccountCards" not in html
    assert ".subscription-provider-chart" not in html
    assert 'id="subscriptionProviderGrid"' in html
    # The raw observation ledger moved from <details> to the fold system
    # (keeper-ruled 2026-08-29: ledger tables collapsed by default).
    assert 'data-fold="subscriptionsRaw"' in html
    script = html.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    assert 'prefix: "subscriptionUnifiedWeekly"' in script
    assert 'prefix: "subscriptionUnifiedFiveHour"' in script
    assert "timeSeries.unified_weekly" in script
    assert "timeSeries.unified_five_hour" in script
    assert 'fetch("/api/subscriptions?hours=168&history=0", { signal: AbortSignal.timeout(30000) })' in html
    assert 'fetch("/api/subscriptions?days=30&history=0")' not in html
    assert "Historical contract only" not in html
    assert "No comparable primary weekly-equivalent data:" in html
    assert "OpenAI / Codex reports no comparable five-hour quota" in html
    assert "quotaConfidenceDash" in html
    assert 'status === "exact" ? [] : [7, 5]' in html
    assert "spanGaps: false" in html
    assert "confidence_status" in html
    assert ".destroy()" in html
    assert "animation: REDUCED_MOTION ? false" in html
    assert "min: 0" in html and "max: 100" in html
    assert "cdn.jsdelivr.net" not in html

    responsive_grid_rule = re.search(
        r"\.subscription-unified-history-grid\s*\{(?P<body>[^}]*)\}", html
    )
    assert responsive_grid_rule is not None
    assert re.search(
        r"grid-template-columns:\s*repeat\(auto-fit,\s*"
        r"minmax\(min\(100%,\s*520px\),\s*1fr\)\)\s*;",
        responsive_grid_rule.group("body"),
    )
    assert '<meta name="viewport" content="width=device-width, initial-scale=1.0" />' in html

    mobile_rule = re.search(
        r"@media \(max-width: 640px\).*?\.subscription-quota-chart-wrap\s*\{"
        r"[^}]*min-height:\s*220px\s*;",
        html,
        re.DOTALL,
    )
    assert mobile_rule is not None


def test_vendor_readme_records_bounded_bklit_design_provenance():
    provenance = Path("src/codex_usage_tracker/static/vendor/README.md").read_text(
        encoding="utf-8"
    )

    assert "c57f66bfa7c3198edb677b567ce08cbf364ae159" in provenance
    assert "2026-07-28" in provenance
    assert "line-chart" in provenance
    assert "live-line" in provenance
    assert "design reference only" in provenance.lower()
    assert "no runtime code or dependency" in provenance.lower()
    assert "Chart.js" in provenance
    assert "Shai-Hulud" in provenance
    assert "universal" in provenance.lower()


def _unified_usage_fact(source_event_id, **updates):
    row = {
        "fact_type": "usage_event_v1",
        "schema_version": 1,
        "source_namespace": "test:dashboard",
        "source_event_id": source_event_id,
        "harness": "hermes",
        "surface": None,
        "purpose": "main",
        "record_kind": "api_attempt",
        "occurred_at": "2026-07-12T12:00:00Z",
        "recorded_at": "2026-07-12T12:00:00Z",
        "session_id": None,
        "logical_call_id": None,
        "attempt_no": None,
        "provider_request_id": None,
        "upstream_provider": None,
        "provider": "anthropic",
        "model_requested": "claude",
        "model_reported": None,
        "api_mode": None,
        "billing_mode": None,
        "request_status": "ok",
        "error_class": None,
        "latency_ms": None,
        "input_tokens": 10,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 5,
        "reasoning_tokens": 0,
        "usage_source": "provider_reported",
        "usage_completeness": "complete",
        "measurement_confidence": "exact",
        "missing_fields": [],
        "attribution_gaps": [],
        "estimated_cost_usd": None,
        "actual_cost_usd": None,
        "cost_status": "included",
        "cost_source": None,
        "pricing_version": None,
        "reconstructed_call_count": None,
        "corrects_source_namespace": None,
        "corrects_source_event_id": None,
    }
    row.update(updates)
    return row


def test_api_data_defaults_to_a_bounded_row_limit(tmp_path, monkeypatch):
    ledger = tmp_path / "usage.jsonl"
    _write_usage_ledger(
        ledger,
        [
            {
                "fetched_at": (
                    datetime(2026, 6, 4, tzinfo=timezone.utc) + timedelta(hours=hour)
                ).isoformat(),
                "weekly_used_pct": float(hour % 100),
                "session_used_pct": None,
                "weekly_reset_at": 1781137839,
            }
            for hour in range(250)
        ],
    )
    monkeypatch.setenv("HERMES_STATE_DB_PATH", str(tmp_path / "missing.db"))
    client = create_app(atrium_root=str(tmp_path / "atrium"), ledger=str(ledger)).test_client()

    default_rows = client.get("/api/data").get_json()
    assert len(default_rows) == 200
    # Newest first, so the bound keeps the most recent samples.
    assert default_rows[0]["fetched_at"] == "2026-06-14T09:00:00+00:00"

    assert len(client.get("/api/data?limit=10").get_json()) == 10
    assert len(client.get("/api/data?limit=0").get_json()) == 250
    assert len(client.get("/api/data?limit=1000").get_json()) == 250

    bad_limit = client.get("/api/data?limit=nope")
    assert bad_limit.status_code == 400
    assert bad_limit.get_json()["error"] == "limit must be an integer"
    negative_limit = client.get("/api/data?limit=-1")
    assert negative_limit.status_code == 400
    assert negative_limit.get_json()["error"] == "limit must be nonnegative"


def test_ledger_rows_are_parsed_once_until_the_file_changes(tmp_path, monkeypatch):
    ledger = tmp_path / "usage.jsonl"
    rows = [
        {"fetched_at": "2026-06-04T01:00:00+00:00", "weekly_used_pct": 72.0, "weekly_reset_at": 1781137839},
    ]
    _write_usage_ledger(ledger, rows)
    monkeypatch.setenv("HERMES_STATE_DB_PATH", str(tmp_path / "missing.db"))

    parses = []
    real_parse = dashboard_module._parse_rows

    def counting_parse(path):
        parses.append(str(path))
        return real_parse(path)

    monkeypatch.setattr(dashboard_module, "_parse_rows", counting_parse)
    client = create_app(atrium_root=str(tmp_path / "atrium"), ledger=str(ledger)).test_client()

    assert len(client.get("/api/data").get_json()) == 1
    assert client.get("/api/summary").get_json()["total_rows"] == 1
    assert client.get("/api/trend").status_code == 200
    assert len(parses) == 1

    rows.append({"fetched_at": "2026-06-04T02:00:00+00:00", "weekly_used_pct": 74.0, "weekly_reset_at": 1781137839})
    _write_usage_ledger(ledger, rows)

    assert client.get("/api/summary").get_json()["total_rows"] == 2
    assert len(parses) == 2


def test_unified_usage_totals_separate_priced_and_unpriced_events(tmp_path):
    unified = tmp_path / "unified.jsonl"
    unified.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                _unified_usage_fact(
                    "priced-estimate", cost_status="estimated", estimated_cost_usd="0.25"
                ),
                _unified_usage_fact("priced-actual", cost_status="actual", actual_cost_usd="1.50"),
                _unified_usage_fact("subscription-included-1"),
                _unified_usage_fact("subscription-included-2"),
                _unified_usage_fact("subscription-included-3"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    client = create_app(
        atrium_root=str(tmp_path),
        ledger=str(tmp_path / "codex.jsonl"),
        unified_usage_ledger=str(unified),
    ).test_client()

    totals = client.get("/api/unified-usage?days=36500").get_json()["totals"]
    assert totals["priced_events"] == 2
    assert totals["unpriced_events"] == 3
    assert totals["estimated_cost_usd"] == "0.25"
    assert totals["actual_cost_usd"] == "1.5"


def test_dashboard_page_retires_session_surfaces_and_bounds_its_data_fetch(tmp_path, monkeypatch):
    ledger = tmp_path / "usage.jsonl"
    _write_usage_ledger(
        ledger,
        [{"fetched_at": "2026-06-04T02:00:00+00:00", "weekly_used_pct": 74.0, "weekly_reset_at": 1781137839}],
    )
    monkeypatch.setenv("HERMES_STATE_DB_PATH", str(tmp_path / "missing.db"))
    client = create_app(atrium_root=str(tmp_path / "atrium"), ledger=str(ledger)).test_client()

    page = client.get("/")
    assert page.status_code == 200
    markup = page.data.decode("utf-8")
    # The retired-window explainer card and the spark surfaces were removed
    # outright (keeper ruling 2026-08-30) — the retirement stays total.
    assert "Session window retired upstream" not in markup
    for retired in (
        'id="sessionChart"', 'id="sessionBarChart"', 'id="currentSession"', 'id="sessionPeak"',
        'id="sparkChart"', 'id="sparkSession"', 'id="sparkWeekly"', "Spark usage",
    ):
        assert retired not in markup
    # The Codex detail section followed (keeper-ruled 2026-08-30): the page
    # no longer fetches the codex JSONL data endpoint at all.
    assert 'fetch("/api/data' not in markup


class TestProviderStatusFeeds:
    """Keeper-ruled 2026-08-30: API liveness comes from the provider's own
    status API; the ledger-observed cells are the secondary line."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self, monkeypatch):
        from codex_usage_tracker import dashboard as dash

        monkeypatch.setattr(dash, "_provider_status_cache", {})
        yield

    def test_snapshot_projects_feeds_and_marks_unreachable(self, monkeypatch):
        from codex_usage_tracker import dashboard as dash

        def fake_fetch(url, timeout=6.0):
            if "claude" in url:
                return {
                    "status": {"indicator": "minor", "description": "Elevated errors"},
                    "incidents": [{"name": "API latency"}],
                }
            raise OSError("boom")

        monkeypatch.setattr(dash, "_fetch_status_feed", fake_fetch)
        snapshot = dash._provider_status_snapshot(1000.0)
        anthropic = snapshot["anthropic"]
        assert anthropic["indicator"] == "minor"
        assert anthropic["incidents"] == ["API latency"]
        assert anthropic["unreachable"] is False
        # openai + openai-codex share one URL and one (failed) fetch
        assert snapshot["openai"]["unreachable"] is True
        assert snapshot["openai-codex"]["unreachable"] is True
        assert "unreachable" in snapshot["openai"]["description"]

    def test_snapshot_caches_per_url_within_ttl(self, monkeypatch):
        from codex_usage_tracker import dashboard as dash

        calls = []

        def fake_fetch(url, timeout=6.0):
            calls.append(url)
            return {"status": {"indicator": "none", "description": "ok"}, "incidents": []}

        monkeypatch.setattr(dash, "_fetch_status_feed", fake_fetch)
        dash._provider_status_snapshot(1000.0)
        first = len(calls)
        assert first == len(set(dash._PROVIDER_STATUS_FEEDS.values()))
        dash._provider_status_snapshot(1000.0 + 30)
        assert len(calls) == first  # inside TTL: no refetch
        dash._provider_status_snapshot(1000.0 + dash._PROVIDER_STATUS_TTL_SECONDS + 1)
        assert len(calls) == first * 2  # past TTL: one refetch per URL

    def test_endpoint_serves_providers_and_feedless_list(self, tmp_path, monkeypatch):
        from codex_usage_tracker import dashboard as dash

        monkeypatch.setattr(
            dash,
            "_fetch_status_feed",
            lambda url, timeout=6.0: {
                "status": {"indicator": "none", "description": "All Systems Operational"},
                "incidents": [],
            },
        )
        client = dash.create_app(ledger=str(tmp_path / "codex.jsonl")).test_client()
        payload = client.get("/api/provider-status").get_json()
        assert payload["providers"]["anthropic"]["indicator"] == "none"
        assert payload["providers"]["kimi-coding"]["feed_url"].startswith("https://status.moonshot.cn")
        # No public feed found for these (verified 2026-08-30) — honest, not inferred.
        assert payload["feedless"] == ["deepseek", "openrouter", "opencode-go", "zai"] or payload["feedless"] == sorted(["zai", "deepseek", "openrouter", "opencode-go"])


def test_page_and_apis_are_never_browser_cached(tmp_path):
    """Keeper report 2026-08-30: a heuristically-cached page hid two deploys.
    HTML and API responses carry no-store; vendored static may cache."""
    from codex_usage_tracker.dashboard import create_app

    client = create_app(ledger=str(tmp_path / "codex.jsonl")).test_client()
    assert client.get("/").headers["Cache-Control"] == "no-store"
    assert client.get("/api/summary").headers["Cache-Control"] == "no-store"
    # Flask stamps static with its own revalidation policy (no-cache +
    # ETag) — acceptable; the load-bearing claim is that it is NOT no-store.
    static = client.get("/static/vendor/chart.umd.min.js")
    assert static.headers.get("Cache-Control", "") != "no-store"
