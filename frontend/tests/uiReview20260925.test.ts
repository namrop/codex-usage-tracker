import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { capacityRoom, deriveCapacityAccounts, deriveCapacityWindow } from "../src/lib/capacity";
import { PROVIDER_ALIASES, accountDisplayLabel, providerDisplayName } from "../src/lib/providers";

// Keeper-approved UI review, 2026-09-25 (Discord unified usage ledger thread,
// msg 1553037883747598450: "one, two, three, four, yeah five, six ... eight
// and nine are good too"). Fixture shapes are the live rows of that morning.

const NOW = Date.parse("2026-09-25T13:25:00Z");
const FRESH = "2026-09-25T13:05:00Z";
const OPTIONS = { now: NOW, maxAgeSeconds: 3 * 3600 };

function row(provider: string, quota: string, used: number | string | null, overrides: Record<string, unknown> = {}) {
  return {
    provider,
    provider_canonical: provider,
    harness: "h",
    observed_at: FRESH,
    quota_name: quota,
    quota_scope: "account",
    window_kind: "rolling",
    unit: "percent",
    used_value: used === null ? null : String(used),
    limit_value: "100",
    remaining_value: used === null ? null : String(100 - Number(used)),
    resets_at: "2026-10-02T00:00:00Z",
    measurement_confidence: "exact",
    provider_state: null,
    coverage_status: null,
    provider_account_index: 1,
    provider_account_count: 1,
    ...overrides,
  };
}

const OPENROUTER = [
  row("openrouter", "credit_balance", "304.726169184", {
    unit: "credits", window_kind: "lifetime", limit_value: "305", remaining_value: "0.273830816", resets_at: null,
  }),
  row("openrouter", "api_key_quota", "304.726169184", {
    unit: "credits", window_kind: "unknown", limit_value: "50", remaining_value: "50", resets_at: null,
  }),
];

describe("item 2: a balance smaller than the key allowance binds", () => {
  it("reads OpenRouter's 0.27 credits, not the 50-credit allowance", () => {
    const [account] = deriveCapacityAccounts(OPENROUTER, OPTIONS);
    assert.equal(account.binding && account.binding.quotaName, "credit_balance");
    assert.equal(account.state, "constrained");
    assert.match(account.headline, /^0\.27 credits balance — less than the key allowance \(50 of 50 credits left\)/);
  });

  it("leaves a balance larger than the allowance alone", () => {
    const rows = [
      { ...OPENROUTER[0], used_value: "5", remaining_value: "300" },
      OPENROUTER[1],
    ];
    const [account] = deriveCapacityAccounts(rows, OPTIONS);
    assert.equal(account.binding && account.binding.quotaName, "api_key_quota");
    assert.equal(account.state, "available");
  });
});

describe("item 3: pace mark on weekly windows", () => {
  it("is the share of the week gone, from the reset back one week", () => {
    const window = deriveCapacityWindow(row("anthropic", "seven_day", 33), OPTIONS, "anthropic", 1);
    // 13h25m into a week that resets 2026-10-02T00:00Z.
    assert.ok(window.elapsedPct !== null && Math.abs(window.elapsedPct - (13.4167 / 168) * 100) < 0.01);
  });

  it("uses the provider's own window start when reported, and skips non-weekly windows", () => {
    const started = deriveCapacityWindow(
      row("z-ai-glm", "week", 29, { window_started_at: "2026-09-24T19:09:48Z", resets_at: "2026-10-01T19:09:48Z" }),
      OPTIONS, "z-ai-glm", 1,
    );
    assert.ok(started.elapsedPct !== null && started.elapsedPct > 10 && started.elapsedPct < 11);
    assert.equal(deriveCapacityWindow(row("anthropic", "five_hour", 8, { resets_at: "2026-09-25T15:00:00Z" }), OPTIONS, "anthropic", 1).elapsedPct, null);
    assert.equal(deriveCapacityWindow(row("anthropic", "seven_day", 33, { resets_at: "2026-09-25T13:00:00Z" }), OPTIONS, "anthropic", 1).elapsedPct, null);
  });
});

describe("item 4: the room line", () => {
  const when = (at: number) => new Date(at).toISOString().slice(5, 16);
  const rows = [
    row("anthropic", "seven_day", 33, { provider_account_count: 2 }),
    row("anthropic", "seven_day", 15, { provider_account_index: 2, provider_account_count: 2 }),
    row("z-ai-glm", "week", 29, { resets_at: "2026-10-01T19:09:48Z" }),
    row("openai-codex", "week", 97, { resets_at: "2026-09-25T19:08:48Z" }),
    row("kimi-k3-coding", "week", 100, { unit: "provider_unit", resets_at: "2026-09-27T02:45:51Z" }),
    row("opencode-go", "month", 100, { provider_state: "rate_limited", resets_at: "2026-10-08T04:51:11Z", window_kind: "fixed" }),
    row("deepseek", "account_balance", null, { unit: "usd", window_kind: "lifetime", limit_value: null, remaining_value: "18.91", resets_at: null }),
    ...OPENROUTER,
  ];
  const burnSeries = [{
    provider: "openai-codex", quota_name: "week", harness: "h", provider_account_index: 1,
    projection_status: "projected_before_reset", projection_reason: "empty_before_reset_at_observed_rate",
    projected_exhaustion_at: "2026-09-25T16:00:01Z", burn_rate_pct_per_hour: 1,
  }];

  it("names room, low and out accounts with their numbers and times", () => {
    const room = capacityRoom(deriveCapacityAccounts(rows, { ...OPTIONS, burnSeries }), NOW, when);
    assert.deepEqual(room.room, ["Claude 2 85%", "Z.AI 71%", "Claude 1 67%", "DeepSeek $18.91"]);
    assert.equal(room.low.length, 2);
    assert.ok(room.low.includes("Codex runs out ~09-25T16:00, resets 09-25T19:08"));
    assert.ok(room.low.includes("OpenRouter 0.27 credits left"));
    assert.ok(room.out.includes("Kimi K3 until 09-27T02:45"));
    assert.ok(room.out.includes("OpenCode Go until 10-08T04:51"));
    assert.deepEqual(room.stale, []);
  });
});

describe("item 5: one name per provider", () => {
  it("maps raw and canonical ids to the same short name", () => {
    for (const [raw, name] of [
      ["zai", "Z.AI"], ["z-ai-glm", "Z.AI"], ["kimi-coding", "Kimi K3"], ["kimi-k3-coding", "Kimi K3"],
      ["openai-codex", "Codex"], ["anthropic", "Claude"], ["opencode-go", "OpenCode Go"],
      ["custom:omlx", "Local (acubens)"], ["custom:gemma-4-moe-it", "Local (acubens)"], ["custom:minicpm5", "Local (acubens)"],
      ["moa", "MoA council"], ["openrouter", "OpenRouter"], ["deepseek", "DeepSeek"],
    ] as const) {
      assert.equal(providerDisplayName(raw), name, raw);
    }
    assert.equal(PROVIDER_ALIASES["custom:omlx"], "local-acubens");
    assert.equal(accountDisplayLabel({ provider_canonical: "openrouter" }), "OpenRouter");
    assert.equal(accountDisplayLabel({ provider_canonical: "anthropic", provider_account_index: 2, provider_account_count: 2 }), "Claude 2");
  });
});
