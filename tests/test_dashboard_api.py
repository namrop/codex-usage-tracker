from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

import pytest

from codex_usage_tracker.canonical_ledger import append_sqlite_facts, canonical_json
import codex_usage_tracker.dashboard as dashboard_module
from codex_usage_tracker.dashboard import create_app

import frontend_surfaces


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


def _in_bucket(value, index, count=4):
    """A ``count``-bucket series (four 15-minute buckets per hour) holding
    ``value`` at ``index`` and ``None`` everywhere else."""
    values = [None] * count
    values[index] = value
    return values


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
    """React + Vite retarget: the served page is the built shell (still 200,
    still no-store); the subscription grid, the raw-observation fold, the
    ruled column order, and the record-class doctrine live in the component
    that renders that card."""
    html = _dashboard_root_html(tmp_path, monkeypatch)
    assert '<div id="root"></div>' in html

    card = frontend_surfaces.source("components/SubscriptionsCard.tsx")
    assert 'id="subscriptionProviderGrid"' in card
    # The raw observation ledger stays behind the fold system
    # (keeper-ruled 2026-08-29: ledger tables collapsed by default).
    assert 'foldKey="subscriptionsRaw"' in card
    assert 'id="subscriptionsBody"' in card
    assert "<th>Observed</th><th>Confidence</th>" in card
    providers = frontend_surfaces.source("lib/providers.ts")
    for display_name in (
        "OpenAI / Codex",
        "Anthropic / Claude",
        "OpenCode Go",
        "Z.AI / GLM",
        "Kimi K3 Coding",
    ):
        assert display_name in providers
    assert (
        "Usage events above, subscription quota observations here, and billing facts below "
        "are separate record classes."
    ) in card
    assert "No quota estimate is a billing fact." in card

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



def test_every_ledger_table_ships_collapsed_behind_an_accessible_fold():
    """Keeper-ruled 2026-08-29: the ledger tables and columns are collapsed by
    default. Fold is a class/attribute toggle, never a removal — the shared
    Fold component renders its body children unconditionally (full tbodies in
    the DOM while folded; nothing lazy-renders on expand), the collapsed
    default comes from unset localStorage prefs, and the aria wiring
    (aria-expanded / aria-controls / role=region) rides the fold key."""
    joined = frontend_surfaces.frontend_text()
    for key in FOLD_KEYS:
        assert f'foldKey="{key}"' in joined, f"{key}: no fold usage"

    fold = frontend_surfaces.source("components/Fold.tsx")
    assert 'data-open={open ? "true" : "false"}' in fold
    assert "aria-expanded={open}" in fold
    assert 'aria-controls={`${foldKey}FoldBody`}' in fold
    assert 'id={`${foldKey}FoldToggle`}' in fold
    assert (
        'className="fold-body" id={`${foldKey}FoldBody`} role="region" '
        'aria-labelledby={`${foldKey}FoldToggle`}'
    ) in fold
    # Toggle, never removal: the body renders children unconditionally.
    assert "{children}" in fold
    assert "open &&" not in fold, "fold body must not conditionally render on open"
    assert '<button\n            type="button"' in fold or 'type="button"' in fold

    state = frontend_surfaces.source("hooks/useFoldState.ts")
    assert "expandAllRequested() || readFoldPrefs(FOLD_OPEN_KEY)[foldKey] === true" in state


def test_wide_ledger_tables_default_to_the_essential_column_tier():
    """Wide ledger tables default to the essential column tier; column
    collapse is visibility (CSS display:none on the tier class), never DOM
    removal, lifted by a real aria-pressed toggle button."""
    tiered_sources = {
        "harnessUsage": ("components/UnifiedUsageCard.tsx", "tier-harness"),
        "providerModelUsage": ("components/UnifiedUsageCard.tsx", "tier-provider-model"),
        "subscriptionsRaw": ("components/SubscriptionsCard.tsx", "tier-quota"),
        "billingLedger": ("components/BillingCard.tsx", "tier-billing"),
    }
    assert set(tiered_sources) == {key for key, _tier in COLUMN_TIERED_FOLDS}
    for key, (source_path, tier_class) in tiered_sources.items():
        component = frontend_surfaces.source(source_path)
        assert re.search(
            rf'<Fold[^>]*foldKey="{key}"[^>]*colsToggle', component, re.DOTALL
        ), f"{key}: fold is not column-tiered"
        assert f'className="{tier_class}"' in component, (
            f"{key}: table missing its column-tier class"
        )

    fold = frontend_surfaces.source("components/Fold.tsx")
    assert 'data-cols={colsToggle ? (allCols ? "all" : "essential") : undefined}' in fold
    assert "aria-pressed={allCols}" in fold
    assert "All columns" in fold
    assert "data-fold-cols" in fold

    # Column collapse is visibility, never DOM removal: the hidden columns
    # are still declared in the shipped markup.
    assert re.search(
        r"\.fold\[data-cols=.essential.\][^{]*\{\s*display: none;",
        frontend_surfaces.app_css(),
    )


def test_fold_state_is_remembered_and_lives_outside_the_rendered_tbodies():
    """Fold state lives in localStorage under the stable keys plus component
    state — never inside the rendered tbodies, so the 60s hydrate cannot
    reopen or re-collapse anything. Unset preference means collapsed /
    essential, never remembered-open. ?expand=all is a one-load debug/print
    affordance that writes nothing back. Summaries re-derive from the same
    data the tables render from on every hydrate."""
    state = frontend_surfaces.source("hooks/useFoldState.ts")
    assert 'FOLD_OPEN_KEY = "aiUsage.fold.open"' in state
    assert 'FOLD_COLS_KEY = "aiUsage.fold.cols"' in state
    assert "expandAllRequested() || readFoldPrefs(FOLD_OPEN_KEY)[foldKey] === true" in state
    assert "readFoldPrefs(FOLD_COLS_KEY)[foldKey] === true" in state
    assert 'has("expand")' in state
    # writeFoldPref appears exactly three times: its definition and the two
    # user toggles — the expand affordance and initial reads never write.
    assert state.count("writeFoldPref(") == 3

    # Summaries are derived from the data in hand (tableFoldSummary) and
    # passed down as a prop — no DOM scraping, no state in the tbodies.
    fold = frontend_surfaces.source("components/Fold.tsx")
    assert "export function tableFoldSummary" in fold
    assert "summary: string" in fold


def test_codex_detail_section_is_removed_entirely():
    """Keeper-ruled 2026-08-30: the Codex detail compatibility section is gone
    outright — hero, trend fold, correlation charts, token correlation ledger,
    and raw ledger rows, with all of their JS. The weekly codex quota signal
    survives in the subscriptions cards and the reset rail; banked resets and
    plan are accepted losses. The server endpoints those views consumed stay
    (other consumers may exist); the frontend just never fetches them. The
    scan covers every frontend source AND the built app chunk."""
    surface = frontend_surfaces.frontend_text() + "\n" + frontend_surfaces.built_app_js()

    assert "Codex detail" not in surface
    assert "Compatibility views" not in surface
    assert "section-label" not in surface
    # The hero and its monitor tiles.
    for retired in (
        'id="globalStatus"', 'id="planType"', 'id="currentWeekly"', 'id="resetCredits"',
        'id="freshness"', "Codex usage monitor", "Limits, resets, and headroom.",
        "Banked resets",
    ):
        assert retired not in surface
    # The trend fold, correlation charts, and both ledger tables.
    for retired in (
        "codexUsageTrend", 'id="weekChart"', 'id="tokenUsageChart"',
        'id="tokenBucketChart"', 'id="tokenDeltaChart"', 'id="cacheHitChart"',
        "Usage over time", "Usage × tokens over time",
        "Quota movement", "Token correlation ledger", "Ledger rows",
        'data-fold="tokenLedger"', 'data-fold="rawLedger"', "tier-token-ledger",
        "detail-grid",
    ):
        assert retired not in surface
    # The dead JS went with the markup — render paths, sorters, and the
    # codex-only chart builders.
    for retired_js in (
        "renderHeader", "renderCharts()", "renderTokenLedger", "renderSortedTable",
        "buildOrUpdateWeekChart", "buildOrUpdateTokenUsageChart",
        "buildOrUpdateTokenBucketChart", "buildOrUpdateTokenDeltaChart",
        "buildOrUpdateCacheHitChart", "thresholdPlugin", "endpointLabelPlugin",
        "state.trend", "state.tokenChart", "state.tokenLedger", "state.sortBy",
    ):
        assert retired_js not in surface
    # The frontend no longer fetches the codex JSONL endpoints at all.
    for retired_fetch in (
        '"/api/summary"', '"/api/trend"', '"/api/data', '"/api/token-ledger"',
        '"/api/token-chart"',
    ):
        assert retired_fetch not in surface
    # The refresh button survives the section it used to live in, and the
    # refresh cycle drives the unified surfaces.
    assert 'id="refreshUsage"' in surface
    assert "onClick={onRefresh}" in frontend_surfaces.source("components/UnifiedUsageCard.tsx")
    hook = frontend_surfaces.source("hooks/useDashboardData.ts")
    assert "if (!document.hidden && booted.current) hydrate();" in hook
    assert 'document.addEventListener("visibilitychange", onVisibility);' in hook

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



def test_estate_wide_token_composition_and_cache_hit_charts_exist():
    """Keeper-ruled 2026-08-30: the codex-only Token bucket mix and Cache hit
    rate charts are replaced by estate-wide charts in the unified section —
    stacked hourly token composition from bucket_series, and per-provider
    cache-hit lines from the provider_status_series token sums."""
    card = frontend_surfaces.source("components/UnifiedUsageCard.tsx")

    # Token composition: stacked bars, SERIES bucket colors, chart+fold table.
    assert "Token composition" in card
    assert 'canvasId="tokenCompositionChart"' in card
    assert 'labelsId="tokenCompositionLabels"' in card
    assert "bucket_series" in card
    assert "TOKEN_COMPOSITION_BUCKETS" in card
    for bucket_label in ('"Input"', '"Cache read"', '"Cache write"', '"Output"', '"Reasoning"'):
        assert bucket_label in card
    assert 'stack: "composition"' in card

    # Cache hit rate: per-provider lines in provider colors, 0-100 percent,
    # gaps where a provider had no prompt tokens, window-average legend.
    assert "Cache hit rate" in card
    assert 'canvasId="cacheHitRateChart"' in card
    assert 'labelsId="cacheHitRateLabels"' in card
    assert "function cacheHitRateModel" in card
    assert "spanGaps: false" in card
    assert "prompt > 0 ? (cache / prompt) * 100 : null" in card
    assert "windowAverage" in card
    # Provider identity rides the same rank-slot mapping every other unified
    # surface uses (status strip, donuts, cache chart).
    joined = frontend_surfaces.frontend_text()
    assert joined.count(
        "subscriptionUnifiedProviderColor(palette, { provider: row.provider_canonical }, index)"
    ) >= 3
    # The status strip's low-traffic folding rule applies to the chart too.
    assert 'row.provider !== "unknown" && Number(row.total_events) >= 10' in card
    assert "Low-traffic fold:" in card

    # Both new charts sit in the unified section, after the model token chart
    # and before the harness fold.
    model_at = card.index('canvasId="modelTokenChart"')
    composition_at = card.index('canvasId="tokenCompositionChart"')
    cache_at = card.index('canvasId="cacheHitRateChart"')
    harness_at = card.index('foldKey="harnessUsage"')
    assert model_at < composition_at < cache_at < harness_at


def test_captions_are_single_sentence_with_doctrine_at_section_level():
    """Keeper polish round 2026-08-30, item 7: card captions say what the
    surface shows in one sentence; shared doctrine lives once at section
    level (scanned across every frontend source)."""
    joined = frontend_surfaces.frontend_text()

    # The pool/slices doctrine appears exactly once, on the unified section
    # header, not per-donut.
    assert joined.count("never shares of one pool") == 1
    assert joined.count("never drawn as slices") == 1
    # The tokens-vs-dollars doctrine appears exactly once, on the
    # Subscriptions header, not on each twin utilization chart.
    assert joined.count("never tokens or dollars") == 1
    assert "this does not compare tokens or dollars" not in joined
    # The UTC policy note lives once at section level. Its bucket-width
    # noun is derived from the API's bucket_minutes (15-minute buckets,
    # keeper-ruled 2026-09-02), so the pin is the fixed clause.
    assert joined.count("(countdowns run in wall-clock time)") == 1


def test_subscription_meters_never_draw_a_lying_bar():
    """Keeper polish round 2026-08-30, items 8-10: lifetime/unknown windows and
    used>limit rows render balance-style (no used/limit bar), null-sided
    usd/credits quotas render as balances with a role accent, and kimi's
    provider_unit limit-100 scale displays as a percentage."""
    script = frontend_surfaces.source("components/SubscriptionsCard.tsx")

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
    css = frontend_surfaces.app_css()
    assert ".subscription-quota-value.role-stability" in css
    assert ".subscription-quota-value.role-violation" in css
    # Item 10: provider_unit with limit 100 displays as %, provenance in the
    # tooltip, canonical unit untouched server-side.
    assert 'rawUnit === "provider_unit" && limitNumber === 100' in script
    assert "normalized 0–100 scale reported by the provider" in script


def test_stale_notices_name_the_failing_lane():
    """A bare "stale" badge sent an operator to the whole pipeline when one
    source was the problem: staleLaneSuffix names the lane everywhere a stale
    notice renders — both panel notices and the hero digest mirror (polish
    round 2026-08-30, item 2)."""
    freshness_lib = frontend_surfaces.source("lib/freshness.ts")
    assert "export function staleLaneSuffix(freshness" in freshness_lib
    assert "no successful run on record" in freshness_lib

    hook = frontend_surfaces.source("hooks/useDashboardData.ts")
    # Both panel stale notices carry the suffix.
    assert hook.count("staleLaneSuffix(freshness)") == 2
    for notice in (
        "Canonical usage is stale",
        "Subscription observations are stale",
    ):
        assert notice in hook
    # The hero ops digest mirrors freshness (and the lane naming) even when
    # the panel below falls back to a stale/empty notice.
    hero = frontend_surfaces.source("components/OpsHero.tsx")
    assert "staleLaneSuffix(freshness)" in hero
    assert "freshness={state.freshness}" in frontend_surfaces.source("App.tsx")


def test_subscription_provider_js_preserves_authority_and_freshness():
    """The subscription cards preserve provider authority and freshness
    semantics: confidence labels, coverage/provider-state provenance, the
    observed_at freshness threshold chain, and grouping. Rendering stays
    injection-safe (React text nodes; no raw-HTML paths). A stale ledger
    keeps rendering observations — the stale branch only sets the notice."""
    script = frontend_surfaces.source("components/SubscriptionsCard.tsx")

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
    # React renders text nodes by construction; the raw-HTML escape hatches
    # stay banned (the .innerHTML ban, translated).
    joined = frontend_surfaces.frontend_text()
    assert ".innerHTML" not in joined
    assert "dangerouslySetInnerHTML" not in joined

    hook = frontend_surfaces.source("hooks/useDashboardData.ts")
    subs_loader = hook.split("async function loadSubscriptions", 1)[1].split(
        "async function loadBilling", 1
    )[0]
    stale_branch = subs_loader.split('if (freshness.status === "stale")', 1)[1].split(
        'if (freshness.status === "empty")', 1
    )[0]
    assert "return" not in stale_branch


def test_subscription_freshness_prefers_threshold_then_api_max_age_then_default():
    script = frontend_surfaces.source("components/SubscriptionsCard.tsx")

    threshold_position = script.index("freshness.threshold_seconds")
    max_age_position = script.index("freshness.max_age_seconds")
    default_position = script.index("21600", max_age_position)
    assert threshold_position < max_age_position < default_position
    assert "Number.isFinite" in script[threshold_position:default_position]


def test_subscription_groups_seed_expected_providers_and_keep_unknown_extras():
    """The provider grid seeds every expected provider (so an empty ledger
    still shows the estate), keeps unknown extras, and the frontend alias
    map stays byte-identical to the server's canonical alias map."""
    card = frontend_surfaces.source("components/SubscriptionsCard.tsx")
    providers_src = frontend_surfaces.source("lib/providers.ts")

    seed_statement = "PROVIDER_ORDER.forEach((provider) => ensureProviderGroup(provider));"
    assert seed_statement in card
    assert card.index(seed_statement) < card.index("rows.forEach((row) =>")
    assert "const aliasKey = rawProvider.toLowerCase();" in providers_src
    assert (
        "Object.prototype.hasOwnProperty.call(PROVIDER_ALIASES, aliasKey)"
        in providers_src
    )
    assert "PROVIDER_ALIASES[normalized]" not in providers_src
    assert "PROVIDER_ALIASES[compact]" not in providers_src
    assert "const compact" not in providers_src
    assert '[".", "_", "/", " "]' not in providers_src
    alias_declaration = re.search(
        r"PROVIDER_ALIASES[^=]*= Object\.freeze\((?P<aliases>\{.*?\})\);",
        providers_src,
        re.DOTALL,
    )
    assert alias_declaration is not None
    frontend_aliases = json.loads(
        re.sub(r",(\s*})$", r"\1", alias_declaration.group("aliases"))
    )
    assert frontend_aliases == dashboard_module._QUOTA_PROVIDER_ALIASES
    assert "return group.key;" in card
    assert "Unknown freshness" in card
    assert "No quota windows available for this provider." in card
    assert "providerGroups.size" not in card


def test_subscription_provider_cards_are_mobile_contained():
    css = frontend_surfaces.app_css()

    card_rule = re.search(r"\.subscription-provider-card\s*\{(?P<body>[^}]*)\}", css)
    assert card_rule is not None
    declarations = card_rule.group("body")
    assert re.search(r"min-width:\s*0\s*;", declarations)
    assert re.search(r"max-width:\s*100%\s*;", declarations)
    assert re.search(r"overflow:\s*hidden\s*;", declarations)
    assert re.search(
        r"@media \(max-width: 640px\).*?\.subscription-provider-grid\s*\{"
        r"[^}]*grid-template-columns:\s*1fr\s*;",
        css,
        re.DOTALL,
    )


def test_subscription_unavailable_path_renders_expected_provider_placeholders():
    """A total subscriptions failure renders the expected-provider
    placeholder cards as VISIBLE content under a "Subscription data
    unavailable." notice — and placeholders are never mistaken for real
    data: last-good gating keys on a real payload, never on hidden-ness."""
    card = frontend_surfaces.source("components/SubscriptionsCard.tsx")
    hook = frontend_surfaces.source("hooks/useDashboardData.ts")
    providers_src = frontend_surfaces.source("lib/providers.ts")

    assert "PlaceholderProviderCard" in card
    for display_name in (
        "OpenAI / Codex",
        "Anthropic / Claude",
        "OpenCode Go",
        "Z.AI / GLM",
        "Kimi K3 Coding",
    ):
        assert display_name in providers_src
    assert '"Unknown freshness"' in card
    assert "No quota windows available for this provider." in card
    # Placeholder mode is visible content with the notice shown.
    assert "const contentVisible = !subs.loading && (Boolean(data) || subs.placeholder);" in card

    subs_loader = hook.split("async function loadSubscriptions", 1)[1].split(
        "async function loadBurn", 1
    )[0]
    catch_branch = subs_loader.split("} catch (error) {", 1)[1]
    assert '"Subscription data unavailable."' in catch_branch
    assert "placeholder: true" in catch_branch
    # Placeholder mode carries no data, so it can never read as a render.
    assert "data: null" in catch_branch
    # "showing last good data" is gated on a real payload, never on
    # content visibility.
    assert "prev.subs.data" in catch_branch
    assert '"Refresh failed — showing last good data."' in catch_branch
    assert "contentVisible" not in catch_branch

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
            return cls(2026, 8, 5, 12, 5, tzinfo=tz)

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
            return cls(2026, 8, 5, 12, 5, tzinfo=tz)

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
        assert card["series"][0]["values"] == _in_bucket(expected_pct, 1)

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


def test_subscription_current_partial_bucket_is_latest_but_not_charted(tmp_path, monkeypatch):
    """At 12:34 the chart ends on the 12:30 boundary (15-minute buckets,
    keeper-ruled 2026-09-02): a 12:20 observation is the last complete
    bucket, a 12:32 observation is ``latest`` but never charted."""
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 5, 12, 34, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDateTime)
    rows = [
        _quota_observation(
            "complete-bucket",
            "week",
            observed_at="2026-08-05T12:20:00Z",
            used_value="10",
            remaining_value="90",
            limit_value="100",
        ),
        _quota_observation(
            "current-bucket",
            "week",
            observed_at="2026-08-05T12:32:00Z",
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
    chart = payload["time_series"]
    assert chart["window_start"] == "2026-08-05T10:30:00Z"
    assert chart["window_end"] == "2026-08-05T12:30:00Z"
    week = chart["providers"][0]["series"][0]
    assert week["values"] == _in_bucket(10.0, 7, count=8)
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
    assert provider["series"][0]["values"] == _in_bucket(10.0, 1)
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
            return cls(2026, 8, 5, 12, 5, tzinfo=tz)

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
    assert [row["values"] for row in week_series] == [
        _in_bucket(10.0, 0), _in_bucket(80.0, 0),
    ]
    assert [row["label"] for row in week_series] == [
        "week · alpha-harness · account 1",
        "week · beta-harness · account 2",
    ]
    month = next(row for row in openai["series"] if row["quota_name"] == "month")
    assert month["values"] == _in_bucket(95.0, 1)
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
    assert [row["values"] for row in openai_week] == [
        _in_bucket(10.0, 0), _in_bucket(80.0, 0),
    ]
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
            return cls(2026, 8, 5, 12, 5, tzinfo=tz)

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
    assert [series["values"] for series in openai["series"]] == [
        _in_bucket(10.0, 1), _in_bucket(90.0, 1),
    ]

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
    assert [series["values"] for series in openai_unified] == [
        _in_bucket(10.0, 1), _in_bucket(90.0, 1),
    ]
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


def test_subscription_chart_uses_actual_last_samples_aliases_and_safe_ratios(
    tmp_path, monkeypatch
):
    """Within one 15-minute bucket the last observation wins (09:05 then
    09:10; 10:10 then the impossible 10:12), so the tie rule is exercised on
    the bucket grid the charts actually draw."""
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 5, 12, 4, 56, tzinfo=tz)

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
            "2026-08-05T09:10:00Z",
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
            "2026-08-05T10:12:00Z",
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
            "impossible-ratio",
            "five_hour",
            "2026-08-05T10:20:00Z",
            "openai",
            used_value="120",
            remaining_value="0",
        ),
        # A historical all-null observation (no used, no remaining, no
        # provider_state, no coverage) is not chart input: it is neither an
        # unavailable series nor counted, the same rule ``latest`` applies.
        observation(
            "retired-null-window",
            "session",
            "2026-08-05T10:21:00Z",
            "openai",
            used_value=None,
            remaining_value=None,
            limit_value=None,
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
            "go-week-impossible",
            "week",
            "2026-08-05T10:35:00Z",
            "opencode-go",
            used_value="120",
            remaining_value="0",
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
        f"2026-08-05T{hour:02d}:{minute:02d}:00Z"
        for hour in (9, 10, 11)
        for minute in (0, 15, 30, 45)
    ]
    assert chart["window_end"] == "2026-08-05T12:00:00Z"
    assert chart["bucket_minutes"] == 15
    assert chart["bucket_count"] == 12

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
    assert codex_week["values"] == _in_bucket(33.3333, 9, count=12)
    assert codex_week["sample_count"] == 1
    assert codex_week["confidence_status"] == "exact"
    assert codex_week["harness"] == "codex-cli"
    # 09:00 bucket: the 09:10 sample wins over 09:05; 10:00 bucket: the
    # impossible 10:12 sample wins over the valid 10:10 one and reads None.
    assert hermes_week["values"] == _in_bucket(20.0, 0, count=12)
    assert hermes_week["sample_count"] == 1
    assert hermes_week["confidence_status"] == "estimated"
    assert {row["quota_name"] for row in openai["unavailable_series"]} == {"five_hour"}
    # Six openai-family rows are in-window and informative; the all-null
    # "session" row is neither counted nor projected.
    assert openai["observation_count"] == 6
    assert "session" not in json.dumps(openai)
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
            return cls(2026, 8, 5, 12, 5, tzinfo=tz)

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

    client = create_app(quota_ledger=str(ledger)).test_client()
    response = client.get(
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
        _in_bucket(value, 1) for value in (11.0, 61.0, 33.0, 55.0, 77.0)
    ]
    assert [row["provider_account_index"] for row in five_hour["series"][:2]] == [1, 2]
    assert all(row["provider_account_count"] == 2 for row in five_hour["series"][:2])
    assert _in_bucket(72.0, 1) not in [row["values"] for row in five_hour["series"]]
    assert _in_bucket(11.0 + 61.0, 1) not in [row["values"] for row in five_hour["series"]]
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
        _in_bucket(value, 1) for value in (91.0, 22.0, 72.0, 44.0, 66.0, 88.0)
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

    # Spark windows are retired from every dashboard surface (keeper ruling
    # 2026-09-06): the fixture's historical spark_week row stays in the
    # ledger but must never reach latest, history, the charts, or the burn
    # panel — a source-only cut would leave the last row newest forever.
    assert "spark_week" not in serialized
    assert "spark_five_hour" not in serialized
    burn = client.get("/api/quota-burn?hours=1")
    assert burn.status_code == 200
    assert "spark" not in json.dumps(burn.get_json())


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
        # Present but impossible (used > limit): reported, not comparable.
        _quota_observation(
            "invalid-claude-five",
            "five_hour",
            provider="anthropic",
            observed_at="2026-08-05T11:20:00Z",
            used_value="120",
            remaining_value="0",
            limit_value="100",
        ),
        # All-null: not reported at all as far as the chart is concerned.
        _quota_observation(
            "null-zai-five",
            "five_hour",
            provider="zai",
            observed_at="2026-08-05T11:25:00Z",
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
    assert result["series"][0]["values"] == _in_bucket(25.0, 1)
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



def test_subscription_quota_chart_ticks_are_responsive():
    """The quota chart's x ticks come from the shared viewport-aware
    limit, not a fixed count — 15-minute buckets put 672 points on the
    weekly chart, so the tick budget must follow the viewport."""
    chart = frontend_surfaces.source("lib/chart.ts")
    assert "export function xTickLimit(viewportWidth: number): number" in chart
    assert "maxTicksLimit: xTickLimit(viewportWidth)" in chart
    options_at = chart.index("export function subscriptionQuotaChartOptions")
    assert "ticks: xScaleTicks(SERIES, viewportWidth)" in chart[options_at:]


def test_unified_subscription_provider_palette_is_stable_contrasting_and_scoped():
    # Governed-cognition refactor (kintsugi, 2026-08-29): the literal palette
    # died — providers pin fixed slots of the declared ai-usage.series-rank
    # set instead. Stability = fixed provider->slot map; contrast/distinctness
    # are the set law enforced per-theme in governed-cognition (pairwise
    # distinct members); scoping is unchanged.
    providers_src = frontend_surfaces.source("lib/providers.ts")
    palette_match = re.search(
        r"SUBSCRIPTION_UNIFIED_PROVIDER_RANK[^=]*= Object\.freeze\((?P<slots>\{.*?\})\);",
        providers_src,
        re.DOTALL,
    )

    assert palette_match is not None
    slots = json.loads(
        re.sub(r",(\s*})$", r"\1", palette_match.group("slots"))
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
    assert "palette.rankColors[rank]" in providers_src

    # the style layer is token-pure: --bg resolves through the GC token layer
    background_match = re.search(r"--bg:\s*([^;]+);", frontend_surfaces.app_css())
    assert background_match is not None
    assert background_match.group(1).strip() == "var(--gc-bg)"

    # Datasets build from chartRows — the chart-window slice (polish round
    # item 11: the five-hour canvas plots the trailing 48 buckets) — while the
    # tables keep rendering from the unsliced rows.
    card = frontend_surfaces.source("components/SubscriptionsCard.tsx")
    assert "subscriptionQuotaDatasets(palette, chartRows, bucketMinutes)" in card
    assert "chartBucketLimit={48}" in card
    assert "const color = subscriptionUnifiedProviderColor(palette, row, index);" in card
    assert "borderDash: subscriptionUnifiedQuotaDash(row)" in card


def test_subscription_quota_history_html_is_accessible_bounded_and_confidence_explicit():
    """The unified quota history renders exactly two accessible chart cards
    (weekly + five-hour), normalized 0-100%, confidence-explicit labels and
    dashes, bounded fetches, and the retired per-provider small multiples
    stay dead — the invariants of the pre-React history section, held
    against the React sources and the entry documents."""
    card = frontend_surfaces.source("components/SubscriptionsCard.tsx")
    chart = frontend_surfaces.source("lib/chart.ts")
    quota_src = frontend_surfaces.source("lib/quota.ts")
    hook = frontend_surfaces.source("hooks/useDashboardData.ts")
    canvas = frontend_surfaces.source("components/ChartCanvas.tsx")
    joined = frontend_surfaces.frontend_text()

    # Exactly two chart cards in the history section.
    assert card.count("<SubscriptionHistoryCard") == 2
    assert "Unified weekly subscription utilization" in card
    assert "Unified five-hour subscription utilization" in card
    assert card.count("normalized 0–100% scale") == 2
    # Caption diet (2026-08-30 polish, item 7): the tokens-vs-dollars doctrine
    # lives on the Subscriptions section header, and the no-substitution rule
    # is carried by the dynamic missingQuotaMessage summary.
    assert "does not compare tokens or dollars" not in joined
    assert "never tokens or dollars" in card
    assert "weekly data is not substituted" in quota_src
    for wired in (
        'prefix="subscriptionUnifiedWeekly"',
        'prefix="subscriptionUnifiedFiveHour"',
        'canvasAriaLabel="Unified weekly subscription quota utilization from 0 to 100 percent"',
        'canvasAriaLabel="Unified five-hour subscription quota utilization from 0 to 100 percent"',
    ):
        assert wired in card
    # The id family and the described-by chain ride the prefix, so both
    # cards carry Chart/Description/Summary/Labels/TableHead/TableBody.
    assert 'role="img"' in card
    assert "ariaDescribedBy={`${prefix}Description ${prefix}Summary`}" in card
    for template_id in (
        "`${prefix}Chart`", "`${prefix}Description`", "`${prefix}Summary`",
        "`${prefix}Labels`", "`${prefix}TableHead`", "`${prefix}TableBody`",
    ):
        assert template_id in card

    # Retired per-provider small multiples stay dead.
    for retired in (
        "Provider quota history",
        "subscriptionProviderChartGrid",
        "subscriptionProviderChart-",
        "subscriptionProviderTableHead-",
        "subscriptionProviderTableBody-",
        "subscriptionProviderCharts",
        "destroySubscriptionProviderCharts",
        "renderSubscriptionProviderCharts",
        "subscriptionProviderAccountCards",
        ".subscription-provider-chart {",
    ):
        assert retired not in joined
    assert 'id="subscriptionProviderGrid"' in card
    # The raw observation ledger stays behind the fold system
    # (keeper-ruled 2026-08-29: ledger tables collapsed by default).
    assert 'foldKey="subscriptionsRaw"' in card
    assert "timeSeries.unified_weekly" in card
    assert "timeSeries.unified_five_hour" in card
    assert 'fetch("/api/subscriptions?hours=168&history=0", { signal: AbortSignal.timeout(30000) })' in hook
    assert 'fetch("/api/subscriptions?days=30&history=0")' not in joined
    assert "Historical contract only" not in joined
    assert "No comparable primary weekly-equivalent data:" in quota_src
    assert "OpenAI / Codex reports no comparable five-hour quota" in quota_src
    assert "quotaConfidenceDash" in quota_src
    assert 'status === "exact" ? [] : [7, 5]' in quota_src
    assert "spanGaps: false" in card
    assert "confidence_status" in card
    assert ".destroy()" in canvas
    assert "animation: REDUCED_MOTION ? false" in chart
    assert "min: 0" in chart and "max: 100" in chart
    assert "cdn.jsdelivr.net" not in joined
    assert "cdn.jsdelivr.net" not in frontend_surfaces.built_html()

    css = frontend_surfaces.app_css()
    responsive_grid_rule = re.search(
        r"\.subscription-unified-history-grid\s*\{(?P<body>[^}]*)\}", css
    )
    assert responsive_grid_rule is not None
    # The weekly and five-hour charts stack full-width (keeper-ruled
    # 2026-09-02); the auto-fit two-up grid is retired.
    assert re.search(
        r"grid-template-columns:\s*minmax\(0,\s*1fr\)\s*;",
        responsive_grid_rule.group("body"),
    )
    assert "auto-fit" not in responsive_grid_rule.group("body")
    viewport = '<meta name="viewport" content="width=device-width, initial-scale=1.0" />'
    assert viewport in frontend_surfaces.entry_html()
    assert viewport in frontend_surfaces.built_html()

    mobile_rule = re.search(
        r"@media \(max-width: 640px\).*?\.subscription-quota-chart-wrap\s*\{"
        r"[^}]*min-height:\s*220px\s*;",
        css,
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


# ── Backend fixes 2026-09-02 ─────────────────────────────────────────────


class _FrozenNoon(datetime):
    """2026-07-14T12:34:56Z; an hours window ends on the 12:30Z boundary."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 7, 14, 12, 34, 56, tzinfo=tz)


class _FrozenJustPastNoon(datetime):
    """2026-07-14T12:04:56Z, so an hours window ends at 12:00Z exactly."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 7, 14, 12, 4, 56, tzinfo=tz)


def _unified_client(tmp_path, rows):
    ledger = tmp_path / "unified.jsonl"
    ledger.write_text("\n".join(canonical_json(row) for row in rows) + "\n", encoding="utf-8")
    return create_app(
        atrium_root=str(tmp_path),
        ledger=str(tmp_path / "codex.jsonl"),
        unified_usage_ledger=str(ledger),
    ).test_client()


def _window_fact(source_id, occurred_at, provider, **updates):
    row = _unified_usage_fact(
        source_id,
        occurred_at=occurred_at,
        recorded_at=occurred_at,
        provider=provider,
        input_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
    )
    row.update(updates)
    return row


def _bucketed_client(tmp_path, usage_rows, quota_rows):
    """One app over both private ledgers, so the usage and quota charts are
    generated from the same frozen clock."""
    usage = tmp_path / "unified.jsonl"
    usage.write_text("\n".join(canonical_json(row) for row in usage_rows) + "\n", encoding="utf-8")
    quota = tmp_path / "quota.jsonl"
    quota.write_text("\n".join(canonical_json(row) for row in quota_rows) + "\n", encoding="utf-8")
    return create_app(
        atrium_root=str(tmp_path),
        ledger=str(tmp_path / "codex.jsonl"),
        unified_usage_ledger=str(usage),
        quota_ledger=str(quota),
    ).test_client()


def test_hours_window_ends_on_the_most_recent_complete_fifteen_minute_boundary(
    tmp_path, monkeypatch
):
    """15-minute buckets (keeper-ruled 2026-09-02: "15 minute buckets are
    better across all of the charts"). At 12:34:56Z an hours= window ends on
    12:30Z — the last complete quarter hour — not on 12:00Z, and hours=5 is
    twenty buckets from 07:30Z on both the usage and the quota surfaces."""
    monkeypatch.setattr(dashboard_module, "datetime", _FrozenNoon)
    assert dashboard_module._BUCKET_MINUTES == 15
    assert dashboard_module._BUCKET_SECONDS == 900
    client = _bucketed_client(tmp_path, [], [])

    usage = client.get("/api/unified-usage?hours=5").get_json()["time_series"]
    quota = client.get("/api/subscriptions?hours=5&history=0").get_json()["time_series"]
    for series in (usage, quota):
        assert series["window_end"] == "2026-07-14T12:30:00Z"
        assert series["window_start"] == "2026-07-14T07:30:00Z"
        assert series["bucket_minutes"] == 15
        assert series["bucket_count"] == 20
        assert series["bucket_starts"][0] == "2026-07-14T07:30:00Z"
        assert series["bucket_starts"][-1] == "2026-07-14T12:15:00Z"
        assert len(series["bucket_starts"]) == 20


def test_bucket_count_is_exactly_four_per_hour_for_every_hours_value(tmp_path, monkeypatch):
    """hours=168 is 672 buckets; every integer hours value yields hours * 4
    buckets on an unbroken 15-minute grid from window_start."""
    monkeypatch.setattr(dashboard_module, "datetime", _FrozenNoon)
    client = _bucketed_client(tmp_path, [], [])

    for hours in (1, 5, 24, 168):
        for path in (
            f"/api/unified-usage?hours={hours}",
            f"/api/subscriptions?hours={hours}&history=0",
        ):
            series = client.get(path).get_json()["time_series"]
            assert series["bucket_count"] == hours * 4, path
            assert len(series["bucket_starts"]) == hours * 4, path
            start = datetime(2026, 7, 14, 12, 30, tzinfo=timezone.utc) - timedelta(hours=hours)
            assert series["window_start"] == start.strftime("%Y-%m-%dT%H:%M:%SZ"), path
            assert series["bucket_starts"] == [
                (start + timedelta(minutes=15 * index)).strftime("%Y-%m-%dT%H:%M:%SZ")
                for index in range(hours * 4)
            ], path

    week = client.get("/api/unified-usage?hours=168").get_json()["time_series"]
    assert week["bucket_count"] == 672
    assert week["bucket_starts"][0] == "2026-07-07T12:30:00Z"
    assert week["bucket_starts"][-1] == "2026-07-14T12:15:00Z"


def test_rows_land_in_the_quarter_hour_bucket_that_contains_them(tmp_path, monkeypatch):
    """A row at HH:07 belongs to the HH:00 bucket and a row at HH:22 to the
    HH:15 bucket — on the model series, the token composition, the status
    strip and the quota series alike."""
    monkeypatch.setattr(dashboard_module, "datetime", _FrozenJustPastNoon)
    client = _bucketed_client(
        tmp_path,
        [
            _window_fact("at-07", "2026-07-14T11:07:00Z", "anthropic", input_tokens=7,
                         model_requested="claude", request_status="error", error_class="boom"),
            _window_fact("at-22", "2026-07-14T11:22:00Z", "anthropic", input_tokens=22,
                         model_requested="claude"),
        ],
        [
            _quota_observation("q-07", "week", observed_at="2026-07-14T11:07:00Z",
                               used_value="7", remaining_value="93", limit_value="100"),
            _quota_observation("q-22", "week", observed_at="2026-07-14T11:22:00Z",
                               used_value="22", remaining_value="78", limit_value="100"),
        ],
    )

    usage = client.get("/api/unified-usage?hours=1").get_json()["time_series"]
    assert usage["bucket_starts"] == [
        "2026-07-14T11:00:00Z", "2026-07-14T11:15:00Z",
        "2026-07-14T11:30:00Z", "2026-07-14T11:45:00Z",
    ]
    assert usage["model_series"][0]["values"] == [7, 22, 0, 0]
    assert usage["bucket_series"]["input_tokens"] == [7, 22, 0, 0]
    status = usage["provider_status_series"][0]
    assert status["provider"] == "anthropic"
    assert status["events"] == [1, 1, 0, 0]
    assert status["errors"] == [1, 0, 0, 0]
    assert status["top_error_classes"] == ["boom", None, None, None]
    assert status["input_tokens"] == [7, 22, 0, 0]

    quota = client.get("/api/subscriptions?hours=1&history=0").get_json()["time_series"]
    assert quota["bucket_starts"] == usage["bucket_starts"]
    week = quota["providers"][0]["series"][0]
    assert week["quota_name"] == "week"
    assert week["values"] == [7.0, 22.0, None, None]
    assert week["sample_count"] == 2


def test_provider_status_and_subscription_series_share_one_bucket_grid(tmp_path, monkeypatch):
    """The status strip, the usage charts and the quota charts are drawn side
    by side: same window, same bucket starts, and one instant lands at the
    same index on every series."""
    monkeypatch.setattr(dashboard_module, "datetime", _FrozenNoon)
    client = _bucketed_client(
        tmp_path,
        [
            _window_fact("usage", "2026-07-14T11:50:00Z", "anthropic", input_tokens=5,
                         model_requested="claude"),
        ],
        [
            _quota_observation("quota", "seven_day", observed_at="2026-07-14T11:50:00Z",
                               provider="anthropic", used_value="50", remaining_value="50",
                               limit_value="100"),
        ],
    )

    usage = client.get("/api/unified-usage?hours=3").get_json()["time_series"]
    quota = client.get("/api/subscriptions?hours=3&history=0").get_json()["time_series"]

    assert usage["bucket_starts"] == quota["bucket_starts"]
    assert usage["window_start"] == quota["window_start"] == "2026-07-14T09:30:00Z"
    assert usage["window_end"] == quota["window_end"] == "2026-07-14T12:30:00Z"
    assert usage["bucket_minutes"] == quota["bucket_minutes"] == 15
    assert usage["bucket_count"] == quota["bucket_count"] == 12

    # 11:50Z is the 11:45Z bucket: index 9 from a 09:30Z window start.
    assert usage["bucket_starts"][9] == "2026-07-14T11:45:00Z"
    status = {row["provider"]: row for row in usage["provider_status_series"]}
    assert status["anthropic"]["events"] == [0] * 9 + [1, 0, 0]
    assert usage["model_series"][0]["values"] == [0] * 9 + [5, 0, 0]
    assert all(
        len(row["events"]) == quota["bucket_count"]
        for row in usage["provider_status_series"]
    )
    providers = {row["provider"]: row for row in quota["providers"]}
    claude_week = providers["anthropic"]["series"][0]
    assert claude_week["values"] == _in_bucket(50.0, 9, count=12)
    unified_week = [row for row in quota["unified_weekly"]["series"] if row["provider"] == "anthropic"]
    assert [row["values"] for row in unified_week] == [_in_bucket(50.0, 9, count=12)]


def test_unified_usage_groups_providers_by_canonical_id_and_keeps_raw_ids(tmp_path, monkeypatch):
    """"openai" and "openai-codex" are one subscription: one donut arc, one
    status-strip row. The ledger view (by_provider_model) keeps the raw key
    but names the canonical one so the two tables can join."""
    monkeypatch.setattr(dashboard_module, "datetime", _FrozenJustPastNoon)
    client = _unified_client(
        tmp_path,
        [
            _window_fact("codex-raw", "2026-07-14T09:10:00Z", "openai", input_tokens=10,
                         cache_read_tokens=4, model_requested="gpt-5"),
            _window_fact("codex-canon", "2026-07-14T10:10:00Z", "openai-codex", input_tokens=20,
                         model_requested="gpt-5", request_status="error", error_class="rate_limit"),
            _window_fact("zai", "2026-07-14T10:20:00Z", "zai", output_tokens=3, model_requested="glm-5.3"),
            _window_fact("z-ai", "2026-07-14T11:20:00Z", "z-ai", output_tokens=5, model_requested="glm-5.3"),
            _window_fact("kimi", "2026-07-14T11:30:00Z", "kimi-coding", output_tokens=1, model_requested="k3"),
            _window_fact("claude", "2026-07-14T11:40:00Z", "anthropic", output_tokens=2, model_requested="claude"),
        ],
    )

    data = client.get("/api/unified-usage?hours=3").get_json()

    by_provider = {row["provider"]: row for row in data["by_provider"]}
    assert [row["provider"] for row in data["by_provider"]] == [
        "openai-codex", "z-ai-glm", "anthropic", "kimi-k3-coding",
    ]
    codex = by_provider["openai-codex"]
    assert codex["provider_canonical"] == "openai-codex"
    assert codex["provider_raw_ids"] == ["openai", "openai-codex"]
    assert codex["events"] == 2
    assert codex["total_tokens"] == 34 and codex["cached_tokens"] == 4 and codex["fresh_tokens"] == 30
    assert by_provider["z-ai-glm"]["provider_raw_ids"] == ["z-ai", "zai"]
    assert by_provider["z-ai-glm"]["total_tokens"] == 8
    assert by_provider["kimi-k3-coding"]["provider_raw_ids"] == ["kimi-coding"]
    assert all(row["provider"] == row["provider_canonical"] for row in data["by_provider"])

    # The ledger view stays keyed by the raw provider but can join to the
    # canonical rows.
    ledger_rows = {(row["provider"], row["model"]): row for row in data["by_provider_model"]}
    assert ledger_rows[("openai", "gpt-5")]["provider_canonical"] == "openai-codex"
    assert ledger_rows[("openai-codex", "gpt-5")]["provider_canonical"] == "openai-codex"
    assert ledger_rows[("zai", "glm-5.3")]["provider_canonical"] == "z-ai-glm"
    assert ledger_rows[("z-ai", "glm-5.3")]["provider_canonical"] == "z-ai-glm"

    status = {row["provider"]: row for row in data["time_series"]["provider_status_series"]}
    assert set(status) == {"openai-codex", "z-ai-glm", "anthropic", "kimi-k3-coding"}
    assert status["openai-codex"]["provider_canonical"] == "openai-codex"
    assert status["openai-codex"]["provider_raw_ids"] == ["openai", "openai-codex"]
    # Twelve 15-minute buckets from 09:00Z: 09:10 -> 0, 10:10 -> 4,
    # 10:20 -> 5, 11:20 -> 9.
    assert status["openai-codex"]["events"] == [1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0]
    assert status["openai-codex"]["errors"] == [0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0]
    assert status["openai-codex"]["input_tokens"] == [10, 0, 0, 0, 20, 0, 0, 0, 0, 0, 0, 0]
    assert status["openai-codex"]["cache_read_tokens"] == [4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    assert status["openai-codex"]["dominant_error_class"] == "rate_limit"
    assert status["z-ai-glm"]["events"] == [0, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0]
    assert status["z-ai-glm"]["provider_raw_ids"] == ["z-ai", "zai"]


def test_provider_status_feed_map_is_resolvable_by_canonical_key(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_module, "_provider_status_cache", {})
    monkeypatch.setattr(
        dashboard_module,
        "_fetch_status_feed",
        lambda url, timeout=6.0: {
            "status": {"indicator": "none", "description": "ok"},
            "incidents": [],
        },
    )

    snapshot = dashboard_module._provider_status_snapshot(1000.0)
    for provider, url in dashboard_module._PROVIDER_STATUS_FEEDS.items():
        canonical = dashboard_module._canonical_quota_provider(provider)
        # Raw keys survive; the canonical key resolves to the same feed.
        assert snapshot[provider]["feed_url"] == url
        assert snapshot[canonical]["feed_url"] == url
    assert snapshot["openai-codex"]["feed_url"] == snapshot["openai"]["feed_url"]
    assert snapshot["kimi-k3-coding"]["feed_url"].startswith("https://status.moonshot.cn")
    assert snapshot["anthropic"]["indicator"] == "none"
    # Nothing has a z-ai-glm feed, so the canonical key is honestly absent.
    assert "z-ai-glm" not in snapshot

    providers = create_app(ledger=str(tmp_path / "codex.jsonl")).test_client().get(
        "/api/provider-status"
    ).get_json()["providers"]
    assert {"openai", "openai-codex", "anthropic", "kimi-coding", "kimi-k3-coding"} <= set(providers)


def test_list_price_daily_buckets_by_utc_calendar_day_and_flags_partial_days(tmp_path, monkeypatch):
    """The UI labels each entry as a calendar date, so entries must BE calendar
    days: hours=168 ending 12:00Z spans eight UTC dates, the first and last
    only partly covered. Traffic at 13:00Z on 07-13 lands on 07-13, not on a
    12:00Z-anchored slice that straddles two dates."""
    monkeypatch.setattr(dashboard_module, "datetime", _FrozenJustPastNoon)
    client = _unified_client(
        tmp_path,
        [
            # First (partial) day: window starts 07-07T12:00Z.
            _window_fact("day0", "2026-07-07T13:00:00Z", "anthropic",
                         model_requested="claude-sonnet-5", model_reported="claude-sonnet-5",
                         input_tokens=1_000_000),
            # Before the window: same date, must not be priced.
            _window_fact("day0-early", "2026-07-07T11:00:00Z", "anthropic",
                         model_requested="claude-sonnet-5", model_reported="claude-sonnet-5",
                         input_tokens=1_000_000),
            # A full middle day, late in the evening — stays on 07-13.
            _window_fact("day6", "2026-07-13T23:30:00Z", "zai", model_requested="glm-5.3",
                         model_reported="glm-5.3", output_tokens=1_000_000),
            _window_fact("day6-unpriced", "2026-07-13T14:00:00Z", "mystery-lab",
                         model_requested="whoknows", input_tokens=1_000_000),
            # Last (partial) day: [07-14T00:00Z, 07-14T12:00Z).
            _window_fact("day7", "2026-07-14T01:00:00Z", "anthropic",
                         model_requested="claude-sonnet-5", model_reported="claude-sonnet-5",
                         input_tokens=1_000_000),
            # At series_end: not in the window at all.
            _window_fact("day7-late", "2026-07-14T12:00:00Z", "anthropic",
                         model_requested="claude-sonnet-5", model_reported="claude-sonnet-5",
                         input_tokens=1_000_000),
        ],
    )

    daily = client.get("/api/unified-usage?hours=168").get_json()["list_price_daily"]

    assert [entry["day_start"] for entry in daily] == [
        f"2026-07-{day:02d}T00:00:00Z" for day in range(7, 15)
    ]
    assert [entry["partial"] for entry in daily] == [True] + [False] * 6 + [True]
    assert daily[0]["estimate_usd"] == "2.00"
    assert daily[0]["priced_token_share"] == 1.0
    assert daily[0]["unpriced_providers"] == []
    assert [entry["estimate_usd"] for entry in daily[1:6]] == ["0.00"] * 5
    assert daily[6]["estimate_usd"] == "4.40"
    assert daily[6]["unpriced_providers"] == ["mystery-lab"]
    assert 0.4 < daily[6]["priced_token_share"] < 0.6
    assert daily[7]["estimate_usd"] == "2.00"
    for entry in daily:
        assert set(entry) >= {"day_start", "partial", "estimate_usd", "priced_token_share", "unpriced_providers"}

    # A window that crosses one midnight yields two partial days.
    class FrozenSmallHours(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 14, 2, 10, 0, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenSmallHours)
    daily = client.get("/api/unified-usage?hours=3").get_json()["list_price_daily"]
    assert [(entry["day_start"], entry["partial"]) for entry in daily] == [
        ("2026-07-13T00:00:00Z", True),
        ("2026-07-14T00:00:00Z", True),
    ]
    # Complete hours [23:00Z, 02:00Z): the 23:30 glm fact stays on 07-13,
    # the 01:00Z sonnet fact lands on 07-14 — each on its own calendar
    # date, no day straddling. (The 14:00Z mystery fact is out of window.)
    assert daily[0]["estimate_usd"] == "4.40"
    assert daily[0]["unpriced_providers"] == []
    assert daily[1]["estimate_usd"] == "2.00"


def _lane_health_payload(**last_success_at):
    return {
        "schema_version": 1,
        "scope": "usage",
        "updated_at": "2026-07-14T12:30:00Z",
        "lanes": {
            lane: {
                "last_run_at": "2026-07-14T12:30:00Z",
                "last_success_at": value,
                "last_error_class": None if value else "AuthError",
                "rows_written": 0 if value is None else 3,
                "quarantined_count": 0,
            }
            for lane, value in last_success_at.items()
        },
    }


def _freshness_for(tmp_path, monkeypatch, lane_health):
    import os

    monkeypatch.setattr(dashboard_module, "datetime", _FrozenNoon)
    usage = tmp_path / "unified.jsonl"
    usage.write_text("", encoding="utf-8")
    health = tmp_path / "health"
    health.mkdir(exist_ok=True)
    heartbeat = health / "usage-collect.ok"
    heartbeat.touch()
    stamp = datetime(2026, 7, 14, 12, 30, 0, tzinfo=timezone.utc).timestamp()
    os.utime(heartbeat, (stamp, stamp))
    (health / "lanes-usage.json").write_text(json.dumps(lane_health), encoding="utf-8")
    client = create_app(unified_usage_ledger=str(usage)).test_client()
    return client.get("/api/unified-usage?hours=3").get_json()["freshness"]


def test_a_lane_that_never_succeeded_is_listed_but_does_not_stale_the_surface(tmp_path, monkeypatch):
    """A lane registered before its credentials exist has no history to go
    stale from. It stays visible under its own key; every chart stays up."""
    freshness = _freshness_for(
        tmp_path,
        monkeypatch,
        _lane_health_payload(hermes="2026-07-14T12:30:00Z", claude_code_secondary=None),
    )

    assert freshness["status"] == "fresh"
    assert freshness["stale_lanes"] == []
    assert freshness["never_lanes"] == ["claude_code_secondary"]
    by_lane = {lane["lane"]: lane for lane in freshness["lanes"]}
    assert by_lane["claude_code_secondary"]["status"] == "never"
    assert by_lane["claude_code_secondary"]["age_seconds"] is None
    assert by_lane["claude_code_secondary"]["last_error_class"] == "AuthError"
    assert by_lane["hermes"]["status"] == "fresh"


def test_a_lane_that_succeeded_then_went_quiet_still_stales_the_surface(tmp_path, monkeypatch):
    freshness = _freshness_for(
        tmp_path,
        monkeypatch,
        _lane_health_payload(
            hermes="2026-07-14T12:30:00Z",
            opencode="2026-07-11T12:30:00Z",
            claude_code_secondary=None,
        ),
    )

    assert freshness["status"] == "stale"
    assert freshness["stale_lanes"] == ["opencode"]
    assert freshness["never_lanes"] == ["claude_code_secondary"]
    by_lane = {lane["lane"]: lane for lane in freshness["lanes"]}
    assert by_lane["opencode"]["status"] == "stale"
    assert by_lane["claude_code_secondary"]["status"] == "never"


@pytest.mark.parametrize("route", ["/api/unified-usage", "/api/subscriptions", "/api/billing"])
@pytest.mark.parametrize("query", ["days=0", "days=-1", "days=0&before=2026-07-14T12:00:00Z"])
def test_private_reads_reject_zero_and_negative_day_windows(tmp_path, route, query):
    """days=0 was accepted and produced a page whose next_before equalled
    before — a cursor that never advances."""
    for name in ("unified.jsonl", "quota.jsonl", "billing.jsonl"):
        (tmp_path / name).write_text("", encoding="utf-8")
    client = create_app(
        ledger=str(tmp_path / "codex.jsonl"),
        unified_usage_ledger=str(tmp_path / "unified.jsonl"),
        quota_ledger=str(tmp_path / "quota.jsonl"),
        billing_ledger=str(tmp_path / "billing.jsonl"),
    ).test_client()

    response = client.get(f"{route}?{query}")

    assert response.status_code == 400
    assert response.get_json()["error"] == "days must be at least 1"
    assert client.get(f"{route}?days=1").status_code == 200


@pytest.mark.parametrize("route", ["/api/unified-usage", "/api/subscriptions"])
def test_hour_windows_below_one_are_rejected(tmp_path, route):
    for name in ("unified.jsonl", "quota.jsonl"):
        (tmp_path / name).write_text("", encoding="utf-8")
    client = create_app(
        ledger=str(tmp_path / "codex.jsonl"),
        unified_usage_ledger=str(tmp_path / "unified.jsonl"),
        quota_ledger=str(tmp_path / "quota.jsonl"),
    ).test_client()

    for query in ("hours=0", "hours=-1"):
        response = client.get(f"{route}?{query}")
        assert response.status_code == 400
        assert "hours must be between 1 and 168" == response.get_json()["error"]


def test_unified_usage_costs_share_the_billing_number_format(tmp_path):
    """str(Decimal) renders tiny sums in scientific notation ("5E-7"); the
    billing endpoint never does. One number format across both surfaces, on
    every cost field: totals, by_harness, by_provider_model."""
    client = _unified_client(
        tmp_path,
        [
            _unified_usage_fact("tiny-1", cost_status="estimated", estimated_cost_usd="0.0000005"),
            _unified_usage_fact("tiny-2", cost_status="estimated", estimated_cost_usd="0.0000002"),
            _unified_usage_fact("actual", cost_status="actual", actual_cost_usd="1.50",
                                harness="claude_code"),
            _unified_usage_fact("actual-zero", cost_status="actual", actual_cost_usd="0.00",
                                harness="claude_code"),
        ],
    )

    data = client.get("/api/unified-usage?days=36500").get_json()

    assert data["totals"]["estimated_cost_usd"] == "0.0000007"
    assert data["totals"]["actual_cost_usd"] == "1.5"
    by_harness = {row["harness"]: row for row in data["by_harness"]}
    assert by_harness["hermes"]["estimated_cost_usd"] == "0.0000007"
    assert by_harness["hermes"]["actual_cost_usd"] == "0"
    assert by_harness["claude_code"]["actual_cost_usd"] == "1.5"
    assert by_harness["claude_code"]["estimated_cost_usd"] == "0"
    (model_row,) = data["by_provider_model"]
    assert model_row["estimated_cost_usd"] == "0.0000007"
    assert model_row["actual_cost_usd"] == "1.5"
    serialized = json.dumps(data)
    assert "E-" not in serialized and "e-" not in serialized
    # Identical rendering to /api/billing for the same Decimal.
    from decimal import Decimal

    assert dashboard_module._decimal_text(Decimal("0.0000005") + Decimal("0.0000002")) == "0.0000007"


def test_subscription_chart_ignores_all_null_historical_observations(tmp_path, monkeypatch):
    """``latest`` already drops an observation with neither used nor remaining
    nor an explicit provider_state/coverage marker. The chart projection now
    applies the same rule: no unavailable_series entry, not counted."""

    class FrozenChartNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 5, 12, 34, 56, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenChartNow)
    rows = [
        _quota_observation(
            "week-live", "week", observed_at="2026-08-05T10:05:00Z",
            used_value="30", remaining_value="70", limit_value="100",
        ),
        # Historical null: the upstream-retired session window.
        _quota_observation(
            "session-null", "session", observed_at="2026-08-05T10:10:00Z",
            used_value=None, remaining_value=None, limit_value=None,
        ),
        # A null that carries an honest provider_state stays.
        _quota_observation(
            "five-inactive", "five_hour", observed_at="2026-08-05T10:15:00Z",
            used_value=None, remaining_value=None, limit_value=None,
            x_provider_state="inactive_or_not_reported",
        ),
    ]
    quota_ledger = tmp_path / "quota.jsonl"
    quota_ledger.write_text("\n".join(canonical_json(row) for row in rows) + "\n", encoding="utf-8")
    client = create_app(quota_ledger=str(quota_ledger)).test_client()

    payload = client.get("/api/subscriptions?hours=3&history=0").get_json()

    assert {row["quota_name"] for row in payload["latest"]} == {"week", "five_hour"}
    providers = {row["provider"]: row for row in payload["time_series"]["providers"]}
    openai = providers["openai-codex"]
    assert openai["observation_count"] == 2
    assert [series["quota_name"] for series in openai["series"]] == ["week"]
    assert [series["quota_name"] for series in openai["unavailable_series"]] == ["five_hour"]
    assert "session" not in json.dumps(payload["time_series"])


def test_readme_documents_the_current_private_route_contracts():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "comparison chart" not in readme
    # Freshness is derived from the collector heartbeat when present, falling
    # back to ledger write time; never-succeeded lanes are named separately
    # and do not stale the surface.
    assert "collector heartbeat" in readme
    assert "ledger-write freshness" not in readme
    assert "never_lanes" in readme
    subscriptions_doc = readme[readme.index("`GET /api/subscriptions`"):readme.index("`GET /api/billing`")]
    assert "hours=1..168" in subscriptions_doc
    assert "`observations` is always empty" in subscriptions_doc
    assert "`history=1` is rejected with a 400" in subscriptions_doc


def test_quota_burn_projects_per_window_series_rates_and_honest_etas(tmp_path, monkeypatch):
    """Burn panel (2026-09-02): per-account five-hour windows with monotonic
    rise deltas, a rate over the last three deltas, and an exhaustion ETA
    that never extends past the window's own reset. Two Claude accounts are
    two series — the identity rule from the subscription charts, applied to
    burn."""
    monkeypatch.setattr(dashboard_module, "datetime", _FrozenNoon)

    def claude(source_id, observed_at, used, resets_at="2026-07-14T13:10:00Z", account=None):
        row = _quota_observation(
            source_id,
            "five_hour",
            observed_at=observed_at,
            provider="anthropic",
            harness="claude_code",
            used_value=str(used),
            remaining_value=str(100 - used),
            limit_value="100",
            resets_at=resets_at,
            measurement_confidence="exact",
        )
        if account is not None:
            # The primary account's canonical series carries a NULL
            # account_ref; the fixture default is a non-empty string.
            row["account_ref"] = account or None
        return row

    rows = [
        # Primary account: 9:10 -> 12:00 climbing 23 -> 59 -> 92 -> 100
        # (each rise spans 25 min = one hourly cadence plus tick density).
        claude("p1", "2026-07-14T09:10:00Z", 23, account=""),
        claude("p2", "2026-07-14T10:10:00Z", 59, account=""),
        claude("p3", "2026-07-14T11:10:00Z", 92, account=""),
        claude("p4", "2026-07-14T12:00:00Z", 100, account=""),
        # Secondary account: flat at 5 until 10:10, then climbing — and its
        # resets_at already passed relative to the frozen now, so no ETA.
        claude("s1", "2026-07-14T09:10:00Z", 5, resets_at="2026-07-14T11:20:00Z",
               account="claude-code-secondary"),
        claude("s2", "2026-07-14T10:10:00Z", 23, resets_at="2026-07-14T11:20:00Z",
               account="claude-code-secondary"),
    ]
    quota_ledger = tmp_path / "quota.jsonl"
    quota_ledger.write_text("\n".join(canonical_json(row) for row in rows) + "\n", encoding="utf-8")
    client = create_app(quota_ledger=str(quota_ledger)).test_client()

    payload = client.get("/api/quota-burn?hours=3").get_json()

    assert payload["generated_at"] == "2026-07-14T12:34:56Z"
    # Two accounts, two series — never merged. account_ref itself is private
    # and never crosses the API, so the series are keyed by what they show.
    assert len(payload["series"]) == 2
    primary = next(row for row in payload["series"] if row["latest_used_pct"] == 100.0)
    secondary = next(row for row in payload["series"] if row["latest_used_pct"] == 23.0)
    assert primary["provider"] == "anthropic"
    assert primary["exhausted"] is True
    assert primary["latest_used_pct"] == 100.0
    assert [d["rise_pct"] for d in primary["deltas"]] == [36.0, 33.0, 8.0]
    # Rate over the last three deltas: 77 points across 170 minutes.
    assert primary["burn_rate_pct_per_hour"] == pytest.approx(77 / (170 / 60), rel=1e-3)
    assert primary["rate_window_deltas"] == 3
    assert primary["projected_exhaustion_at"] is None
    assert [o["used_pct"] for o in primary["observations"]] == [23.0, 59.0, 92.0, 100.0]

    secondary = next(row for row in payload["series"] if row["latest_used_pct"] == 23.0)
    assert secondary["exhausted"] is False
    assert secondary["seconds_to_reset"] is None
    assert secondary["projected_exhaustion_at"] is None
    assert [d["rise_pct"] for d in secondary["deltas"]] == [18.0]


def test_quota_burn_reset_reads_as_a_gap_not_a_negative_burn(tmp_path, monkeypatch):
    """A window that hits 100 and then observes a fresh window at low usage
    must not produce a negative delta; the fall advances the anchor and the
    next rise measures burn again. Flat steps produce no delta either."""
    monkeypatch.setattr(dashboard_module, "datetime", _FrozenNoon)
    rows = [
        _quota_observation("a", "five_hour", observed_at="2026-07-14T10:20:00Z",
                           provider="anthropic", harness="claude_code",
                           used_value="90", remaining_value="10", limit_value="100",
                           resets_at="2026-07-14T11:10:00Z", measurement_confidence="exact"),
        _quota_observation("b", "five_hour", observed_at="2026-07-14T11:20:00Z",
                           provider="anthropic", harness="claude_code",
                           used_value="100", remaining_value="0", limit_value="100",
                           resets_at="2026-07-14T11:10:00Z", measurement_confidence="exact"),
        # Reset landed: fresh window at 5 with a new reset deadline.
        _quota_observation("c", "five_hour", observed_at="2026-07-14T11:20:00Z",
                           provider="anthropic", harness="claude_code",
                           used_value="5", remaining_value="95", limit_value="100",
                           resets_at="2026-07-14T15:20:00Z", measurement_confidence="exact"),
        # Flat step: same value observed again — no delta.
        _quota_observation("d", "five_hour", observed_at="2026-07-14T11:50:00Z",
                           provider="anthropic", harness="claude_code",
                           used_value="5", remaining_value="95", limit_value="100",
                           resets_at="2026-07-14T15:20:00Z", measurement_confidence="exact"),
        # Burning again.
        _quota_observation("e", "five_hour", observed_at="2026-07-14T12:10:00Z",
                           provider="anthropic", harness="claude_code",
                           used_value="41", remaining_value="59", limit_value="100",
                           resets_at="2026-07-14T15:20:00Z", measurement_confidence="exact"),
    ]
    quota_ledger = tmp_path / "quota.jsonl"
    _write_usage_ledger(quota_ledger, rows)
    client = create_app(quota_ledger=str(quota_ledger)).test_client()

    series = client.get("/api/quota-burn?hours=3").get_json()["series"]
    assert len(series) == 1
    entry = series[0]
    # Only the two genuine rises measure burn: 90->100 (60 min) and 5->41
    # (20 min). The reset fall and the flat step advance the anchor without
    # producing a delta; b and c share a timestamp, and a zero gap is never
    # a measurement.
    assert [d["rise_pct"] for d in entry["deltas"]] == [10.0, 36.0]
    # Rate is "while it burns": 46 points across the 60+20 measured minutes,
    # not across idle wall time between them.
    assert entry["burn_rate_pct_per_hour"] == pytest.approx(46 / (80 / 60), rel=1e-3)
    assert entry["latest_used_pct"] == 41.0


def test_quota_burn_requires_configuration_and_hours_bounds(tmp_path, monkeypatch):
    ledger = tmp_path / "quota.jsonl"
    ledger.write_text("", encoding="utf-8")
    client = create_app(quota_ledger=str(ledger)).test_client()
    response = client.get("/api/quota-burn?hours=169")
    assert response.status_code == 400
    monkeypatch.delenv("QUOTA_LEDGER_PATH", raising=False)
    unconfigured = create_app().test_client()
    assert unconfigured.get("/api/quota-burn").status_code == 404
