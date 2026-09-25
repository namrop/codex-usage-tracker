import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { BURN_PROJECTION_STATUSES, burnRowClass, deriveBurnRows, projectionStatusOf, projectionText } from "../src/lib/burn";
import { accountDisplayLabel } from "../src/lib/providers";

function series(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    provider: "anthropic",
    label: "Anthropic / Claude",
    quota_name: "five_hour",
    harness: "claude_code",
    quota_scope: "account",
    window_kind: "rolling",
    unit: "percent",
    provider_account_index: 1,
    provider_account_count: 1,
    measurement_confidence: "exact",
    confidence_status: "exact",
    resets_at: "2026-09-14T23:00:00Z",
    seconds_to_reset: 3600,
    latest_observed_at: "2026-09-14T20:00:00Z",
    latest_used_pct: 40,
    observation_count: 3,
    burn_rate_pct_per_hour: 12.5,
    rate_window_deltas: 2,
    exhausted: false,
    projection_status: "resets_first",
    projection_reason: "reset_before_empty_at_observed_rate",
    projected_exhaustion_at: null,
    deltas: [],
    observations: [
      { observed_at: "2026-09-14T19:00:00Z", used_pct: 20 },
      { observed_at: "2026-09-14T19:30:00Z", used_pct: 30 },
      { observed_at: "2026-09-14T20:00:00Z", used_pct: 40 },
    ],
    ...overrides,
  };
}

describe("accountDisplayLabel", () => {
  it("numbers accounts only when the provider has more than one, from the API ordinal", () => {
    assert.equal(accountDisplayLabel({ provider: "anthropic", provider_account_index: 1, provider_account_count: 2 }), "Claude 1");
    assert.equal(accountDisplayLabel({ provider: "anthropic", provider_account_index: 2, provider_account_count: 2 }), "Claude 2");
    assert.equal(accountDisplayLabel({ provider: "anthropic", provider_account_index: 1, provider_account_count: 1 }), "Claude");
    assert.equal(accountDisplayLabel({ provider: "claude", provider_account_index: 2, provider_account_count: 2 }), "Claude 2", "raw aliases resolve");
    assert.equal(accountDisplayLabel({ provider: "z-ai", provider_canonical: "z-ai-glm" }), "Z.AI");
    // OpenRouter has had a display name since the 2026-09-25 UI review (item 5).
    assert.equal(accountDisplayLabel({ provider: "openrouter" }), "OpenRouter");
    assert.equal(accountDisplayLabel({ provider: "acme-ai" }), "acme-ai", "an unmapped provider keeps its id");
    assert.equal(accountDisplayLabel(null), "unknown");
  });
});

describe("deriveBurnRows", () => {
  it("labels two Claude accounts as Claude 1 and Claude 2 by ordinal, not by array order", () => {
    const rows = deriveBurnRows([
      series({ provider_account_index: 2, provider_account_count: 2, latest_used_pct: 6 }),
      series({ provider_account_index: 1, provider_account_count: 2, latest_used_pct: 100, projection_status: "exhausted", projection_reason: "used_at_cap", exhausted: true }),
    ]);
    assert.deepEqual(rows.map((row) => row.label), ["Claude 2", "Claude 1"]);
    assert.deepEqual(rows.map((row) => row.status), ["resets_first", "exhausted"]);
    assert.equal(rows[1].exhausted, true);
    assert.equal(rows[0].spark.length, 3);
  });

  it("carries the provider's confidence and never upgrades an estimate to exact", () => {
    const [row] = deriveBurnRows([
      series({ provider: "opencode-go", harness: "opencode", unit: "usd", measurement_confidence: "estimated", confidence_status: "partial" }),
    ]);
    assert.equal(row.label, "OpenCode Go");
    assert.equal(row.confidenceStatus, "partial");
    assert.equal(row.confidenceLabel, "Partial");
    assert.notEqual(row.confidenceLabel, "Exact");
  });

  it("reconstructs only the two states an older server could assert", () => {
    assert.deepEqual(projectionStatusOf({ exhausted: true }), { status: "exhausted", reason: "used_at_cap" });
    assert.deepEqual(projectionStatusOf({ projected_exhaustion_at: "2026-09-14T22:00:00Z" }), {
      status: "projected_before_reset",
      reason: "empty_before_reset_at_observed_rate",
    });
    assert.deepEqual(projectionStatusOf({ exhausted: false }), { status: "insufficient_observations", reason: null });
    assert.deepEqual(projectionStatusOf({ projection_status: "bogus" }), { status: "insufficient_observations", reason: null });
  });
});

describe("projectionText", () => {
  it("has words for every status and never says unknown", () => {
    for (const status of BURN_PROJECTION_STATUSES) {
      const text = projectionText(status, null, "Sep 14, 22:00");
      assert.ok(text.length > 0, status);
      assert.doesNotMatch(text, /unknown/i, status);
    }
    assert.equal(projectionText("exhausted", "used_at_cap", null), "exhausted");
    assert.equal(projectionText("projected_before_reset", null, "Sep 14, 22:00"), "empty Sep 14, 22:00");
    assert.equal(projectionText("resets_first", null, null), "resets first");
    assert.equal(projectionText("insufficient_observations", "single_observation", null), "one sample only");
    assert.equal(projectionText("insufficient_observations", "no_positive_rate", null), "no rise measured");
    assert.equal(projectionText("not_applicable", "balance_window", null), "n/a · balance");
    assert.equal(projectionText("not_applicable", "no_reset_reported", null), "n/a · no reset reported");
    assert.equal(projectionText("stale", "observation_stale", null), "stale sample");
    assert.equal(projectionText("stale", "reset_passed_since_observation", null), "stale · reset passed since sample");
  });

  it("gives row classes only to the claims that need the eye", () => {
    assert.equal(burnRowClass("exhausted"), "burn-exhausted");
    assert.equal(burnRowClass("projected_before_reset"), "burn-projected");
    assert.equal(burnRowClass("stale"), "burn-stale");
    assert.equal(burnRowClass("not_applicable"), "burn-not-applicable");
    assert.equal(burnRowClass("resets_first"), undefined);
    assert.equal(burnRowClass("insufficient_observations"), undefined);
  });
});
