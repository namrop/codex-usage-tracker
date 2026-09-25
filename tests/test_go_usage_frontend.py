"""OpenCode Go usage explanation — frontend surfaces (keeper request
2026-09-14: "what consumes the shared allowance").

The shared contract (/api/opencode-go-usage, docs/go-native-usage-
explanation.md) is the pure projector's; these tests pin the frontend half:
the typed payload helper, the loader living in the existing dashboard data
hook, a small expandable card on History right after the weekly allowance
panel (not a new page), the native percent rendered as a whole integer, the
model rows labelled as a current-tariff estimate (never a bill, never
rescaled), coverage labels, token-only styling, and the ``#goUsage`` deep
link. The pure helper logic runs as real Node tests
(frontend/tests/goUsage.test.ts) through the runner
test_capacity_navigation.py already drives.
"""

from __future__ import annotations

import json
import re

import frontend_surfaces

REPO_ROOT = frontend_surfaces.REPO_ROOT


def _src(relative: str) -> str:
    return frontend_surfaces.source(relative)


def test_payload_helper_matches_the_shared_contract_and_stays_pure():
    helper = _src("lib/goUsage.ts")
    assert "export interface GoUsagePayload" in helper
    for field in ("generated_at", "window", "account", "native", "models", "totals",
                  "reconciliation", "coverage", "tariff", "notes"):
        assert re.search(rf"^\s+{field}[?]?:", helper, re.M), field
    for field in ("percent", "status", "observed_at", "resets_at", "fresh", "age_seconds", "capped",
                  "percent_resolution", "source_surface"):
        assert re.search(rf"^\s+{field}[?]?:", helper, re.M), field
    for field in ("model", "event_count", "input_tokens", "cache_read_tokens", "cache_write_tokens",
                  "output_tokens", "total_tokens", "reasoning_tokens", "multiplier",
                  "estimated_list_cost_usd", "estimated_quota_units", "estimated_quota_pct",
                  "share_of_modeled_pct", "pricing_status"):
        assert f"{field}:" in helper, field
    for field in ("native_lower_pct", "native_upper_pct", "modeled_pct", "modeled_complete",
                  "matches_within_rounding", "band_gap_pct"):
        assert f"{field}:" in helper, field
    # The native percent is an integer or null — the type says so.
    assert re.search(r"^\s+percent:\s*number \| null", helper, re.M)
    assert "export function isGoUsagePayload" in helper
    assert "export function nativeHeadline" in helper
    assert "export function goModelRows" in helper
    assert "export function reconciliationText" in helper
    # No DOM, no fetch, no chart library in the pure helper.
    for forbidden in ("document.", "window.", "fetch(", "Chart"):
        assert forbidden not in helper, forbidden
    # Nothing here routes, rescales, or invents a native figure.
    for forbidden in ("route", "normalize", "rescale"):
        assert forbidden not in helper.lower(), forbidden


def test_loader_lives_in_the_dashboard_hook_with_the_estate_failure_law():
    hook = _src("hooks/useDashboardData.ts")
    assert "export interface GoUsagePanelState" in hook
    assert "goUsage: GoUsagePanelState" in hook
    assert '"/api/opencode-go-usage"' in hook
    assert "async function loadGoUsage" in hook
    assert "loadGoUsage(setState)" in hook
    loader = hook.split("async function loadGoUsage", 1)[1].split("\nasync function", 1)[0]
    assert "AbortSignal.timeout(30000)" in loader
    assert "showing last good data" in loader
    assert "404" in loader
    assert "isGoUsagePayload(" in loader
    assert "contentVisible: false" in loader
    # The hook stays the single hydrate cycle: no view-level fetch.
    for view in ("views/HistoryView.tsx", "components/GoUsageCard.tsx"):
        assert "fetch(" not in _src(view), view


def test_history_places_the_go_card_last():
    # Moved from right after the weekly panel to the end of History by the
    # keeper-approved UI review (item 9, msg 1553037883747598450, 2026-09-25).
    history = _src("views/HistoryView.tsx")
    weekly_at = history.index("<WeeklyUtilizationCard")
    unified_at = history.index("<UnifiedUsageCard")
    charts_at = history.index("<SubscriptionHistoryCharts")
    go_at = history.index("<GoUsageCard")
    assert weekly_at < unified_at < charts_at < go_at
    assert "goUsage={state.goUsage}" in history
    views = _src("lib/views.ts")
    assert re.search(r'\["goUsage",\s*"history"\]', views)


def test_card_is_a_small_expandable_deep_link_target_with_both_modes():
    card = _src("components/GoUsageCard.tsx")
    assert "export function GoUsageCard" in card
    assert 'id="goUsage"' in card
    assert "tabIndex={-1}" in card
    assert 'id="goUsageTitle"' in card
    # Expandable: the detail (model rows, method) sits behind <details>.
    assert card.count("<details") >= 2
    assert "aria-pressed" in card
    assert '"quota"' in card and '"tokens"' in card
    assert "nativeHeadline(" in card
    assert "goModelRows(" in card
    assert "reconciliationText(" in card
    assert "goCoverageLine(" in card
    assert 'role="progressbar"' in card
    assert "aria-valuenow" in card
    # Existing loading / error / last-good plumbing.
    assert "goUsage.message" in card
    assert "goUsage.contentVisible" in card
    # No new dependency, no chart library.
    manifest = json.loads((REPO_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert set(manifest["dependencies"]) == {"react", "react-dom"}
    for forbidden in ("chartConstructor", "ChartCanvas", "Donut", "<canvas", "<svg"):
        assert forbidden not in card, forbidden


def test_copy_says_provider_reported_and_current_tariff_estimate_never_bill():
    card = _src("components/GoUsageCard.tsx")
    helper = _src("lib/goUsage.ts")
    joined = (card + helper).lower()
    assert "current-tariff estimate" in joined
    assert "provider-reported" in joined
    assert "whole-point" in joined
    assert "at least 100%" in joined
    assert "within rounding" in joined
    assert "reasoning" in joined
    # The estimate is never presented as money owed.
    for forbidden in ("bill", "charged", "invoice", "wasted"):
        assert forbidden not in joined, forbidden
    # The native integer is never rendered through the one-decimal formatter.
    native_block = card.split("nativeHeadline(", 1)[1]
    assert "percentText" in native_block
    assert "toFixed(1)" not in helper.split("export function nativeHeadline", 1)[1].split("\nexport function", 1)[0]


def test_go_styles_are_token_only_and_reuse_the_weekly_layer():
    css = frontend_surfaces.app_css()
    go = "\n".join(block for block in re.split(r"\n(?=\S)", css) if block.startswith(".go-usage"))
    assert go, "no .go-usage* rules in the app style layer"
    assert "#" not in re.sub(r"/\*.*?\*/", "", go, flags=re.S)
    assert "var(--" in go
    card = _src("components/GoUsageCard.tsx")
    # The bars, details and notes reuse the weekly classes rather than a
    # second vocabulary.
    for reused in ("weekly-bar", "weekly-detail", "weekly-notes", "weekly-mode"):
        assert reused in card, reused
