"""Weekly allowance utilization — frontend surfaces (keeper request 2026-09-14:
"what percentage of my weekly windows I am using ... aggregate, and per
provider").

The shared contract (/api/weekly-utilization) is the backend's; these tests
pin the frontend half: the typed payload helper, the loader living in the
existing dashboard data hook (so switching views never refetches), the
compact Now strip, the full panel at the TOP of History ahead of the heavy
charts, the ``#weeklyUtilization`` deep link, token-only styling, and copy
that says "remaining" for current windows and "unused at reset" for
completed ones — never "wasted". The pure helper logic runs as real Node
tests (frontend/tests/weeklyUtilization.test.ts) through the runner
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
    helper = _src("lib/weeklyUtilization.ts")
    assert "export interface WeeklyUtilizationPayload" in helper
    for field in ("generated_at", "weeks", "lookback_days", "weighting", "current", "completed",
                  "windows", "coverage", "notes"):
        assert re.search(rf"^\s+{field}[?]?:", helper, re.M), field
    for field in ("used_pct", "remaining_pct", "account_count", "excluded_account_count",
                  "estimated_account_count", "cycle_count", "providers"):
        assert f"{field}:" in helper, field
    for field in ("provider_account_index", "provider_account_count", "quota_name", "observed_at",
                  "resets_at", "last_observed_at", "last_sample_seconds_before_reset", "reasons",
                  "eligible", "confidence", "status"):
        assert f"{field}" in helper, field
    assert '"primary" | "model_cap"' in helper
    # Percentages arrive as numeric strings; the helper parses, never trusts.
    assert "export function pctNumber" in helper
    assert "export function isWeeklyUtilizationPayload" in helper
    # No DOM, no fetch, no chart library in the pure helper.
    for forbidden in ("document.", "window.", "fetch(", "Chart"):
        assert forbidden not in helper, forbidden
    # Nothing here pools capacity or routes anything.
    for forbidden in ("pooled", "route", "wasted"):
        assert forbidden not in helper.lower(), forbidden


def test_loader_lives_in_the_dashboard_hook_with_the_estate_failure_law():
    hook = _src("hooks/useDashboardData.ts")
    assert "export interface WeeklyPanelState" in hook
    assert "weekly: WeeklyPanelState" in hook
    assert '"/api/weekly-utilization?weeks=4"' in hook
    assert "async function loadWeeklyUtilization" in hook
    assert "loadWeeklyUtilization(setState)" in hook
    loader = hook.split("async function loadWeeklyUtilization", 1)[1].split("\nasync function", 1)[0]
    # Same timeout law as the other private-panel loaders, last-good on a
    # failed refresh, and an explicit unavailable state for a 404 (safe
    # missing/unconfigured source) rather than a zero.
    assert "AbortSignal.timeout(30000)" in loader
    assert "showing last good data" in loader
    assert "404" in loader
    assert "isWeeklyUtilizationPayload(" in loader
    assert "contentVisible: false" in loader
    # The hook stays the single hydrate cycle: no view-level fetch.
    for view in ("views/NowView.tsx", "views/HistoryView.tsx", "components/WeeklyUtilizationCard.tsx"):
        assert "fetch(" not in _src(view), view


def test_history_leads_with_the_weekly_panel_and_keeps_every_old_chart():
    history = _src("views/HistoryView.tsx")
    weekly_at = history.index("<WeeklyUtilizationCard")
    unified_at = history.index("<UnifiedUsageCard")
    charts_at = history.index("<SubscriptionHistoryCharts")
    assert weekly_at < unified_at < charts_at
    assert "weekly={state.weekly}" in history


def test_now_carries_a_compact_summary_that_links_to_the_full_panel():
    now = _src("views/NowView.tsx")
    capacity_at = now.index("<CapacityCard")
    burn_at = now.index("<BurnRateCard")
    summary_at = now.index("<WeeklyUtilizationSummary")
    rail_at = now.index("<ResetRail")
    detail_at = now.index("<SubscriptionProviderDetail")
    # Capacity stays the first useful screen; the strip sits with the other
    # compact cards, never ahead of capacity.
    assert capacity_at < burn_at < summary_at < rail_at < detail_at
    card = _src("components/WeeklyUtilizationCard.tsx")
    assert "export function WeeklyUtilizationSummary" in card
    summary = card.split("export function WeeklyUtilizationSummary", 1)[1]
    assert 'href="#weeklyUtilization"' in summary
    assert "compactWeeklySummary(" in summary


def test_full_panel_is_focusable_deep_link_target_with_both_modes():
    card = _src("components/WeeklyUtilizationCard.tsx")
    assert "export function WeeklyUtilizationCard" in card
    panel = card.split("export function WeeklyUtilizationCard", 1)[1]
    assert 'id="weeklyUtilization"' in panel
    assert "tabIndex={-1}" in panel
    assert 'id="weeklyUtilizationTitle"' in panel
    assert "Weekly allowance use" in panel
    # A real two-state selector, not two panels (the switch is a
    # sub-component of the same file; the panel picks the summary by mode).
    assert '"current"' in panel and '"completed"' in panel
    assert "aria-pressed" in card
    # Aggregate headline, per-provider bars labelled directly, expandable
    # account/cycle detail and an expandable method note.
    assert "summaryHeadline(" in panel
    assert "weeklyProviderRows(" in panel
    assert 'role="progressbar"' in card
    assert "aria-valuenow" in card
    assert panel.count("<details") >= 2
    assert "sortWeeklyWindows(" in panel
    assert "cycleReasonText(" in card
    # Model caps are visible but marked out of the aggregate.
    assert "not included in aggregate" in panel
    assert '"model_cap"' in panel
    # Existing loading / error / last-good plumbing.
    assert "weekly.message" in panel
    assert "weekly.contentVisible" in panel


def test_copy_says_remaining_and_unused_at_reset_never_wasted():
    card = _src("components/WeeklyUtilizationCard.tsx")
    helper = _src("lib/weeklyUtilization.ts")
    joined = (card + helper).lower()
    assert "wasted" not in joined
    assert "unused at reset" in joined
    assert "remaining" in joined
    assert "average weekly allowance used" in joined
    assert "equal weight per account" in joined or "equal_account" in joined
    # The completed figure is a polling estimate, said out loud.
    assert "last sample before" in joined
    # The deep link routes to History.
    views = _src("lib/views.ts")
    assert re.search(r'\["weeklyUtilization",\s*"history"\]', views)


def test_completed_evidence_names_boundary_records_not_proven_resets():
    """A cycle row is an observed reset-target record — sometimes an
    excluded early replacement with a nominal date still in the future —
    not proof that a reset occurred. The evidence cell and the cycle row
    label say so; the counts and exclusion reasons stay."""
    card = _src("components/WeeklyUtilizationCard.tsx")
    assert "observed resets" not in card
    assert "no observed reset" not in card
    assert "${cycles.length} boundary records" in card
    assert "`window end ${formatTimestamp(cycle.resets_at)}`" in card
    assert "`reset ${formatTimestamp(cycle.resets_at)}`" not in card
    # Exclusion reasons and the eligible/excluded counts are untouched.
    assert "cycleReasonText(reason)" in card
    assert "${completed.cycle_count} eligible · ${completed.excluded_cycle_count} excluded" in card


def test_weekly_styles_are_token_only_and_contained_on_phones():
    css = frontend_surfaces.app_css()
    assert re.search(r"\n\.weekly-bar\s*\{", css)
    assert re.search(r"\n\.weekly-bar-fill\s*\{", css)
    assert re.search(r"\n\.weekly-mode\b", css)
    assert re.search(r"\n\.weekly-headline\s*\{([^}]*)\}", css)
    weekly = "\n".join(
        block for block in re.split(r"\n(?=\S)", css) if block.startswith(".weekly-")
    )
    assert weekly, "no .weekly-* rules in the app style layer"
    assert "#" not in re.sub(r"/\*.*?\*/", "", weekly, flags=re.S)
    assert "var(--" in weekly
    # Phone rule: the panel's grids collapse to one column so nothing widens
    # the root.
    phone = css.split("@media (max-width: 640px)", 1)[1].split("\n}\n", 1)[0]
    assert re.search(r"\.weekly-headline\s*\{[^}]*grid-template-columns:\s*1fr\s*;", phone)
    assert "overflow-wrap: anywhere" in weekly or "min-width: 0" in weekly


def test_no_new_dependency_and_no_chart_library_in_the_panel():
    manifest = json.loads((REPO_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert set(manifest["dependencies"]) == {"react", "react-dom"}
    card = _src("components/WeeklyUtilizationCard.tsx")
    for forbidden in ("chartConstructor", "ChartCanvas", "Donut", "<canvas", "<svg"):
        assert forbidden not in card, forbidden
