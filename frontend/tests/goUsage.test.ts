import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  GO_USAGE_MODES,
  goCoverageLine,
  goModelKey,
  goModelRows,
  goTotalsLine,
  goUsageModeLabel,
  isGoUsagePayload,
  modeledPctText,
  nativeHeadline,
  reconciliationText,
  type GoUsagePayload,
} from "../src/lib/goUsage";

// Fixture mirrors /api/opencode-go-usage (contract docs/go-native-usage-
// explanation.md): the native percent is an INTEGER (floor-rounded, capped
// at 100), modeled figures are Decimal strings, and one unpriced model keeps
// its tokens with null estimates.

const PAYLOAD: GoUsagePayload = {
  generated_at: "2026-09-15T03:39:00Z",
  window: { started_at: "2026-09-14T00:00:00Z", ends_at: "2026-09-21T00:00:00Z", as_of: "2026-09-15T03:39:00Z" },
  account: { provider: "opencode-go", label: "OpenCode Go", provider_account_index: 1, provider_account_count: 1 },
  native: {
    percent: 24,
    status: "ok",
    observed_at: "2026-09-15T02:50:00Z",
    resets_at: "2026-09-21T00:00:00Z",
    fresh: true,
    age_seconds: 2940,
    capped: false,
    percent_resolution: 1,
    source_surface: "opencode_go_usage_api",
    basis: "provider_reported",
  },
  models: [
    {
      model: "glm-5.3",
      event_count: 3,
      correction_count: 0,
      missing_token_event_count: 0,
      input_tokens: 1000000,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      output_tokens: 100000,
      total_tokens: 1100000,
      reasoning_tokens: 20000,
      multiplier: "4",
      estimated_list_cost_usd: "1.84000000",
      estimated_quota_units: "7.36000000",
      estimated_quota_pct: "24.533",
      share_of_modeled_pct: "100.000",
      pricing_status: "priced",
      basis: "current_tariff_estimate",
    },
    {
      model: "mystery-model",
      event_count: 1,
      correction_count: 0,
      missing_token_event_count: 1,
      input_tokens: 10,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      output_tokens: 5,
      total_tokens: 15,
      reasoning_tokens: 0,
      multiplier: null,
      estimated_list_cost_usd: null,
      estimated_quota_units: null,
      estimated_quota_pct: null,
      share_of_modeled_pct: null,
      pricing_status: "unpriced",
      basis: "current_tariff_estimate",
    },
  ],
  totals: {
    event_count: 4,
    priced_event_count: 3,
    unpriced_event_count: 1,
    model_count: 2,
    priced_model_count: 1,
    unpriced_model_count: 1,
    input_tokens: 1000010,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    output_tokens: 100005,
    total_tokens: 1100015,
    reasoning_tokens: 20000,
    estimated_list_cost_usd: "1.84000000",
    estimated_quota_units: "7.36000000",
    estimated_quota_pct: "24.533",
    weekly_limit_quota_units: "30",
    basis: "current_tariff_estimate",
  },
  reconciliation: {
    status: "modeled_incomplete",
    native_pct: 24,
    native_lower_pct: "24",
    native_upper_pct: "25",
    modeled_pct: "24.533",
    modeled_quota_units: "7.36000000",
    modeled_complete: false,
    matches_within_rounding: null,
    band_gap_pct: null,
    native_basis: "provider_reported",
    modeled_basis: "current_tariff_estimate",
  },
  coverage: {
    source_usage_rows: 7,
    usage_rows_in_window: 4,
    duplicate_rows: 1,
    correction_rows: 0,
    missing_token_rows: 1,
    unpriced_events: 1,
    unpriced_models: ["mystery-model"],
    ignored_usage_rows: { malformed: 0, other_fact_type: 0, other_provider: 0, invalid_timestamp: 0, before_window: 2, future: 0 },
    source_quota_rows: 5,
    native_candidates: 3,
    ignored_quota_rows: {},
  },
  tariff: {
    as_of: "2026-09-15T03:31:00Z",
    source_url: "https://opencode.ai/docs/go/#usage-limits",
    basis: "current_tariff_estimate",
    weekly_limit_quota_units: "30",
    models: { "glm-5.3": { input_usd_per_m: "1.40", output_usd_per_m: "4.40", cache_read_usd_per_m: "0.26", cache_write_usd_per_m: "0", multiplier: "4" } },
  },
  notes: ["Native weekly percent is provider-reported at whole-point resolution."],
};

function withNative(native: Partial<GoUsagePayload["native"]>, reconciliation: Partial<GoUsagePayload["reconciliation"]> = {}): GoUsagePayload {
  return { ...PAYLOAD, native: { ...PAYLOAD.native, ...native }, reconciliation: { ...PAYLOAD.reconciliation, ...reconciliation } };
}

describe("modes", () => {
  it("defaults to quota points and offers a token view", () => {
    assert.deepEqual([...GO_USAGE_MODES], ["quota", "tokens"]);
    assert.equal(goUsageModeLabel("quota"), "Quota points");
    assert.equal(goUsageModeLabel("tokens"), "Tokens");
  });
});

describe("nativeHeadline", () => {
  it("renders the native percent as a whole integer with its band and provenance", () => {
    const headline = nativeHeadline(PAYLOAD);
    assert.equal(headline.percentText, "24%");
    assert.equal(headline.boundaryText, "band 24–25%");
    assert.equal(headline.statusText, "ok");
    assert.equal(headline.freshnessText, "fresh");
    assert.equal(headline.sourceText, "provider-reported · whole-point resolution");
    assert.ok(!headline.percentText.includes("."));
  });

  it("says at least 100% when capped and reads rate limited plainly", () => {
    const headline = nativeHeadline(withNative({ percent: 100, capped: true, status: "rate_limited" }));
    assert.equal(headline.percentText, "100%");
    assert.equal(headline.boundaryText, "at least 100%");
    assert.equal(headline.statusText, "rate limited");
  });

  it("shows an em-dash and says so when the native reading is missing or stale — never 0%", () => {
    const missing = nativeHeadline(withNative({ percent: null, status: null, observed_at: null, fresh: false, age_seconds: null, capped: null }));
    assert.equal(missing.percentText, "—");
    assert.equal(missing.boundaryText, "no native reading this week");
    assert.equal(missing.statusText, "unknown");
    assert.equal(missing.freshnessText, "no native reading this week");
    const stale = nativeHeadline(withNative({ fresh: false, age_seconds: 4 * 3600 }));
    assert.equal(stale.percentText, "24%");
    assert.equal(stale.freshnessText, "stale · 4h old");
    assert.ok(!JSON.stringify(missing).includes("0%"));
  });
});

describe("modeledPctText", () => {
  it("shows modeled figures at one decimal and refuses to invent a value", () => {
    assert.equal(modeledPctText("24.533"), "24.5%");
    assert.equal(modeledPctText("245.333"), "245.3%");
    assert.equal(modeledPctText(null), "—");
    assert.equal(modeledPctText("n/a"), "—");
  });
});

describe("goModelRows", () => {
  it("keeps every model in payload order with multiplier, tokens and estimated contribution", () => {
    const rows = goModelRows(PAYLOAD, "quota");
    assert.deepEqual(rows.map((row) => row.model), ["glm-5.3", "mystery-model"]);
    const glm = rows[0];
    assert.equal(glm.priced, true);
    assert.equal(glm.multiplierText, "4×");
    assert.equal(glm.eventsText, "3 events");
    assert.equal(glm.totalTokens, 1100000);
    assert.equal(glm.quotaUnitsText, "7.36");
    assert.equal(glm.quotaPctText, "24.5%");
    assert.equal(glm.listCostText, "$1.84");
    assert.equal(glm.shareText, "100.0%");
    assert.equal(glm.barPct, 24.533);
    assert.equal(glm.coverageText, "3 events");
    assert.deepEqual(glm.buckets.map((bucket) => bucket.label), ["input", "cache read", "cache write", "output"]);
    assert.deepEqual(glm.buckets.map((bucket) => bucket.tokens), [1000000, 0, 0, 100000]);
    assert.equal(glm.reasoningTokens, 20000);
    const mystery = rows[1];
    assert.equal(mystery.priced, false);
    assert.equal(mystery.multiplierText, "unknown");
    assert.equal(mystery.quotaUnitsText, "—");
    assert.equal(mystery.quotaPctText, "—");
    assert.equal(mystery.listCostText, "unpriced");
    assert.equal(mystery.shareText, "—");
    assert.equal(mystery.barPct, null);
    assert.equal(mystery.coverageText, "1 event · unpriced · 1 missing tokens");
  });

  it("draws token share in token mode without touching the estimates", () => {
    const rows = goModelRows(PAYLOAD, "tokens");
    assert.ok(Math.abs((rows[0].barPct ?? 0) - (1100000 / 1100015) * 100) < 0.001);
    assert.ok(Math.abs((rows[1].barPct ?? 0) - (15 / 1100015) * 100) < 0.001);
    assert.equal(rows[0].quotaPctText, "24.5%");
    assert.equal(goModelKey(rows[0]), "glm-5.3");
  });

  it("never mutates or reorders the payload the hook holds", () => {
    goModelRows(PAYLOAD, "quota");
    assert.equal(PAYLOAD.models[0].model, "glm-5.3");
    assert.equal(PAYLOAD.models[0].estimated_quota_pct, "24.533");
  });
});

describe("reconciliationText", () => {
  it("does not compare incomplete modeled coverage with the native band", () => {
    assert.equal(
      reconciliationText(PAYLOAD),
      "Modeled coverage is incomplete; 24.5 known points not compared with the full native allowance",
    );
    const complete = {
      ...PAYLOAD,
      reconciliation: {
        ...PAYLOAD.reconciliation,
        status: "within_rounding",
        modeled_complete: true,
        matches_within_rounding: true,
      },
    };
    assert.equal(reconciliationText(complete), "Modeled 24.5 points sit inside the native band 24–25%: within rounding");
  });

  it("carries the signed gap outside the band", () => {
    const below = withNative({ percent: 30 }, { status: "modeled_below", native_lower_pct: "30", native_upper_pct: "31", matches_within_rounding: false, band_gap_pct: "-5.467", modeled_complete: true });
    assert.equal(reconciliationText(below), "Modeled 24.5 points sit 5.5 below the native band 30–31%");
    const above = withNative({ percent: 20 }, { status: "modeled_above", native_lower_pct: "20", native_upper_pct: "21", matches_within_rounding: false, band_gap_pct: "3.533", modeled_complete: true });
    assert.equal(reconciliationText(above), "Modeled 24.5 points sit 3.5 above the native band 20–21%");
  });

  it("states the capped boundary, the stale case and the missing case without a delta", () => {
    const capped = withNative({ percent: 100, capped: true, status: "rate_limited" }, { status: "native_capped", native_lower_pct: "100", native_upper_pct: null, matches_within_rounding: null, band_gap_pct: null, modeled_pct: "245.333", modeled_complete: true });
    assert.equal(reconciliationText(capped), "Native reports at least 100%; modeled 245.3 points — no exact remainder can be claimed");
    const stale = withNative({ fresh: false }, { status: "native_stale", matches_within_rounding: null, modeled_complete: true });
    assert.equal(reconciliationText(stale), "Native reading is stale; modeled 24.5 points not compared");
    const missing = withNative({ percent: null }, { status: "native_missing", native_lower_pct: null, native_upper_pct: null, matches_within_rounding: null, modeled_complete: true });
    assert.equal(reconciliationText(missing), "No native reading this week to compare; modeled 24.5 points stand alone");
  });
});

describe("goTotalsLine / goCoverageLine", () => {
  it("summarizes the modeled total against the pool and the coverage counts", () => {
    assert.equal(goTotalsLine(PAYLOAD), "modeled 24.5% of the 30-unit weekly pool · 7.36 quota units · 2 models · 4 events");
    assert.equal(
      goCoverageLine(PAYLOAD),
      "4 events this week · 1 unpriced model · 1 event missing tokens · 1 duplicate ignored · 2 rows before the window",
    );
  });

  it("reads plainly when nothing was observed", () => {
    const empty: GoUsagePayload = {
      ...PAYLOAD,
      models: [],
      totals: { ...PAYLOAD.totals, event_count: 0, model_count: 0, unpriced_model_count: 0, unpriced_event_count: 0, estimated_quota_units: "0.00000000", estimated_quota_pct: "0.000" },
      coverage: { ...PAYLOAD.coverage, usage_rows_in_window: 0, duplicate_rows: 0, missing_token_rows: 0, unpriced_models: [], ignored_usage_rows: { ...PAYLOAD.coverage.ignored_usage_rows, before_window: 0 } },
    };
    assert.equal(goCoverageLine(empty), "no Go usage events this week");
  });
});

describe("isGoUsagePayload", () => {
  it("accepts the contract shape and rejects what the panel could not render honestly", () => {
    assert.equal(isGoUsagePayload(PAYLOAD), true);
    assert.equal(isGoUsagePayload(null), false);
    assert.equal(isGoUsagePayload({}), false);
    assert.equal(isGoUsagePayload({ ...PAYLOAD, models: "nope" }), false);
    assert.equal(isGoUsagePayload({ ...PAYLOAD, native: null }), false);
    assert.equal(isGoUsagePayload({ ...PAYLOAD, native: { ...PAYLOAD.native, percent: 24.5 } }), false);
    assert.equal(isGoUsagePayload({ ...PAYLOAD, native: { ...PAYLOAD.native, percent: "24" } }), false);
    assert.equal(isGoUsagePayload({ ...PAYLOAD, native: { ...PAYLOAD.native, percent: null } }), true);
    assert.equal(isGoUsagePayload({ ...PAYLOAD, reconciliation: undefined }), false);
    assert.equal(isGoUsagePayload({ ...PAYLOAD, totals: [] }), false);
    assert.equal(isGoUsagePayload({ ...PAYLOAD, notes: "x" }), false);
  });

  it("rejects the reproduced structurally incomplete response", () => {
    assert.equal(isGoUsagePayload({
      native: { percent: null },
      models: [],
      totals: {},
      reconciliation: {},
      coverage: {},
    }), false);
  });

  it("requires the nested objects and arrays dereferenced while rendering", () => {
    const malformed: Array<[string, unknown]> = [
      ["window", { ...PAYLOAD, window: undefined }],
      ["account", { ...PAYLOAD, account: null }],
      ["tariff", { ...PAYLOAD, tariff: undefined }],
      ["tariff.models", { ...PAYLOAD, tariff: { ...PAYLOAD.tariff, models: null } }],
      ["coverage.unpriced_models", { ...PAYLOAD, coverage: { ...PAYLOAD.coverage, unpriced_models: null } }],
      ["coverage.ignored_usage_rows", { ...PAYLOAD, coverage: { ...PAYLOAD.coverage, ignored_usage_rows: null } }],
      ["notes", { ...PAYLOAD, notes: undefined }],
    ];
    for (const [label, value] of malformed) {
      assert.equal(isGoUsagePayload(value), false, label);
    }
  });

  it("rejects model, tariff, and note entries that are not render-safe", () => {
    const malformed: Array<[string, unknown]> = [
      ["model entry", { ...PAYLOAD, models: [null] }],
      ["model label", { ...PAYLOAD, models: [{ ...PAYLOAD.models[0], model: {} }] }],
      ["model count", { ...PAYLOAD, models: [{ ...PAYLOAD.models[0], event_count: "3" }] }],
      ["tariff model entry", { ...PAYLOAD, tariff: { ...PAYLOAD.tariff, models: { "glm-5.3": null } } }],
      ["note entry", { ...PAYLOAD, notes: [{}] }],
    ];
    for (const [label, value] of malformed) {
      assert.equal(isGoUsagePayload(value), false, label);
    }
  });

  it("rejects non-scalar field values that would reach formatting or JSX", () => {
    const malformed: Array<[string, unknown]> = [
      ["generated_at", { ...PAYLOAD, generated_at: {} }],
      ["window.started_at", { ...PAYLOAD, window: { ...PAYLOAD.window, started_at: {} } }],
      ["account.label", { ...PAYLOAD, account: { ...PAYLOAD.account, label: {} } }],
      ["native.status", { ...PAYLOAD, native: { ...PAYLOAD.native, status: {} } }],
      ["native.fresh", { ...PAYLOAD, native: { ...PAYLOAD.native, fresh: "yes" } }],
      ["totals.total_tokens", { ...PAYLOAD, totals: { ...PAYLOAD.totals, total_tokens: "1100015" } }],
      ["totals.weekly_limit_quota_units", { ...PAYLOAD, totals: { ...PAYLOAD.totals, weekly_limit_quota_units: {} } }],
      ["reconciliation.status", { ...PAYLOAD, reconciliation: { ...PAYLOAD.reconciliation, status: {} } }],
      ["coverage.native_candidates", { ...PAYLOAD, coverage: { ...PAYLOAD.coverage, native_candidates: {} } }],
      ["tariff.as_of", { ...PAYLOAD, tariff: { ...PAYLOAD.tariff, as_of: {} } }],
      ["tariff rate", { ...PAYLOAD, tariff: { ...PAYLOAD.tariff, models: { "glm-5.3": { ...PAYLOAD.tariff.models["glm-5.3"], multiplier: 4 } } } }],
    ];
    for (const [label, value] of malformed) {
      assert.equal(isGoUsagePayload(value), false, label);
    }
  });

  it("preserves valid optional reconciliation and coverage metadata", () => {
    assert.equal(isGoUsagePayload({
      ...PAYLOAD,
      reconciliation: {
        ...PAYLOAD.reconciliation,
        latest_usage_at: "2026-09-15T02:45:00Z",
        native_observed_at: "2026-09-15T02:50:00Z",
        usage_ahead_of_native: false,
      },
      coverage: {
        ...PAYLOAD.coverage,
        latest_usage_at: "2026-09-15T02:45:00Z",
        usage_rows_after_native: 0,
      },
    }), true);
  });

  it("accepts only null or an integer native percent from 0 through 100", () => {
    assert.equal(isGoUsagePayload({ ...PAYLOAD, native: { ...PAYLOAD.native, percent: null } }), true);
    assert.equal(isGoUsagePayload({ ...PAYLOAD, native: { ...PAYLOAD.native, percent: 0 } }), true);
    assert.equal(isGoUsagePayload({ ...PAYLOAD, native: { ...PAYLOAD.native, percent: 100 } }), true);
    assert.equal(isGoUsagePayload({ ...PAYLOAD, native: { ...PAYLOAD.native, percent: -1 } }), false);
    assert.equal(isGoUsagePayload({ ...PAYLOAD, native: { ...PAYLOAD.native, percent: 101 } }), false);
  });
});
