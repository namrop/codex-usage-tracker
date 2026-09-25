import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { deriveSubscriptionValue, listPriceBySubscription, money } from "../src/lib/subscriptionValue";

// Keeper, 2026-09-25 (msg 1553037883747598450): "we should record what I pay
// each month per subscription". Fees come from /api/subscription-fees; list
// prices from the unified window's own rows.

const UNIFIED = {
  by_provider: [
    { provider: "anthropic", list_price_usd: "1600.00" },
    { provider: "openai-codex", list_price_usd: "2064.83" },
    { provider: "kimi-k3-coding", list_price_usd: "70.89" },
  ],
  by_provider_model: [
    { provider_canonical: "anthropic", provider_account_index: 1, list_price_usd: "1057.63" },
    { provider_canonical: "anthropic", provider_account_index: 1, list_price_usd: "334.69" },
    { provider_canonical: "anthropic", provider_account_index: 2, list_price_usd: "200.00" },
    // Account not recorded: pooled in the provider total, attributed to neither account.
    { provider_canonical: "anthropic", provider_account_index: null, list_price_usd: "7.68" },
    { provider_canonical: "openai-codex", provider_account_index: null, list_price_usd: "2064.83" },
  ],
};

const FEES = {
  subscriptions: [
    { subscription: "claude-1", provider: "anthropic", provider_account_index: 1, label: "Claude 1", plan: "Claude Max 20x",
      fee_per_week: "46.67", fee_per_month: "202.90", currency: "USD", charged_at: "2026-09-03T00:00:00Z",
      billing_period_end: "2026-10-03T00:00:00Z", period_current: true },
    { subscription: "codex", provider: "openai-codex", provider_account_index: null, label: "Codex", plan: "ChatGPT Pro",
      fee_per_week: "46.15", fee_per_month: "200.00", currency: "USD", fee_source: "stated", stated_at: "2026-09-25T00:00:00Z",
      charged_at: null, billing_period_end: null, period_current: null },
    { subscription: "opencode-go", provider: "opencode-go", provider_account_index: null, label: "OpenCode Go", plan: "OpenCode Go",
      fee_per_week: "2.31", fee_per_month: "10.00", currency: "USD", charged_at: "2026-09-08T00:00:00Z",
      billing_period_end: "2026-09-08T00:00:00Z", period_current: false },
  ],
};

describe("subscription value", () => {
  it("attributes Claude list price by account and others by provider", () => {
    const totals = listPriceBySubscription(UNIFIED);
    assert.equal(totals.get("anthropic:1")?.toFixed(2), "1392.32");
    assert.equal(totals.get("anthropic:2")?.toFixed(2), "200.00");
    assert.equal(totals.get("openai-codex"), 2064.83);
    assert.equal(totals.has("anthropic"), false);
  });

  it("joins the fee to the same week's list price", () => {
    const rows = deriveSubscriptionValue(FEES, UNIFIED);
    const byKey = new Map(rows.map((row) => [row.key, row]));
    assert.equal(byKey.get("claude-1")!.multiple!.toFixed(1), "29.8");
    assert.equal(byKey.get("codex")!.listPriceWeek, 2064.83);
    // No traffic for a subscription is $0 of list price, not missing.
    assert.equal(byKey.get("opencode-go")!.listPriceWeek, 0);
    assert.equal(byKey.get("opencode-go")!.periodCurrent, false);
    // Without the unified window there is no list-price side at all.
    assert.equal(deriveSubscriptionValue(FEES, null)[0].listPriceWeek, null);
    assert.equal(money(1392.315), "$1,392.32");
  });

  it("marks a stated fee (keeper msg 1553054862856556656) and keeps receipts as receipts", () => {
    const byKey = new Map(deriveSubscriptionValue(FEES, UNIFIED).map((row) => [row.key, row]));
    const codex = byKey.get("codex")!;
    assert.equal(codex.feeSource, "stated");
    assert.equal(codex.chargedAt, null);
    assert.equal(codex.statedAt, "2026-09-25T00:00:00Z");
    assert.equal(codex.multiple!.toFixed(1), "44.7");
    assert.equal(byKey.get("claude-1")!.feeSource, "receipt");
  });
});
