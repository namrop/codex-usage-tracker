"""Native OpenCode Go whole-point metadata on the existing scalar displays
(2026-09-14 follow-up to the Go usage explanation).

The native usage API reports whole percents (floor-rounded, capped at 100)
and the backend marks those rows with ``source_surface``,
``percent_resolution`` and ``provider_state`` on subscription rows, burn
rows and ``weekly.window.current``. These tests pin that the subscriptions,
capacity, burn and weekly surfaces read that metadata through one pure
helper (frontend/src/lib/nativePercent.ts, Node-tested in
frontend/tests/nativeWholePoint.test.ts), render such rows as integers with
a provider-reported whole-point label, keep every other row fractional, and
keep saying that Go has no completed native window yet.
"""

from __future__ import annotations

import re

import frontend_surfaces


def _src(relative: str) -> str:
    return frontend_surfaces.source(relative)


def test_one_pure_helper_decides_whole_point_rendering():
    helper = _src("lib/nativePercent.ts")
    assert 'export const NATIVE_WHOLE_POINT_SURFACE = "opencode_go_usage_api"' in helper
    assert "export function isWholePointNative" in helper
    assert "export function nativePercentText" in helper
    assert "export function wholePointLabel" in helper
    assert "export function providerStateText" in helper
    assert "percent_resolution" in helper and "source_surface" in helper and "provider_state" in helper
    for forbidden in ("document.", "window.", "fetch(", "Chart"):
        assert forbidden not in helper, forbidden
    # Whole-point text is an integer; the fallback is the estate's one-decimal
    # formatter, never a second formatter.
    assert "toFixed(0)" in helper
    assert "percent(" in helper


def test_capacity_and_burn_derivations_carry_the_metadata_and_the_cards_render_it():
    capacity = _src("lib/capacity.ts")
    assert "wholePoint: boolean" in capacity
    assert "resolutionLabel: string | null" in capacity
    assert "providerState: string | null" in capacity
    assert "usedText: string | null" in capacity
    assert "isWholePointNative(" in capacity
    card = _src("components/CapacityCard.tsx")
    assert "window.resolutionLabel" in card
    assert "window.usedText" in card

    burn = _src("lib/burn.ts")
    assert "wholePoint: boolean" in burn
    assert "latestPctText: string" in burn
    assert "resolutionLabel: string | null" in burn
    assert "nativePercentText(" in burn
    burn_card = _src("components/BurnRateCard.tsx")
    assert "row.latestPctText" in burn_card
    assert "row.resolutionLabel" in burn_card
    # The Used cell no longer formats the number itself.
    assert "percent(row.latestPct)" not in burn_card
    # Rates stay the backend's decimal figure.
    assert "row.rate.toFixed(1)" in burn_card


def test_subscription_window_explains_whole_point_provenance_without_reformatting_others():
    card = _src("components/SubscriptionsCard.tsx")
    assert "isWholePointNative(" in card or "wholePointLabel(" in card
    assert "whole-point" in card.lower() or "whole points" in card.lower()
    # Provider state was already surfaced and stays.
    assert "Provider state:" in card


def test_weekly_panel_reads_window_metadata_and_keeps_the_no_completed_native_window_text():
    helper = _src("lib/weeklyUtilization.ts")
    for field in ("source_surface", "percent_resolution", "provider_state"):
        assert re.search(rf"^\s+{field}\?:", helper, re.M), field
    assert "export function isWholePointCurrent" in helper
    assert "export function currentPctText" in helper
    assert 'export const NO_COMPLETED_NATIVE_TEXT = "no completed native window yet"' in helper
    assert "export function weeklyProviderRows(summary: WeeklySummary, mode: WeeklyMode, windows?:" in helper
    assert "wholePoint: boolean" in helper
    card = _src("components/WeeklyUtilizationCard.tsx")
    assert "currentPctText(current," in card
    assert "NO_COMPLETED_NATIVE_TEXT" in card
    assert "weeklyProviderRows(summary, mode, payload" in card
    # The aggregate headline and the compact strip still use the one-decimal
    # mean formatter — a mean of accounts is not a provider integer.
    assert "summaryHeadline(summary, mode)" in card
    assert "usedText: pctText(summary.used_pct)" in helper
    # The historical wording stays for non-native evidence.
    assert "${cycles.length} boundary records" in card
