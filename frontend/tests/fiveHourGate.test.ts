import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  deriveCapacityAccounts,
  fiveHourHiddenAccounts,
  hidesFiveHour,
  isFiveHourQuotaName,
  quotaAccountKey,
} from "../src/lib/capacity";

// Keeper, 2026-09-25 (Discord unified usage ledger thread, msg
// 1553031185041461300): "I think we need more intelligence about when to
// show 5 hour cards. They are not useful if the weekly or monthly cap is
// hit, across all providers". Fixture shapes are the live 2026-09-25 rows:
// Kimi's week at 100 (provider_unit), OpenCode Go's month at 100
// (rate_limited) with its week at 42, Claude with a Fable model cap, Z.AI
// with its monthly web-search tool quota.

const NOW = Date.parse("2026-09-25T13:10:00Z");
const FRESH = "2026-09-25T13:05:00Z";
const OPTIONS = { now: NOW, maxAgeSeconds: 3 * 3600 };

function row(provider: string, quota: string, used: number | string, overrides: Record<string, unknown> = {}) {
  return {
    provider,
    provider_canonical: provider,
    harness: "h",
    observed_at: FRESH,
    quota_name: quota,
    quota_scope: "account",
    window_kind: quota === "five_hour" ? "rolling" : "fixed",
    unit: "percent",
    used_value: String(used),
    limit_value: "100",
    remaining_value: String(100 - Number(used)),
    resets_at: "2026-09-27T02:45:00Z",
    measurement_confidence: "exact",
    provider_state: null,
    coverage_status: null,
    provider_account_index: 1,
    provider_account_count: 1,
    ...overrides,
  };
}

const LIVE = [
  row("kimi-k3-coding", "week", 100, { unit: "provider_unit" }),
  row("kimi-k3-coding", "five_hour", 0, { unit: "provider_unit" }),
  row("opencode-go", "week", 42, { provider_state: "ok" }),
  row("opencode-go", "month", 100, { provider_state: "rate_limited", resets_at: "2026-10-08T04:51:11Z" }),
  row("opencode-go", "five_hour", 0, { provider_state: "ok" }),
  row("anthropic", "seven_day", 32, { provider_account_count: 2 }),
  row("anthropic", "seven_day_fable", 100, { provider_account_count: 2 }),
  row("anthropic", "five_hour", 0, { provider_account_count: 2 }),
  row("anthropic", "seven_day", 15, { provider_account_index: 2, provider_account_count: 2 }),
  row("anthropic", "five_hour", 3, { provider_account_index: 2, provider_account_count: 2 }),
  row("z-ai-glm", "week", 29),
  row("z-ai-glm", "web_search_month", 4000, { unit: "requests", limit_value: "4000", remaining_value: "0" }),
  row("z-ai-glm", "five_hour", 1),
];

describe("five-hour windows under a spent weekly or monthly cap", () => {
  it("names five-hour windows", () => {
    assert.equal(isFiveHourQuotaName("five_hour"), true);
    assert.equal(isFiveHourQuotaName("five_hour_opus"), true);
    assert.equal(isFiveHourQuotaName("seven_day"), false);
    assert.equal(isFiveHourQuotaName(null), false);
  });

  it("hides them for a spent week or month, on every provider, and nowhere else", () => {
    const hidden = fiveHourHiddenAccounts(LIVE, OPTIONS);
    assert.deepEqual(Object.fromEntries(hidden), { "kimi-k3-coding:1": "week", "opencode-go:1": "month" });
    // A model cap (Fable) and a tool quota (web search) stop one model or
    // one tool, not the account.
    assert.equal(hidden.has("anthropic:1"), false);
    assert.equal(hidden.has("z-ai-glm:1"), false);
    const shown = LIVE.filter((entry) => !hidesFiveHour(hidden, entry)).map((entry) => `${quotaAccountKey(entry)} ${entry.quota_name}`);
    assert.deepEqual(shown.filter((name) => name.endsWith("five_hour")), [
      "anthropic:1 five_hour", "anthropic:2 five_hour", "z-ai-glm:1 five_hour",
    ]);
    // Only five-hour windows are hidden; the spent cap itself stays.
    assert.ok(shown.includes("opencode-go:1 month") && shown.includes("kimi-k3-coding:1 week"));
  });

  it("brings them back once the cap reading is stale or its reset has passed", () => {
    const staleWeek = [
      row("kimi-k3-coding", "week", 100, { observed_at: "2026-09-25T08:00:00Z" }),
      row("kimi-k3-coding", "five_hour", 0),
    ];
    assert.equal(fiveHourHiddenAccounts(staleWeek, OPTIONS).size, 0);
    const resetPassed = [
      row("kimi-k3-coding", "week", 100, { resets_at: "2026-09-25T13:08:00Z" }),
      row("kimi-k3-coding", "five_hour", 0),
    ];
    assert.equal(fiveHourHiddenAccounts(resetPassed, OPTIONS).size, 0);
    assert.equal(fiveHourHiddenAccounts([row("kimi-k3-coding", "week", 99), row("kimi-k3-coding", "five_hour", 0)], OPTIONS).size, 0);
  });

  it("keeps the second account of a provider separate", () => {
    const rows = [
      row("anthropic", "seven_day", 100, { provider_account_index: 2, provider_account_count: 2 }),
      row("anthropic", "five_hour", 40, { provider_account_index: 2, provider_account_count: 2 }),
      row("anthropic", "seven_day", 30, { provider_account_count: 2 }),
      row("anthropic", "five_hour", 20, { provider_account_count: 2 }),
    ];
    const hidden = fiveHourHiddenAccounts(rows, OPTIONS);
    assert.deepEqual([...hidden.keys()], ["anthropic:2"]);
    // A quota-burn series carries the same fields and resolves the same key.
    assert.equal(hidesFiveHour(hidden, { provider: "anthropic", quota_name: "five_hour", provider_account_index: 2 }), true);
    assert.equal(hidesFiveHour(hidden, { provider: "anthropic", quota_name: "five_hour", provider_account_index: 1 }), false);
  });

  it("drops the window from the capacity row and binds on the spent cap", () => {
    const accounts = deriveCapacityAccounts(LIVE, OPTIONS);
    const byKey = new Map(accounts.map((account) => [account.key, account]));
    const go = byKey.get("opencode-go:1")!;
    assert.deepEqual(go.windows.map((window) => window.quotaName), ["month", "week"]);
    assert.equal(go.hiddenFiveHour, 1);
    assert.equal(go.state, "exhausted");
    assert.equal(go.binding && go.binding.quotaName, "month");
    const kimi = byKey.get("kimi-k3-coding:1")!;
    assert.deepEqual(kimi.windows.map((window) => window.quotaName), ["week"]);
    assert.equal(kimi.state, "exhausted");
    const claude = byKey.get("anthropic:1")!;
    assert.equal(claude.hiddenFiveHour, 0);
    assert.ok(claude.windows.some((window) => window.quotaName === "five_hour"));
  });
});
