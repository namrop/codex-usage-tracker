import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  compactWeeklySummary,
  cycleReasonText,
  isWeeklyUtilizationPayload,
  pctNumber,
  pctText,
  sortWeeklyWindows,
  summaryHeadline,
  weeklyCoverageLine,
  weeklyProviderRows,
  weeklyWindowKey,
  type WeeklyUtilizationPayload,
  type WeeklyWindow,
} from "../src/lib/weeklyUtilization";

// Fixture mirrors /api/weekly-utilization (shared contract 2026-09-14):
// percentages are numeric STRINGS, one primary weekly window per account,
// a Claude model cap kept as a separate detail row, and one Kimi account
// excluded from the current summary for staleness.

function window(overrides: Partial<WeeklyWindow>): WeeklyWindow {
  return {
    provider: "anthropic",
    label: "Claude 1",
    provider_account_index: 1,
    provider_account_count: 2,
    quota_name: "seven_day",
    role: "primary",
    current: {
      used_pct: "62.0",
      remaining_pct: "38.0",
      observed_at: "2026-09-14T19:55:00Z",
      resets_at: "2026-09-18T00:00:00Z",
      confidence: "exact",
      status: "fresh",
      eligible: true,
    },
    completed: { used_pct: "71.5", remaining_pct: "28.5", cycle_count: 4, excluded_cycle_count: 1 },
    cycles: [
      {
        resets_at: "2026-09-11T00:00:00Z",
        last_observed_at: "2026-09-10T23:40:00Z",
        used_pct: "80.0",
        remaining_pct: "20.0",
        confidence: "exact",
        eligible: true,
        reasons: [],
        last_sample_seconds_before_reset: 1200,
      },
      {
        resets_at: "2026-09-04T00:00:00Z",
        last_observed_at: "2026-09-03T12:00:00Z",
        used_pct: "40.0",
        remaining_pct: "60.0",
        confidence: "exact",
        eligible: false,
        reasons: ["missing_tail"],
        last_sample_seconds_before_reset: 43200,
      },
    ],
    ...overrides,
  };
}

const PAYLOAD: WeeklyUtilizationPayload = {
  generated_at: "2026-09-14T20:00:00Z",
  weeks: 4,
  lookback_days: 35,
  weighting: "equal_account",
  current: {
    used_pct: "48.3",
    remaining_pct: "51.7",
    account_count: 3,
    excluded_account_count: 1,
    estimated_account_count: 1,
    cycle_count: 0,
    providers: [
      { provider: "anthropic", label: "Anthropic / Claude", used_pct: "45.0", remaining_pct: "55.0", account_count: 2, excluded_account_count: 0, estimated_account_count: 0, cycle_count: 0 },
      { provider: "kimi-k3-coding", label: "Kimi K3 Coding", used_pct: null, remaining_pct: null, account_count: 0, excluded_account_count: 1, estimated_account_count: 0, cycle_count: 0 },
      { provider: "opencode-go", label: "OpenCode Go", used_pct: "55.0", remaining_pct: "45.0", account_count: 1, excluded_account_count: 0, estimated_account_count: 1, cycle_count: 0 },
    ],
  },
  completed: {
    used_pct: "66.2",
    remaining_pct: "33.8",
    account_count: 3,
    excluded_account_count: 0,
    estimated_account_count: 0,
    cycle_count: 9,
    providers: [
      { provider: "anthropic", label: "Anthropic / Claude", used_pct: "70.0", remaining_pct: "30.0", account_count: 2, excluded_account_count: 0, estimated_account_count: 0, cycle_count: 6 },
      { provider: "kimi-k3-coding", label: "Kimi K3 Coding", used_pct: "58.6", remaining_pct: "41.4", account_count: 1, excluded_account_count: 0, estimated_account_count: 0, cycle_count: 3 },
      { provider: "opencode-go", label: "OpenCode Go", used_pct: null, remaining_pct: null, account_count: 0, excluded_account_count: 1, estimated_account_count: 0, cycle_count: 0 },
    ],
  },
  windows: [
    window({ provider: "kimi-k3-coding", label: "Kimi K3", provider_account_index: 1, provider_account_count: 1, quota_name: "week",
      current: { used_pct: "92.0", remaining_pct: "8.0", observed_at: "2026-09-14T10:00:00Z", resets_at: "2026-09-20T00:00:00Z", confidence: "exact", status: "stale", eligible: false } }),
    window({ label: "Claude 1", provider_account_index: 1, quota_name: "seven_day_fable", role: "model_cap" }),
    window({ label: "Claude 2", provider_account_index: 2 }),
    window({ label: "Claude 1", provider_account_index: 1 }),
    window({ provider: "opencode-go", label: "OpenCode Go", provider_account_index: 1, provider_account_count: 1, quota_name: "week",
      current: { used_pct: "55.0", remaining_pct: "45.0", observed_at: "2026-09-14T19:50:00Z", resets_at: "2026-09-19T00:00:00Z", confidence: "estimated", status: "fresh", eligible: true },
      completed: { used_pct: null, remaining_pct: null, cycle_count: 0, excluded_cycle_count: 2 }, cycles: [] }),
  ],
  coverage: { source_rows: 1200, invalid_rows: 3, excluded_cycles: 3 },
  notes: ["Completed values are the last sample before each observed reset, a polling estimate."],
};

describe("pctNumber / pctText", () => {
  it("parses the contract's numeric strings and refuses anything else", () => {
    assert.equal(pctNumber("48.3"), 48.3);
    assert.equal(pctNumber("100"), 100);
    assert.equal(pctNumber(null), null);
    assert.equal(pctNumber(undefined), null);
    assert.equal(pctNumber(""), null);
    assert.equal(pctNumber("n/a"), null);
    assert.equal(pctText("48.3"), "48.3%");
    assert.equal(pctText("7"), "7.0%");
    assert.equal(pctText(null), "—");
  });
});

describe("summaryHeadline", () => {
  it("says remaining for current windows and unused at reset for completed ones — never wasted", () => {
    const current = summaryHeadline(PAYLOAD.current, "current");
    assert.equal(current.usedText, "48.3%");
    assert.equal(current.complementLabel, "remaining");
    assert.equal(current.complementText, "51.7%");
    const completed = summaryHeadline(PAYLOAD.completed, "completed");
    assert.equal(completed.usedText, "66.2%");
    assert.equal(completed.complementLabel, "unused at reset");
    assert.equal(completed.complementText, "33.8%");
    assert.ok(!JSON.stringify(current).toLowerCase().includes("wasted"));
    assert.ok(!JSON.stringify(completed).toLowerCase().includes("wasted"));
  });

  it("reads em-dash when no account contributed", () => {
    const empty = summaryHeadline({ ...PAYLOAD.current, used_pct: null, remaining_pct: null, account_count: 0 }, "current");
    assert.equal(empty.usedText, "—");
    assert.equal(empty.complementText, "—");
  });
});

describe("weeklyCoverageLine", () => {
  it("counts contributing, estimated and excluded accounts for current windows", () => {
    assert.equal(
      weeklyCoverageLine(PAYLOAD.current, "current"),
      "3 accounts · 1 estimated · 1 excluded (stale or missing) · equal weight per account",
    );
  });

  it("adds completed cycle counts and the last-sample qualification", () => {
    assert.equal(
      weeklyCoverageLine(PAYLOAD.completed, "completed"),
      "3 accounts · 9 completed cycles · last sample before each reset · equal weight per account",
    );
  });

  it("says so when nothing contributed", () => {
    const line = weeklyCoverageLine({ ...PAYLOAD.current, account_count: 0, excluded_account_count: 4, estimated_account_count: 0 }, "current");
    assert.ok(line.startsWith("no contributing accounts"), line);
    assert.ok(line.includes("4 excluded"));
  });
});

describe("weeklyProviderRows", () => {
  it("keeps every provider, in the estate order, with numeric bars and a coverage note", () => {
    const rows = weeklyProviderRows(PAYLOAD.current, "current");
    assert.deepEqual(rows.map((row) => row.provider), ["anthropic", "opencode-go", "kimi-k3-coding"]);
    const claude = rows[0];
    assert.equal(claude.label, "Anthropic / Claude");
    assert.equal(claude.usedPct, 45);
    assert.equal(claude.complementPct, 55);
    assert.equal(claude.usedText, "45.0%");
    assert.equal(claude.complementText, "55.0%");
    assert.equal(claude.coverageText, "2 accounts");
    const opencode = rows[1];
    assert.equal(opencode.coverageText, "1 account · 1 estimated");
    assert.equal(opencode.estimated, true);
    const kimi = rows[2];
    assert.equal(kimi.usedPct, null);
    assert.equal(kimi.usedText, "—");
    assert.equal(kimi.coverageText, "no contributing account · 1 excluded");
    assert.equal(kimi.empty, true);
  });

  it("adds cycle counts under completed windows", () => {
    const rows = weeklyProviderRows(PAYLOAD.completed, "completed");
    assert.equal(rows[0].coverageText, "2 accounts · 6 cycles");
    assert.equal(rows[1].provider, "opencode-go");
    assert.equal(rows[1].coverageText, "no contributing account · 1 excluded");
    assert.equal(rows[2].coverageText, "1 account · 3 cycles");
  });
});

describe("sortWeeklyWindows / weeklyWindowKey", () => {
  it("orders primary windows by provider then account and pushes model caps to the end", () => {
    const sorted = sortWeeklyWindows(PAYLOAD.windows);
    assert.deepEqual(
      sorted.map((row) => `${row.label}:${row.quota_name}`),
      ["Claude 1:seven_day", "Claude 2:seven_day", "OpenCode Go:week", "Kimi K3:week", "Claude 1:seven_day_fable"],
    );
    // Sorting never mutates the payload the hook holds.
    assert.equal(PAYLOAD.windows[0].provider, "kimi-k3-coding");
    assert.equal(weeklyWindowKey(sorted[0]), "anthropic-1-seven_day");
    assert.equal(new Set(sorted.map(weeklyWindowKey)).size, sorted.length);
  });
});

describe("cycleReasonText", () => {
  it("names the contract's exclusion reasons plainly and never invents a provider-side cause", () => {
    assert.equal(cycleReasonText("counter_decreased"), "counter decreased mid-window");
    assert.equal(cycleReasonText("missing_tail"), "no sample within 3h of reset");
    assert.equal(cycleReasonText("collection_gap"), "collection gap");
    assert.equal(cycleReasonText("early_boundary_replacement"), "reset target replaced early");
    assert.equal(cycleReasonText("manual_reset"), "manual reset");
    assert.equal(cycleReasonText("unconfirmed_completion"), "rollover not confirmed");
    // Unknown reasons still read as words, not as raw enum tokens.
    assert.equal(cycleReasonText("some_new_reason"), "some new reason");
    assert.equal(cycleReasonText(""), "unspecified");
  });
});

describe("compactWeeklySummary", () => {
  it("gives the Now strip its two lines: current used/remaining and completed used/unused", () => {
    const compact = compactWeeklySummary(PAYLOAD);
    assert.deepEqual(compact.current, { usedText: "48.3%", complementLabel: "remaining", complementText: "51.7%", note: "3 accounts · 1 estimated" });
    assert.deepEqual(compact.completed, {
      usedText: "66.2%",
      complementLabel: "unused at reset",
      complementText: "33.8%",
      note: "3 accounts · 9 cycles · last 4 windows",
    });
  });
});

describe("isWeeklyUtilizationPayload", () => {
  it("accepts the contract shape and rejects anything the panel could not render", () => {
    assert.equal(isWeeklyUtilizationPayload(PAYLOAD), true);
    assert.equal(isWeeklyUtilizationPayload(null), false);
    assert.equal(isWeeklyUtilizationPayload({}), false);
    assert.equal(isWeeklyUtilizationPayload({ ...PAYLOAD, windows: "nope" }), false);
    assert.equal(isWeeklyUtilizationPayload({ ...PAYLOAD, current: { providers: null } }), false);
    assert.equal(isWeeklyUtilizationPayload({ ...PAYLOAD, completed: undefined }), false);
  });
});
