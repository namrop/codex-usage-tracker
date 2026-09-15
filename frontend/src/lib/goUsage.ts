import { humanizeToken, pctNumber } from "./weeklyUtilization";

// OpenCode Go usage explanation (keeper request 2026-09-14: "what consumes
// the shared allowance"). The backend (/api/opencode-go-usage, contract in
// docs/go-native-usage-explanation.md) projects this UTC week's canonical Go
// usage by model against the provider's native weekly percent. This module
// types that payload exactly and derives the small presentational rows the
// History card renders. Nothing here reads the DOM or fetches — it is
// Node-tested like lib/weeklyUtilization.ts.
//
// Two figures, never mixed: the NATIVE percent is provider-reported at
// whole-point resolution (an integer, floor-rounded, capped at 100 — so a
// value p below 100 stands for the band [p, p+1)); the MODELED points are a
// current-tariff estimate of weighted quota units over the 30-unit weekly
// pool. Model rows are never scaled to match the native total, and the
// estimate is never presented as money owed.

export type GoUsageMode = "quota" | "tokens";

export const GO_USAGE_MODES: readonly GoUsageMode[] = Object.freeze(["quota", "tokens"]);

export interface GoUsageWindow {
  started_at: string;
  ends_at: string;
  as_of: string;
}

export interface GoUsageAccount {
  provider: string;
  label: string;
  provider_account_index: number;
  provider_account_count: number;
}

export interface GoUsageNative {
  // Whole points (floor-rounded, capped at 100) or null when no fresh-or-
  // stale native reading exists this week. Never 0 for "missing".
  percent: number | null;
  status: string | null;
  observed_at: string | null;
  resets_at: string | null;
  fresh: boolean;
  age_seconds: number | null;
  capped: boolean | null;
  percent_resolution: number;
  source_surface: string;
  basis: string;
}

export interface GoUsageModel {
  model: string;
  event_count: number;
  correction_count: number;
  missing_token_event_count: number;
  input_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  output_tokens: number;
  total_tokens: number;
  // Diagnostic only: canonical output_tokens already include reasoning.
  reasoning_tokens: number;
  multiplier: string | null;
  estimated_list_cost_usd: string | null;
  estimated_quota_units: string | null;
  estimated_quota_pct: string | null;
  share_of_modeled_pct: string | null;
  pricing_status: string;
  basis: string;
}

export interface GoUsageTotals {
  event_count: number;
  priced_event_count: number;
  unpriced_event_count: number;
  model_count: number;
  priced_model_count: number;
  unpriced_model_count: number;
  input_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  output_tokens: number;
  total_tokens: number;
  reasoning_tokens: number;
  estimated_list_cost_usd: string;
  estimated_quota_units: string;
  estimated_quota_pct: string;
  weekly_limit_quota_units: string;
  basis: string;
}

export interface GoUsageReconciliation {
  status: string;
  native_pct: number | null;
  native_lower_pct: string | null;
  native_upper_pct: string | null;
  modeled_pct: string;
  modeled_quota_units: string;
  modeled_complete: boolean;
  latest_usage_at?: string | null;
  native_observed_at?: string | null;
  usage_ahead_of_native?: boolean | null;
  matches_within_rounding: boolean | null;
  band_gap_pct: string | null;
  native_basis: string;
  modeled_basis: string;
}

export interface GoUsageCoverage {
  latest_usage_at?: string | null;
  usage_rows_after_native?: number | null;
  source_usage_rows: number;
  usage_rows_in_window: number;
  duplicate_rows: number;
  correction_rows: number;
  missing_token_rows: number;
  unpriced_events: number;
  unpriced_models: string[];
  ignored_usage_rows: Record<string, number>;
  source_quota_rows: number;
  native_candidates: number;
  ignored_quota_rows: Record<string, number>;
}

export interface GoUsageTariffModel {
  input_usd_per_m: string;
  output_usd_per_m: string;
  cache_read_usd_per_m: string;
  cache_write_usd_per_m: string;
  multiplier: string;
}

export interface GoUsageTariff {
  as_of: string;
  source_url: string;
  basis: string;
  weekly_limit_quota_units: string;
  models: Record<string, GoUsageTariffModel>;
}

export interface GoUsagePayload {
  generated_at: string;
  window: GoUsageWindow;
  account: GoUsageAccount;
  native: GoUsageNative;
  models: GoUsageModel[];
  totals: GoUsageTotals;
  reconciliation: GoUsageReconciliation;
  coverage: GoUsageCoverage;
  tariff: GoUsageTariff;
  notes: string[];
}

export const GO_NATIVE_SOURCE_TEXT = "provider-reported · whole-point resolution";
export const GO_MODELED_BASIS_TEXT = "current-tariff estimate";
export const GO_NO_NATIVE_TEXT = "no native reading this week";

export function goUsageModeLabel(mode: GoUsageMode): string {
  return mode === "quota" ? "Quota points" : "Tokens";
}

function plural(count: number, singular: string, pluralNoun = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralNoun}`;
}

function count(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

// Modeled figures (Decimal strings) read at one decimal, like the other
// estimated surfaces; a missing figure is an em-dash, never a zero.
export function modeledPctText(value: unknown): string {
  const parsed = pctNumber(value);
  return parsed === null ? "—" : `${parsed.toFixed(1)}%`;
}

function pointsText(value: unknown): string {
  const parsed = pctNumber(value);
  return parsed === null ? "—" : parsed.toFixed(1);
}

function unitsText(value: unknown): string {
  const parsed = pctNumber(value);
  return parsed === null ? "—" : parsed.toFixed(2);
}

function ageText(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return "unknown age";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m old`;
  return `${Math.floor(seconds / 3600)}h old`;
}

function bandText(lower: unknown, upper: unknown): string {
  const low = pctNumber(lower);
  const high = pctNumber(upper);
  if (low === null) return GO_NO_NATIVE_TEXT;
  if (high === null) return "at least 100%";
  return `${low}–${high}%`;
}

export interface GoNativeHeadline {
  // "24%" — the integer the provider reports; "—" when missing.
  percentText: string;
  // "band 24–25%" | "at least 100%" | "no native reading this week"
  boundaryText: string;
  // "ok" | "rate limited" | "unknown"
  statusText: string;
  // "fresh" | "stale · 4h old" | "no native reading this week"
  freshnessText: string;
  sourceText: string;
  observedAt: string | null;
  resetsAt: string | null;
}

export function nativeHeadline(payload: GoUsagePayload): GoNativeHeadline {
  const native = payload.native;
  const percent = native.percent;
  if (percent === null || percent === undefined) {
    return {
      percentText: "—",
      boundaryText: GO_NO_NATIVE_TEXT,
      statusText: "unknown",
      freshnessText: GO_NO_NATIVE_TEXT,
      sourceText: GO_NATIVE_SOURCE_TEXT,
      observedAt: null,
      resetsAt: null,
    };
  }
  const capped = native.capped === true || percent >= 100;
  return {
    percentText: `${percent}%`,
    boundaryText: capped ? "at least 100%" : `band ${percent}–${percent + 1}%`,
    statusText: native.status ? humanizeToken(native.status) : "unknown",
    freshnessText: native.fresh ? "fresh" : `stale · ${ageText(native.age_seconds)}`,
    sourceText: GO_NATIVE_SOURCE_TEXT,
    observedAt: native.observed_at,
    resetsAt: native.resets_at,
  };
}

export interface GoTokenBucket {
  label: string;
  tokens: number;
}

export interface GoModelRow {
  model: string;
  label: string;
  priced: boolean;
  eventsText: string;
  multiplierText: string;
  totalTokens: number;
  buckets: GoTokenBucket[];
  reasoningTokens: number;
  listCostText: string;
  quotaUnitsText: string;
  quotaPctText: string;
  shareText: string;
  // Bar length: modeled points of the pool (quota mode) or share of this
  // week's tokens (token mode); null draws an empty bar.
  barPct: number | null;
  coverageText: string;
}

export function goModelRows(payload: GoUsagePayload, mode: GoUsageMode): GoModelRow[] {
  const models = Array.isArray(payload.models) ? payload.models : [];
  const totalTokens = count(payload.totals && payload.totals.total_tokens);
  return models.map((entry) => {
    const priced = entry.pricing_status === "priced";
    const events = count(entry.event_count);
    const missing = count(entry.missing_token_event_count);
    const corrections = count(entry.correction_count);
    const coverage: string[] = [plural(events, "event")];
    if (!priced) coverage.push("unpriced");
    if (missing) coverage.push(`${missing} missing tokens`);
    if (corrections) coverage.push(plural(corrections, "correction"));
    const tokens = count(entry.total_tokens);
    const multiplier = pctNumber(entry.multiplier);
    const listCost = pctNumber(entry.estimated_list_cost_usd);
    let barPct: number | null;
    if (mode === "tokens") {
      barPct = totalTokens > 0 ? (tokens / totalTokens) * 100 : null;
    } else {
      barPct = priced ? pctNumber(entry.estimated_quota_pct) : null;
    }
    return {
      model: entry.model,
      label: entry.model,
      priced,
      eventsText: plural(events, "event"),
      multiplierText: multiplier === null ? "unknown" : `${multiplier}×`,
      totalTokens: tokens,
      buckets: [
        { label: "input", tokens: count(entry.input_tokens) },
        { label: "cache read", tokens: count(entry.cache_read_tokens) },
        { label: "cache write", tokens: count(entry.cache_write_tokens) },
        { label: "output", tokens: count(entry.output_tokens) },
      ],
      reasoningTokens: count(entry.reasoning_tokens),
      listCostText: priced && listCost !== null ? `$${listCost.toFixed(2)}` : "unpriced",
      quotaUnitsText: priced ? unitsText(entry.estimated_quota_units) : "—",
      quotaPctText: priced ? modeledPctText(entry.estimated_quota_pct) : "—",
      shareText: priced ? modeledPctText(entry.share_of_modeled_pct) : "—",
      barPct,
      coverageText: coverage.join(" · "),
    };
  });
}

export function goModelKey(row: Pick<GoModelRow, "model">): string {
  return row.model;
}

// One sentence on how the modeled points relate to the native band. The
// native figure is whole points, so "within rounding" is the strongest
// claim below 100; at 100 the provider says "at least" and no remainder
// can be named.
export function reconciliationText(payload: GoUsagePayload): string {
  const rec = payload.reconciliation;
  const modeled = pointsText(rec.modeled_pct);
  const band = bandText(rec.native_lower_pct, rec.native_upper_pct);
  let text: string;
  switch (rec.status) {
    case "native_missing":
      text = `No native reading this week to compare; modeled ${modeled} points stand alone`;
      break;
    case "native_stale":
      text = `Native reading is stale; modeled ${modeled} points not compared`;
      break;
    case "native_capped":
      text = `Native reports at least 100%; modeled ${modeled} points — no exact remainder can be claimed`;
      break;
    case "usage_ahead_of_native":
      text = `Local activity is newer than the native reading; modeled ${modeled} points not compared`;
      break;
    case "modeled_incomplete":
      text = `Modeled coverage is incomplete; ${modeled} known points not compared with the full native allowance`;
      break;
    case "within_rounding":
      text = `Modeled ${modeled} points sit inside the native band ${band}: within rounding`;
      break;
    case "modeled_below":
    case "modeled_above": {
      const gap = pctNumber(rec.band_gap_pct);
      const distance = gap === null ? "—" : Math.abs(gap).toFixed(1);
      text = `Modeled ${modeled} points sit ${distance} ${rec.status === "modeled_below" ? "below" : "above"} the native band ${band}`;
      break;
    }
    default:
      text = `Reconciliation ${humanizeToken(rec.status)}; modeled ${modeled} points`;
  }
  if (!rec.modeled_complete && rec.status !== "modeled_incomplete") text += " · model incomplete (unpriced or missing tokens)";
  return text;
}

export function goTotalsLine(payload: GoUsagePayload): string {
  const totals = payload.totals;
  return [
    `modeled ${modeledPctText(totals.estimated_quota_pct)} of the ${totals.weekly_limit_quota_units}-unit weekly pool`,
    `${unitsText(totals.estimated_quota_units)} quota units`,
    plural(count(totals.model_count), "model"),
    plural(count(totals.event_count), "event"),
  ].join(" · ");
}

// Who counted and who was left out, in the contract's own counts.
export function goCoverageLine(payload: GoUsagePayload): string {
  const coverage = payload.coverage;
  const events = count(coverage.usage_rows_in_window);
  if (!events) return "no Go usage events this week";
  const parts: string[] = [`${plural(events, "event")} this week`];
  const unpriced = Array.isArray(coverage.unpriced_models) ? coverage.unpriced_models.length : 0;
  if (unpriced) parts.push(plural(unpriced, "unpriced model"));
  const missing = count(coverage.missing_token_rows);
  if (missing) parts.push(`${plural(missing, "event")} missing tokens`);
  const duplicates = count(coverage.duplicate_rows);
  if (duplicates) parts.push(`${plural(duplicates, "duplicate")} ignored`);
  const before = count(coverage.ignored_usage_rows && coverage.ignored_usage_rows.before_window);
  if (before) parts.push(`${plural(before, "row")} before the window`);
  return parts.join(" · ");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isNullableString(value: unknown): value is string | null {
  return value === null || isString(value);
}

function isNullableNumber(value: unknown): value is number | null {
  return value === null || isFiniteNumber(value);
}

function isNullableBoolean(value: unknown): value is boolean | null {
  return value === null || typeof value === "boolean";
}

function isNumberRecord(value: unknown): value is Record<string, number> {
  return isRecord(value) && Object.values(value).every(isFiniteNumber);
}

function isGoUsageModel(value: unknown): value is GoUsageModel {
  if (!isRecord(value)) return false;
  return [value.model, value.pricing_status, value.basis].every(isString)
    && [
      value.event_count,
      value.correction_count,
      value.missing_token_event_count,
      value.input_tokens,
      value.cache_read_tokens,
      value.cache_write_tokens,
      value.output_tokens,
      value.total_tokens,
      value.reasoning_tokens,
    ].every(isFiniteNumber)
    && [
      value.multiplier,
      value.estimated_list_cost_usd,
      value.estimated_quota_units,
      value.estimated_quota_pct,
      value.share_of_modeled_pct,
    ].every(isNullableString);
}

function isGoUsageTariffModel(value: unknown): value is GoUsageTariffModel {
  if (!isRecord(value)) return false;
  return [
    value.input_usd_per_m,
    value.output_usd_per_m,
    value.cache_read_usd_per_m,
    value.cache_write_usd_per_m,
    value.multiplier,
  ].every(isString);
}

// The loader refuses a payload the card could not render honestly — in
// particular an incomplete nested shape or a native percent outside the
// provider's whole-point 0..100 contract.
export function isGoUsagePayload(value: unknown): value is GoUsagePayload {
  if (!isRecord(value)) return false;
  const candidate = value;
  if (!isString(candidate.generated_at)) return false;

  const usageWindow = candidate.window;
  if (!isRecord(usageWindow) || ![usageWindow.started_at, usageWindow.ends_at, usageWindow.as_of].every(isString)) return false;

  const account = candidate.account;
  if (!isRecord(account)
    || ![account.provider, account.label].every(isString)
    || ![account.provider_account_index, account.provider_account_count].every(isFiniteNumber)) return false;

  const native = candidate.native;
  if (!isRecord(native)) return false;
  const percent = native.percent;
  if (percent !== null
    && !(isFiniteNumber(percent) && Number.isInteger(percent) && percent >= 0 && percent <= 100)) return false;
  if (![native.status, native.observed_at, native.resets_at].every(isNullableString)
    || typeof native.fresh !== "boolean"
    || !isNullableNumber(native.age_seconds)
    || !isNullableBoolean(native.capped)
    || !isFiniteNumber(native.percent_resolution)
    || ![native.source_surface, native.basis].every(isString)) return false;

  if (!Array.isArray(candidate.models) || !candidate.models.every(isGoUsageModel)) return false;

  const totals = candidate.totals;
  if (!isRecord(totals)
    || ![
      totals.event_count,
      totals.priced_event_count,
      totals.unpriced_event_count,
      totals.model_count,
      totals.priced_model_count,
      totals.unpriced_model_count,
      totals.input_tokens,
      totals.cache_read_tokens,
      totals.cache_write_tokens,
      totals.output_tokens,
      totals.total_tokens,
      totals.reasoning_tokens,
    ].every(isFiniteNumber)
    || ![
      totals.estimated_list_cost_usd,
      totals.estimated_quota_units,
      totals.estimated_quota_pct,
      totals.weekly_limit_quota_units,
      totals.basis,
    ].every(isString)) return false;

  const reconciliation = candidate.reconciliation;
  if (!isRecord(reconciliation)
    || ![reconciliation.status, reconciliation.modeled_pct, reconciliation.modeled_quota_units,
      reconciliation.native_basis, reconciliation.modeled_basis].every(isString)
    || !isNullableNumber(reconciliation.native_pct)
    || ![reconciliation.native_lower_pct, reconciliation.native_upper_pct,
      reconciliation.band_gap_pct].every(isNullableString)
    || typeof reconciliation.modeled_complete !== "boolean"
    || !isNullableBoolean(reconciliation.matches_within_rounding)
    || (reconciliation.latest_usage_at !== undefined && !isNullableString(reconciliation.latest_usage_at))
    || (reconciliation.native_observed_at !== undefined && !isNullableString(reconciliation.native_observed_at))
    || (reconciliation.usage_ahead_of_native !== undefined
      && !isNullableBoolean(reconciliation.usage_ahead_of_native))) return false;

  const coverage = candidate.coverage;
  if (!isRecord(coverage)
    || ![
      coverage.source_usage_rows,
      coverage.usage_rows_in_window,
      coverage.duplicate_rows,
      coverage.correction_rows,
      coverage.missing_token_rows,
      coverage.unpriced_events,
      coverage.source_quota_rows,
      coverage.native_candidates,
    ].every(isFiniteNumber)
    || !Array.isArray(coverage.unpriced_models)
    || !coverage.unpriced_models.every(isString)
    || !isNumberRecord(coverage.ignored_usage_rows)
    || !isNumberRecord(coverage.ignored_quota_rows)
    || (coverage.latest_usage_at !== undefined && !isNullableString(coverage.latest_usage_at))
    || (coverage.usage_rows_after_native !== undefined
      && !isNullableNumber(coverage.usage_rows_after_native))) return false;

  const tariff = candidate.tariff;
  if (!isRecord(tariff)
    || ![tariff.as_of, tariff.source_url, tariff.basis, tariff.weekly_limit_quota_units].every(isString)
    || !isRecord(tariff.models)
    || !Object.values(tariff.models).every(isGoUsageTariffModel)) return false;

  if (!Array.isArray(candidate.notes) || !candidate.notes.every(isString)) return false;
  return true;
}
