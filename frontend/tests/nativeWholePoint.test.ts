import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { deriveBurnRows } from "../src/lib/burn";
import { deriveCapacityWindow } from "../src/lib/capacity";
import {
  NATIVE_WHOLE_POINT_SURFACE,
  WHOLE_POINT_LABEL,
  isWholePointNative,
  nativePercentText,
  providerStateText,
  wholePointLabel,
} from "../src/lib/nativePercent";
import {
  NO_COMPLETED_NATIVE_TEXT,
  currentPctText,
  isWholePointCurrent,
  summaryHeadline,
  weeklyProviderRows,
  type WeeklySummary,
  type WeeklyWindow,
} from "../src/lib/weeklyUtilization";

// Native OpenCode Go whole-point metadata (2026-09-14). The native usage
// API reports whole percents (floor-rounded, capped at 100) and the backend
// marks those rows with source_surface=opencode_go_usage_api,
// percent_resolution=1 and provider_state=ok|rate_limited on subscription
// rows, burn rows and weekly.window.current. Those rows render as integers
// with a provider-reported whole-point label; the cookie-backed rows, other
// providers, aggregate account means and derived rates keep their decimals.

const NATIVE = {
  source_surface: NATIVE_WHOLE_POINT_SURFACE,
  percent_resolution: 1,
  provider_state: "rate_limited",
};

describe("isWholePointNative / nativePercentText", () => {
  it("recognizes the native marker and refuses rows without it", () => {
    assert.equal(isWholePointNative(NATIVE), true);
    assert.equal(isWholePointNative({ ...NATIVE, percent_resolution: "1" }), true);
    assert.equal(isWholePointNative({ source_surface: "opencode_go_server_function" }), false);
    assert.equal(isWholePointNative({ percent_resolution: 1 }), false, "resolution without the native surface is not claimed");
    assert.equal(isWholePointNative({ source_surface: NATIVE_WHOLE_POINT_SURFACE }), false, "surface without the resolution is not claimed");
    assert.equal(isWholePointNative({ ...NATIVE, percent_resolution: 0.1 }), false);
    assert.equal(isWholePointNative(null), false);
    assert.equal(isWholePointNative(undefined), false);
  });

  it("renders whole-point rows as integers and everything else at one decimal", () => {
    assert.equal(nativePercentText(100, NATIVE), "100%");
    assert.equal(nativePercentText("100.0", NATIVE), "100%");
    assert.equal(nativePercentText("24", NATIVE), "24%");
    assert.equal(nativePercentText(24.5, {}), "24.5%");
    assert.equal(nativePercentText("24.5", { source_surface: "opencode_go_server_function" }), "24.5%");
    assert.equal(nativePercentText(100, {}), "100.0%");
    assert.equal(nativePercentText(null, NATIVE), "—");
    assert.equal(nativePercentText("n/a", NATIVE), "—");
  });

  it("never rounds a non-integer that arrives on a whole-point row into a false integer", () => {
    assert.equal(nativePercentText(24.5, NATIVE), "24.5%");
  });

  it("labels provenance and provider state in words", () => {
    assert.equal(WHOLE_POINT_LABEL, "provider-reported · whole points");
    assert.equal(wholePointLabel(NATIVE), WHOLE_POINT_LABEL);
    assert.equal(wholePointLabel({}), null);
    assert.equal(providerStateText(NATIVE), "rate limited");
    assert.equal(providerStateText({ provider_state: "ok" }), "ok");
    assert.equal(providerStateText({}), null);
    assert.equal(providerStateText({ provider_state: "" }), null);
  });
});

describe("capacity windows", () => {
  const NOW = Date.parse("2026-09-15T03:00:00Z");
  const OPTIONS = { now: NOW, maxAgeSeconds: 3 * 3600 };
  function row(overrides: Record<string, unknown>): Record<string, unknown> {
    return {
      provider: "opencode-go",
      provider_canonical: "opencode-go",
      harness: "opencode_go_api",
      observed_at: "2026-09-15T02:50:00Z",
      quota_name: "week",
      quota_scope: "account",
      window_kind: "fixed",
      unit: "percent",
      used_value: "100",
      limit_value: "100",
      remaining_value: "0",
      resets_at: "2026-09-21T00:00:00Z",
      measurement_confidence: "exact",
      provider_state: null,
      coverage_status: null,
      provider_account_index: 1,
      provider_account_count: 1,
      ...overrides,
    };
  }

  it("marks a native Go window whole-point, prints integers and carries the provider state", () => {
    const window = deriveCapacityWindow(row(NATIVE), OPTIONS, "opencode-go", 1);
    assert.equal(window.wholePoint, true);
    assert.equal(window.usedText, "100%");
    assert.equal(window.remainingText, "0% left");
    assert.equal(window.providerState, "rate_limited");
    assert.equal(window.resolutionLabel, WHOLE_POINT_LABEL);
    assert.equal(window.atCap, true);
    const partial = deriveCapacityWindow(row({ ...NATIVE, used_value: "24", remaining_value: "76", provider_state: "ok" }), OPTIONS, "opencode-go", 1);
    assert.equal(partial.usedText, "24%");
    assert.equal(partial.remainingText, "76% left");
    assert.equal(partial.providerState, "ok");
  });

  it("leaves the cookie-backed Go row and other providers fractional", () => {
    const cookie = deriveCapacityWindow(row({ used_value: "24.5", remaining_value: "75.5", source_surface: "opencode_go_server_function" }), OPTIONS, "opencode-go", 1);
    assert.equal(cookie.wholePoint, false);
    assert.equal(cookie.usedText, "24.5%");
    assert.equal(cookie.resolutionLabel, null);
    assert.equal(cookie.providerState, null);
    const claude = deriveCapacityWindow(row({ provider: "anthropic", provider_canonical: "anthropic", harness: "claude_code", quota_name: "seven_day", used_value: "62.5", remaining_value: "37.5" }), OPTIONS, "anthropic", 1);
    assert.equal(claude.wholePoint, false);
    assert.equal(claude.usedText, "62.5%");
    // A dollar-cap window has no percent text to print.
    const dollars = deriveCapacityWindow(row({ unit: "usd", used_value: "4", limit_value: "20", remaining_value: "16", window_kind: "rolling" }), OPTIONS, "opencode-go", 1);
    assert.equal(dollars.usedText, null);
  });
});

describe("burn rows", () => {
  function series(overrides: Record<string, unknown>): Record<string, unknown> {
    return {
      provider: "opencode-go",
      label: "OpenCode Go",
      quota_name: "week",
      harness: "opencode_go_api",
      window_kind: "fixed",
      unit: "percent",
      provider_account_index: 1,
      provider_account_count: 1,
      measurement_confidence: "exact",
      confidence_status: "exact",
      resets_at: "2026-09-21T00:00:00Z",
      seconds_to_reset: 500000,
      latest_observed_at: "2026-09-15T02:50:00Z",
      latest_used_pct: 100,
      observation_count: 3,
      burn_rate_pct_per_hour: 2.35,
      rate_window_deltas: 2,
      projection_status: "exhausted",
      projection_reason: "used_at_cap",
      observations: [
        { observed_at: "2026-09-15T00:50:00Z", used_pct: 95 },
        { observed_at: "2026-09-15T01:50:00Z", used_pct: 98 },
        { observed_at: "2026-09-15T02:50:00Z", used_pct: 100 },
      ],
      ...overrides,
    };
  }

  it("prints the native Go latest percent as an integer and keeps the derived rate decimal", () => {
    const [row] = deriveBurnRows([series(NATIVE)]);
    assert.equal(row.wholePoint, true);
    assert.equal(row.latestPctText, "100%");
    assert.equal(row.latestPct, 100);
    assert.equal(row.rate, 2.35);
    assert.equal(row.providerState, "rate_limited");
    assert.equal(row.resolutionLabel, WHOLE_POINT_LABEL);
  });

  it("keeps fractional text for the cookie surface and other providers", () => {
    const [cookie, claude, empty] = deriveBurnRows([
      series({ latest_used_pct: 24.5, source_surface: "opencode_go_server_function" }),
      series({ provider: "anthropic", harness: "claude_code", quota_name: "seven_day", latest_used_pct: 40 }),
      series({ latest_used_pct: null }),
    ]);
    assert.equal(cookie.wholePoint, false);
    assert.equal(cookie.latestPctText, "24.5%");
    assert.equal(cookie.resolutionLabel, null);
    assert.equal(claude.latestPctText, "40.0%");
    assert.equal(empty.latestPctText, "—");
  });
});

describe("weekly windows and provider rows", () => {
  function window(overrides: Partial<WeeklyWindow>): WeeklyWindow {
    return {
      provider: "opencode-go",
      label: "OpenCode Go",
      provider_account_index: 1,
      provider_account_count: 1,
      quota_name: "week",
      role: "primary",
      current: {
        used_pct: "100",
        remaining_pct: "0",
        observed_at: "2026-09-15T02:50:00Z",
        resets_at: "2026-09-21T00:00:00Z",
        confidence: "exact",
        status: "fresh",
        eligible: true,
        ...NATIVE,
      },
      completed: { used_pct: null, remaining_pct: null, cycle_count: 0, excluded_cycle_count: 2 },
      cycles: [],
      ...overrides,
    };
  }
  const goWindow = window({});
  const claudeWindow = window({
    provider: "anthropic", label: "Claude", quota_name: "seven_day",
    current: { used_pct: "62.5", remaining_pct: "37.5", observed_at: "2026-09-15T02:50:00Z", resets_at: "2026-09-18T00:00:00Z", confidence: "exact", status: "fresh", eligible: true },
    completed: { used_pct: "71.5", remaining_pct: "28.5", cycle_count: 4, excluded_cycle_count: 0 },
  });
  const current: WeeklySummary = {
    used_pct: "81.3", remaining_pct: "18.7", account_count: 2, excluded_account_count: 0, estimated_account_count: 0, cycle_count: 0,
    providers: [
      { provider: "anthropic", label: "Anthropic / Claude", used_pct: "62.5", remaining_pct: "37.5", account_count: 1, excluded_account_count: 0, estimated_account_count: 0, cycle_count: 0 },
      { provider: "opencode-go", label: "OpenCode Go", used_pct: "100.0", remaining_pct: "0.0", account_count: 1, excluded_account_count: 0, estimated_account_count: 0, cycle_count: 0 },
    ],
  };
  const completed: WeeklySummary = {
    used_pct: "71.5", remaining_pct: "28.5", account_count: 1, excluded_account_count: 1, estimated_account_count: 0, cycle_count: 4,
    providers: [
      { provider: "anthropic", label: "Anthropic / Claude", used_pct: "71.5", remaining_pct: "28.5", account_count: 1, excluded_account_count: 0, estimated_account_count: 0, cycle_count: 4 },
      { provider: "opencode-go", label: "OpenCode Go", used_pct: null, remaining_pct: null, account_count: 0, excluded_account_count: 1, estimated_account_count: 0, cycle_count: 0 },
    ],
  };

  it("prints a native current window as integers and a plain window at one decimal", () => {
    assert.equal(isWholePointCurrent(goWindow.current), true);
    assert.equal(currentPctText(goWindow.current, "used_pct"), "100%");
    assert.equal(currentPctText(goWindow.current, "remaining_pct"), "0%");
    assert.equal(isWholePointCurrent(claudeWindow.current), false);
    assert.equal(currentPctText(claudeWindow.current, "used_pct"), "62.5%");
    const cookie = { ...goWindow.current, used_pct: "24.5", remaining_pct: "75.5", source_surface: "opencode_go_server_function", percent_resolution: undefined };
    assert.equal(currentPctText(cookie, "used_pct"), "24.5%");
  });

  it("uses the windows' metadata for a single-account provider row and keeps every other row and the aggregate fractional", () => {
    const rows = weeklyProviderRows(current, "current", [goWindow, claudeWindow]);
    const go = rows.find((row) => row.provider === "opencode-go")!;
    assert.equal(go.wholePoint, true);
    assert.equal(go.usedText, "100%");
    assert.equal(go.complementText, "0%");
    assert.equal(go.usedPct, 100);
    assert.equal(go.coverageText, "1 account · provider-reported · whole points");
    const claude = rows.find((row) => row.provider === "anthropic")!;
    assert.equal(claude.wholePoint, false);
    assert.equal(claude.usedText, "62.5%");
    // Without the windows nothing is guessed.
    const bare = weeklyProviderRows(current, "current").find((row) => row.provider === "opencode-go")!;
    assert.equal(bare.wholePoint, false);
    assert.equal(bare.usedText, "100.0%");
    // A two-account provider is a mean and stays at one decimal even if
    // every window is native.
    const twoAccounts = weeklyProviderRows(
      { ...current, providers: [{ ...current.providers[1], used_pct: "62.5", remaining_pct: "37.5", account_count: 2 }] },
      "current",
      [goWindow, window({ provider_account_index: 2, provider_account_count: 2, current: { ...goWindow.current, used_pct: "25", remaining_pct: "75" } })],
    )[0];
    assert.equal(twoAccounts.wholePoint, false);
    assert.equal(twoAccounts.usedText, "62.5%");
    // The aggregate headline is a mean of accounts and never changes shape.
    assert.equal(summaryHeadline(current, "current").usedText, "81.3%");
  });

  it("says out loud that no completed native window exists yet for Go", () => {
    assert.equal(NO_COMPLETED_NATIVE_TEXT, "no completed native window yet");
    const rows = weeklyProviderRows(completed, "completed", [goWindow, claudeWindow]);
    const go = rows.find((row) => row.provider === "opencode-go")!;
    assert.equal(go.empty, true);
    assert.equal(go.coverageText, "no completed native window yet · 1 excluded");
    const claude = rows.find((row) => row.provider === "anthropic")!;
    assert.equal(claude.coverageText, "1 account · 4 cycles");
    // A non-native provider with no completed cycle keeps the generic words.
    const generic = weeklyProviderRows(completed, "completed", [claudeWindow]).find((row) => row.provider === "opencode-go")!;
    assert.equal(generic.coverageText, "no contributing account · 1 excluded");
  });
});
