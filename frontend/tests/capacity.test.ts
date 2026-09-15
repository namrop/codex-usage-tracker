import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { capacityWindowKind, confidenceText, deriveCapacityAccounts, deriveCapacityWindow } from "../src/lib/capacity";

// Fixture shapes mirror /api/subscriptions `latest` rows. The numbers here
// are the shapes the 2026-09-14 live review surfaced (a Fable-only cap at
// 100%, a second Claude account at 6%, a Kimi week at 92%, a Z.AI search
// month at its cap, an estimated OpenCode Go window), but nothing in the
// helper knows these values — they are inputs.

const NOW = Date.parse("2026-09-14T20:00:00Z");
const FRESH = "2026-09-14T19:55:00Z";
const OPTIONS = { now: NOW, maxAgeSeconds: 3 * 3600 };

function row(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    provider: "anthropic",
    provider_canonical: "anthropic",
    harness: "claude_code",
    observed_at: FRESH,
    quota_name: "seven_day",
    quota_scope: "account",
    window_kind: "rolling",
    unit: "percent",
    used_value: "10",
    limit_value: "100",
    remaining_value: "90",
    resets_at: "2026-09-18T00:00:00Z",
    measurement_confidence: "exact",
    provider_state: null,
    coverage_status: null,
    provider_account_index: 1,
    provider_account_count: 1,
    ...overrides,
  };
}

describe("capacityWindowKind", () => {
  it("separates model windows, model-specific caps, tool quotas and balances", () => {
    assert.equal(capacityWindowKind(row({ quota_name: "seven_day" })), "model");
    assert.equal(capacityWindowKind(row({ quota_name: "five_hour" })), "model");
    assert.equal(capacityWindowKind(row({ quota_name: "seven_day_fable" })), "model_specific");
    assert.equal(capacityWindowKind(row({ quota_name: "web_search_month", unit: "requests", window_kind: "fixed" })), "tool");
    assert.equal(capacityWindowKind(row({ quota_name: "credit_balance", unit: "credits", window_kind: "lifetime" })), "balance");
    // A credits row on an "unknown" window with no cap at all is a balance;
    // with a provider-reported limit it is a key allowance, never a balance
    // and never a model window (OpenRouter api_key_quota).
    assert.equal(capacityWindowKind(row({ quota_name: "api_key_quota", unit: "credits", window_kind: "unknown", used_value: null, limit_value: null, remaining_value: "12" })), "balance");
    assert.equal(capacityWindowKind(row({ quota_name: "api_key_quota", unit: "credits", window_kind: "unknown", used_value: null, limit_value: "100", remaining_value: "90" })), "allowance");
    assert.equal(capacityWindowKind(row({ quota_name: "api_key_quota", unit: "credits", window_kind: "unknown", used_value: "50", limit_value: "100" })), "allowance");
    assert.equal(capacityWindowKind(row({ quota_name: "account_balance", unit: "usd", window_kind: "lifetime", used_value: null, limit_value: null })), "balance");
    // An estimated dollar-denominated rolling window with a cap is a quota,
    // not a balance (OpenCode Go).
    assert.equal(capacityWindowKind(row({ quota_name: "five_hour", unit: "usd", window_kind: "rolling", used_value: "4", limit_value: "20" })), "model");
  });

  it("classifies a request-counted search limit on an unknown window as a tool quota, not a balance", () => {
    // The shape search_events.py writes for a rate-limited backend: no
    // used/limit ratio, remaining 0, window unknown, unit requests.
    const hermesSearch = row({
      provider: "brave", provider_canonical: "brave", harness: "hermes", quota_name: "web_search_limit",
      window_kind: "unknown", unit: "requests", used_value: null, limit_value: null, remaining_value: "0",
      measurement_confidence: "estimated", resets_at: "2026-09-15T00:00:00Z",
    });
    assert.equal(capacityWindowKind(hermesSearch), "tool");
    assert.equal(capacityWindowKind(row({ quota_name: "web_search_limit", window_kind: "unknown", unit: "requests", used_value: null, limit_value: null })), "tool");
    assert.equal(capacityWindowKind(row({ quota_name: "extract_limit", window_kind: "unknown", unit: "calls", used_value: null, limit_value: null })), "tool");
  });
});

describe("tool quota without a cap denominator", () => {
  const hermesSearch = row({
    provider: "brave", provider_canonical: "brave", harness: "hermes", quota_name: "web_search_limit",
    window_kind: "unknown", unit: "requests", used_value: null, limit_value: null, remaining_value: "0",
    measurement_confidence: "estimated", resets_at: "2026-09-15T00:00:00Z",
  });

  it("omits the percentage, keeps the zero-remaining constraint and never reads as a balance", () => {
    const window = deriveCapacityWindow(hermesSearch, OPTIONS, "brave", 1);
    assert.equal(window.kind, "tool");
    assert.equal(window.usedPct, null, "no denominator, no percentage");
    assert.equal(window.remainingText, "0 requests left");
    assert.equal(window.atCap, true, "zero remaining constrains the tool");
    assert.doesNotMatch(window.remainingText, /balance/);
  });

  it("constrains the tool without exhausting the account", () => {
    const [account] = deriveCapacityAccounts([hermesSearch], OPTIONS);
    assert.equal(account.state, "constrained");
    assert.equal(account.binding?.kind, "tool");
    assert.match(account.headline, /tool quota exhausted/);
    assert.doesNotMatch(account.headline, /no usable balance/);
    assert.match(String(account.note), /Model inference is unaffected/);
  });
});

describe("OpenRouter key allowance", () => {
  // The sanitized live API row: used_value is the key's LIFETIME usage;
  // limit/remaining are the current allowance.
  const allowance = row({
    provider: "openrouter", provider_canonical: "openrouter", harness: "provider_api", quota_name: "api_key_quota",
    unit: "credits", window_kind: "unknown", used_value: "265.172541939", limit_value: "50", remaining_value: "50", resets_at: null,
  });
  const emptyBalance = row({
    provider: "openrouter", provider_canonical: "openrouter", harness: "provider_api", quota_name: "credit_balance",
    unit: "credits", window_kind: "lifetime", used_value: "265.172541939", limit_value: "265.172541939", remaining_value: "0", resets_at: null,
  });

  it("derives the meter from provider-reported remaining/limit, never lifetime spend", () => {
    const window = deriveCapacityWindow(allowance, OPTIONS, "openrouter", 1);
    assert.equal(window.kind, "allowance");
    assert.equal(window.usedPct, 0);
    assert.equal(window.atCap, false);
    assert.equal(window.remainingText, "50 of 50 credits left");
    // Raw source fields stay on the window for diagnostics.
    assert.equal(window.usedValue, 265.172541939);
    assert.equal(window.limitValue, 50);
    assert.equal(window.remainingValue, 50);
    const half = deriveCapacityWindow({ ...allowance, remaining_value: "20" }, OPTIONS, "openrouter", 1);
    assert.equal(half.usedPct, 60);
    assert.equal(half.remainingText, "20 of 50 credits left");
    const spent = deriveCapacityWindow({ ...allowance, remaining_value: "0" }, OPTIONS, "openrouter", 1);
    assert.equal(spent.usedPct, 100);
    assert.equal(spent.atCap, true);
    // No remaining figure: nothing is inferred from the lifetime usage.
    const noRemaining = deriveCapacityWindow({ ...allowance, remaining_value: null }, OPTIONS, "openrouter", 1);
    assert.equal(noRemaining.usedPct, null);
    assert.equal(noRemaining.atCap, false);
  });

  it("does not restore a missing credit balance", () => {
    const [openrouter] = deriveCapacityAccounts([allowance, emptyBalance], OPTIONS);
    assert.equal(openrouter.state, "exhausted");
    assert.equal(openrouter.binding?.quotaName, "credit_balance");
    assert.match(openrouter.headline, /no usable balance/);
    assert.match(String(openrouter.note), /api key quota 50 of 50 credits left/);
    assert.match(String(openrouter.note), /allowance does not restore/);
    assert.equal(openrouter.windows[1].kind, "allowance");
  });

  it("constrains the key when the allowance is spent while credit remains", () => {
    const [openrouter] = deriveCapacityAccounts([
      { ...allowance, remaining_value: "0" },
      { ...emptyBalance, remaining_value: "12.5" },
    ], OPTIONS);
    assert.equal(openrouter.state, "constrained");
    assert.equal(openrouter.binding?.kind, "allowance");
    assert.match(openrouter.headline, /key allowance/);
    assert.doesNotMatch(openrouter.headline, /no usable balance/);
  });

  it("leaves other providers' accounting untouched", () => {
    const go = deriveCapacityWindow(row({
      provider: "opencode-go", provider_canonical: "opencode-go", harness: "opencode", unit: "usd",
      used_value: "4", limit_value: "20", remaining_value: "16", measurement_confidence: "estimated", coverage_status: "partial",
    }), OPTIONS, "opencode-go", 1);
    assert.equal(go.kind, "model");
    assert.equal(go.usedPct, 20);
    const deepseek = deriveCapacityWindow(row({
      provider: "deepseek", provider_canonical: "deepseek", harness: "provider_api", quota_name: "account_balance",
      unit: "usd", window_kind: "lifetime", used_value: null, limit_value: null, remaining_value: "3.5", resets_at: null,
    }), OPTIONS, "deepseek", 1);
    assert.equal(deepseek.kind, "balance");
    assert.equal(deepseek.remainingText, "$3.50 balance");
  });
});

describe("confidence", () => {
  const base = { provider: "opencode-go", provider_canonical: "opencode-go", harness: "opencode", unit: "usd", used_value: "4", limit_value: "20", remaining_value: "16" };

  it("gives partial coverage the backend's precedence and still names the measurement", () => {
    const exactPartial = deriveCapacityWindow(row({ ...base, measurement_confidence: "exact", coverage_status: "partial" }), OPTIONS, "opencode-go", 1);
    assert.equal(exactPartial.confidence, "partial");
    assert.equal(exactPartial.measured, "exact");
    assert.match(confidenceText(exactPartial), /Partial/);
    assert.match(confidenceText(exactPartial), /exact/i);
    assert.notEqual(confidenceText(exactPartial), "Exact");

    const estimatedPartial = deriveCapacityWindow(row({ ...base, measurement_confidence: "estimated", coverage_status: "partial" }), OPTIONS, "opencode-go", 1);
    assert.equal(estimatedPartial.confidence, "partial");
    assert.equal(estimatedPartial.measured, "estimated");
    assert.match(confidenceText(estimatedPartial), /Partial/);
    assert.match(confidenceText(estimatedPartial), /estimated/i);
  });

  it("keeps plain exact and estimated readings as they are", () => {
    const exact = deriveCapacityWindow(row({}), OPTIONS, "anthropic", 1);
    assert.equal(exact.confidence, "exact");
    assert.equal(exact.measured, "exact");
    assert.equal(confidenceText(exact), "Exact");
    const estimated = deriveCapacityWindow(row({ ...base, measurement_confidence: "estimated", coverage_status: null }), OPTIONS, "opencode-go", 1);
    assert.equal(estimated.confidence, "estimated");
    assert.equal(confidenceText(estimated), "Estimated");
    const unknown = deriveCapacityWindow(row({ measurement_confidence: null }), OPTIONS, "anthropic", 1);
    assert.equal(unknown.confidence, "unknown");
    assert.equal(confidenceText(unknown), "Unknown");
  });
});

describe("deriveCapacityWindow", () => {
  it("keeps raw units: percent, dollars of a cap, request counts and balances never mix", () => {
    const percent = deriveCapacityWindow(row({ used_value: "41", remaining_value: "59" }), OPTIONS, "anthropic", 1);
    assert.equal(percent.remainingText, "59% left");
    assert.equal(percent.usedPct, 41);

    const dollars = deriveCapacityWindow(row({
      provider: "opencode-go", provider_canonical: "opencode-go", harness: "opencode", unit: "usd",
      used_value: "4", limit_value: "20", remaining_value: "16", measurement_confidence: "estimated", coverage_status: "partial",
    }), OPTIONS, "opencode-go", 1);
    assert.equal(dollars.remainingText, "$16.00 of $20.00 left");
    assert.equal(dollars.usedPct, 20);
    assert.equal(dollars.confidence, "partial", "partial coverage outranks the measurement, as the backend rules");
    assert.equal(dollars.measured, "estimated");

    const requests = deriveCapacityWindow(row({
      provider: "z-ai", provider_canonical: "z-ai-glm", harness: "z_ai_api", quota_name: "web_search_month",
      unit: "requests", window_kind: "fixed", used_value: "4000", limit_value: "4000", remaining_value: "0",
      resets_at: "2026-10-01T00:00:00Z",
    }), OPTIONS, "z-ai-glm", 1);
    assert.equal(requests.kind, "tool");
    assert.equal(requests.remainingText, "0 of 4000 requests left");
    assert.equal(requests.atCap, true);

    const balance = deriveCapacityWindow(row({
      provider: "openrouter", provider_canonical: "openrouter", harness: "provider_api", quota_name: "credit_balance",
      unit: "credits", window_kind: "lifetime", used_value: "10", limit_value: "10", remaining_value: "0", resets_at: null,
    }), OPTIONS, "openrouter", 1);
    assert.equal(balance.kind, "balance");
    assert.equal(balance.usedPct, null, "a balance is never a percentage");
    assert.equal(balance.remainingText, "0 credits balance");
    assert.equal(balance.atCap, true);
  });

  it("marks stale readings and readings whose window reset after the sample", () => {
    const old = deriveCapacityWindow(row({ observed_at: "2026-09-14T15:00:00Z" }), OPTIONS, "anthropic", 1);
    assert.equal(old.stale, true);
    assert.equal(old.staleReason, "observation_stale");
    const rolled = deriveCapacityWindow(row({
      quota_name: "five_hour", observed_at: "2026-09-14T18:30:00Z", resets_at: "2026-09-14T19:00:00Z",
    }), OPTIONS, "anthropic", 1);
    assert.equal(rolled.stale, true);
    assert.equal(rolled.staleReason, "reset_passed_since_observation");
  });

  it("joins the burn projection by canonical identity and account ordinal, never by position", () => {
    const burnSeries = [
      { provider: "anthropic", quota_name: "seven_day", harness: "claude_code", provider_account_index: 2, provider_account_count: 2, projection_status: "resets_first", projection_reason: "reset_before_empty_at_observed_rate", burn_rate_pct_per_hour: 1.2 },
      { provider: "anthropic", quota_name: "seven_day", harness: "claude_code", provider_account_index: 1, provider_account_count: 2, projection_status: "projected_before_reset", projection_reason: "empty_before_reset_at_observed_rate", projected_exhaustion_at: "2026-09-15T02:00:00Z", burn_rate_pct_per_hour: 9.5 },
    ];
    const second = deriveCapacityWindow(row({ provider_account_index: 2, provider_account_count: 2 }), { ...OPTIONS, burnSeries }, "anthropic", 2);
    assert.equal(second.projection?.status, "resets_first");
    const first = deriveCapacityWindow(row({ provider_account_index: 1, provider_account_count: 2 }), { ...OPTIONS, burnSeries }, "anthropic", 1);
    assert.equal(first.projection?.status, "projected_before_reset");
    assert.equal(first.projection?.exhaustionAt, "2026-09-15T02:00:00Z");
  });
});

describe("deriveCapacityAccounts", () => {
  const claudeOne = [
    row({ quota_name: "seven_day_fable", used_value: "100", remaining_value: "0", provider_account_index: 1, provider_account_count: 2 }),
    row({ quota_name: "seven_day", used_value: "41", remaining_value: "59", provider_account_index: 1, provider_account_count: 2 }),
    row({ quota_name: "five_hour", used_value: "12", remaining_value: "88", resets_at: "2026-09-14T23:00:00Z", provider_account_index: 1, provider_account_count: 2 }),
  ];
  const claudeTwo = [
    row({ quota_name: "seven_day", used_value: "6", remaining_value: "94", provider_account_index: 2, provider_account_count: 2 }),
    row({ quota_name: "five_hour", used_value: "0", remaining_value: "100", resets_at: "2026-09-14T23:00:00Z", provider_account_index: 2, provider_account_count: 2 }),
  ];

  it("labels accounts consistently from the API ordinal regardless of row order", () => {
    const accounts = deriveCapacityAccounts([...claudeTwo, ...claudeOne], OPTIONS);
    assert.deepEqual(accounts.map((account) => account.label), ["Claude 1", "Claude 2"]);
    assert.deepEqual(accounts.map((account) => account.accountIndex), [1, 2]);
  });

  it("never calls an account exhausted for a model-specific cap alone, and names what binds", () => {
    const [one, two] = deriveCapacityAccounts([...claudeOne, ...claudeTwo], OPTIONS);
    assert.equal(one.state, "constrained");
    assert.equal(one.binding?.quotaName, "seven_day_fable");
    assert.equal(one.binding?.kind, "model_specific");
    assert.match(one.headline, /Fable/);
    assert.match(one.headline, /not exhausted/);
    assert.match(String(one.note), /seven day 41% used/);
    // Binding window first, then the rest by pressure.
    assert.deepEqual(one.windows.map((window) => window.quotaName), ["seven_day_fable", "seven_day", "five_hour"]);
    assert.equal(two.state, "available");
    assert.equal(two.binding?.quotaName, "seven_day");
    assert.equal(two.headline, "seven day 6% used.");
  });

  it("treats a general window at its cap as exhausted and a near-cap week as constrained", () => {
    const kimi = deriveCapacityAccounts([
      row({ provider: "kimi-coding", provider_canonical: "kimi-k3-coding", harness: "kimi_code", quota_name: "week", unit: "provider_unit", used_value: "92", remaining_value: "8", window_kind: "fixed" }),
    ], OPTIONS);
    assert.equal(kimi[0].label, "Kimi K3");
    assert.equal(kimi[0].state, "constrained");
    assert.equal(kimi[0].windows[0].remainingText, "8% left");
    const exhausted = deriveCapacityAccounts([
      row({ quota_name: "five_hour", used_value: "100", remaining_value: "0" }),
    ], OPTIONS);
    assert.equal(exhausted[0].state, "exhausted");
  });

  it("keeps a tool quota distinct from model capacity", () => {
    const zai = deriveCapacityAccounts([
      row({ provider: "z-ai", provider_canonical: "z-ai-glm", harness: "z_ai_api", quota_name: "web_search_month", unit: "requests", window_kind: "fixed", used_value: "4000", limit_value: "4000", remaining_value: "0", resets_at: "2026-10-01T00:00:00Z" }),
      row({ provider: "z-ai", provider_canonical: "z-ai-glm", harness: "z_ai_api", quota_name: "week", used_value: "30", remaining_value: "70" }),
      row({ provider: "z-ai", provider_canonical: "z-ai-glm", harness: "z_ai_api", quota_name: "five_hour", used_value: "5", remaining_value: "95", resets_at: "2026-09-14T23:00:00Z" }),
    ], OPTIONS);
    assert.equal(zai[0].label, "Z.AI");
    assert.equal(zai[0].state, "constrained");
    assert.equal(zai[0].binding?.kind, "tool");
    assert.match(zai[0].headline, /tool quota exhausted/);
    assert.match(String(zai[0].note), /Model inference is unaffected/);
    assert.match(String(zai[0].note), /week 30% used/);
  });

  it("lets a zero balance bind even when an API-key allowance remains", () => {
    const [openrouter] = deriveCapacityAccounts([
      row({ provider: "openrouter", provider_canonical: "openrouter", harness: "provider_api", quota_name: "api_key_quota", unit: "credits", window_kind: "unknown", used_value: "50", limit_value: "100", remaining_value: "50", resets_at: null }),
      row({ provider: "openrouter", provider_canonical: "openrouter", harness: "provider_api", quota_name: "credit_balance", unit: "credits", window_kind: "lifetime", used_value: "10", limit_value: "10", remaining_value: "0", resets_at: null }),
    ], OPTIONS);
    assert.equal(openrouter.state, "exhausted");
    assert.equal(openrouter.binding?.quotaName, "credit_balance");
    assert.match(openrouter.headline, /no usable balance/);
    assert.match(String(openrouter.note), /api key quota 50 of 100 credits left/);
    assert.equal(openrouter.windows[1].kind, "allowance", "a key allowance is a cap, not a balance");
    assert.match(String(openrouter.note), /does not restore/);
    assert.equal(openrouter.windows.length, 2, "both windows stay visible");
  });

  it("carries estimated confidence and never upgrades it", () => {
    const [go] = deriveCapacityAccounts([
      row({ provider: "opencode-go", provider_canonical: "opencode-go", harness: "opencode", quota_name: "five_hour", unit: "usd", used_value: "4", limit_value: "20", remaining_value: "16", measurement_confidence: "estimated", coverage_status: "partial", resets_at: "2026-09-14T23:00:00Z" }),
    ], OPTIONS);
    assert.equal(go.label, "OpenCode Go");
    assert.equal(go.windows[0].confidence, "partial");
    assert.equal(go.windows[0].measured, "estimated");
    assert.notEqual(go.windows[0].measured, "exact");
    assert.notEqual(confidenceText(go.windows[0]), "Exact");
  });

  it("does not assert capacity from stale rows and orders exhausted accounts first", () => {
    const accounts = deriveCapacityAccounts([
      row({ provider: "openai-codex", provider_canonical: "openai-codex", harness: "hermes", quota_name: "week", used_value: "100", remaining_value: "0", observed_at: "2026-09-14T12:00:00Z" }),
      row({ quota_name: "five_hour", used_value: "100", remaining_value: "0", resets_at: "2026-09-14T23:00:00Z" }),
      row({ provider: "kimi-coding", provider_canonical: "kimi-k3-coding", harness: "kimi_code", quota_name: "week", unit: "provider_unit", used_value: "20", remaining_value: "80" }),
    ], OPTIONS);
    assert.deepEqual(accounts.map((account) => [account.label, account.state]), [
      ["Claude", "exhausted"],
      ["Kimi K3", "available"],
      ["Codex", "stale"],
    ]);
    const codex = accounts[2];
    assert.match(codex.headline, /cannot be asserted/);
    assert.equal(codex.windows[0].atCap, true, "the reading is still shown");
  });

  it("returns nothing for no rows and unknown for an account without windows", () => {
    assert.deepEqual(deriveCapacityAccounts([], OPTIONS), []);
    assert.deepEqual(deriveCapacityAccounts(undefined, OPTIONS), []);
  });
});
