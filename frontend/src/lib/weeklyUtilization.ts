import { PROVIDER_DISPLAY_NAMES, PROVIDER_ORDER } from "./providers";

// Weekly allowance utilization (keeper request 2026-09-14: "what percentage
// of my weekly windows I am using ... aggregate, and per provider"). The
// backend (/api/weekly-utilization) does every mean in Decimal and serializes
// percentages as numeric strings; this module types that payload exactly,
// parses the strings, and derives the small presentational rows the Now
// strip and the History panel render. Nothing here reads the DOM or fetches
// — it is Node-tested like lib/capacity.ts.
//
// Two vocabularies, never mixed: a CURRENT window has used and REMAINING
// percent; a COMPLETED window has used and UNUSED AT RESET percent (the last
// sample before the observed reset — a polling estimate, not a bill). No
// blaming word appears anywhere: an unused allowance is a fact, not a fault.

export type WeeklyMode = "current" | "completed";

export const WEEKLY_MODES: readonly WeeklyMode[] = Object.freeze(["current", "completed"]);

export interface WeeklySummaryProvider {
  provider: string;
  label: string;
  used_pct: string | null;
  remaining_pct: string | null;
  account_count: number;
  excluded_account_count: number;
  estimated_account_count: number;
  cycle_count: number;
}

export interface WeeklySummary {
  used_pct: string | null;
  // remaining (current) or unused at reset (completed).
  remaining_pct: string | null;
  account_count: number;
  excluded_account_count: number;
  // Includes partial / mixed / unknown-confidence contributors.
  estimated_account_count: number;
  // Completed only; 0 for current.
  cycle_count: number;
  providers: WeeklySummaryProvider[];
}

export interface WeeklyWindowCurrent {
  used_pct: string | null;
  remaining_pct: string | null;
  observed_at: string | null;
  resets_at: string | null;
  confidence: string;
  status: string;
  eligible: boolean;
}

export interface WeeklyWindowCompleted {
  used_pct: string | null;
  remaining_pct: string | null;
  cycle_count: number;
  excluded_cycle_count: number;
}

export interface WeeklyCycle {
  resets_at: string;
  last_observed_at: string | null;
  used_pct: string | null;
  remaining_pct: string | null;
  confidence: string;
  eligible: boolean;
  reasons: string[];
  last_sample_seconds_before_reset: number | null;
}

export interface WeeklyWindow {
  provider: string;
  // Public account label ("Claude 1"); never an account ref.
  label: string;
  provider_account_index: number;
  provider_account_count: number;
  quota_name: string;
  role: "primary" | "model_cap";
  current: WeeklyWindowCurrent;
  completed: WeeklyWindowCompleted;
  cycles: WeeklyCycle[];
}

export interface WeeklyCoverage {
  source_rows: number;
  invalid_rows: number;
  excluded_cycles: number;
}

export interface WeeklyUtilizationPayload {
  generated_at: string;
  weeks: number;
  lookback_days: number;
  weighting: string;
  current: WeeklySummary;
  completed: WeeklySummary;
  windows: WeeklyWindow[];
  coverage: WeeklyCoverage;
  notes: string[];
}

export const WEEKLY_USED_LABEL = "Average weekly allowance used";
export const WEEKLY_WEIGHTING_TEXT = "equal weight per account";

export function pctNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

export function pctText(value: unknown, digits = 1): string {
  const parsed = pctNumber(value);
  return parsed === null ? "—" : `${parsed.toFixed(digits)}%`;
}

export function complementLabel(mode: WeeklyMode): string {
  return mode === "current" ? "remaining" : "unused at reset";
}

export function weeklyModeLabel(mode: WeeklyMode): string {
  return mode === "current" ? "Current windows" : "Completed windows";
}

function plural(count: number, singular: string, pluralNoun = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralNoun}`;
}

export interface WeeklyHeadline {
  usedLabel: string;
  usedText: string;
  complementLabel: string;
  complementText: string;
}

export function summaryHeadline(summary: WeeklySummary, mode: WeeklyMode): WeeklyHeadline {
  return {
    usedLabel: WEEKLY_USED_LABEL,
    usedText: pctText(summary.used_pct),
    complementLabel: complementLabel(mode),
    complementText: pctText(summary.remaining_pct),
  };
}

// One line under the headline: who counted, who was estimated, who was
// left out and why, and the weighting. The method note carries the rest.
export function weeklyCoverageLine(summary: WeeklySummary, mode: WeeklyMode): string {
  const parts: string[] = [];
  const accounts = Number(summary.account_count) || 0;
  const estimated = Number(summary.estimated_account_count) || 0;
  const excluded = Number(summary.excluded_account_count) || 0;
  const cycles = Number(summary.cycle_count) || 0;
  parts.push(accounts ? plural(accounts, "account") : "no contributing accounts");
  if (mode === "completed" && accounts) parts.push(plural(cycles, "completed cycle"));
  if (estimated) parts.push(`${estimated} estimated`);
  if (excluded) {
    parts.push(mode === "current" ? `${excluded} excluded (stale or missing)` : `${excluded} excluded (no eligible cycle)`);
  }
  if (mode === "completed") parts.push("last sample before each reset");
  parts.push(WEEKLY_WEIGHTING_TEXT);
  return parts.join(" · ");
}

export interface WeeklyProviderRow {
  provider: string;
  label: string;
  usedPct: number | null;
  complementPct: number | null;
  usedText: string;
  complementLabel: string;
  complementText: string;
  coverageText: string;
  accountCount: number;
  cycleCount: number;
  estimated: boolean;
  // No account contributed: the bar draws empty, the text says why.
  empty: boolean;
}

function providerRank(provider: string): number {
  const index = PROVIDER_ORDER.indexOf(provider);
  return index === -1 ? PROVIDER_ORDER.length : index;
}

export function weeklyProviderRows(summary: WeeklySummary, mode: WeeklyMode): WeeklyProviderRow[] {
  const providers = Array.isArray(summary.providers) ? summary.providers : [];
  return providers
    .map((entry) => {
      const accounts = Number(entry.account_count) || 0;
      const estimated = Number(entry.estimated_account_count) || 0;
      const excluded = Number(entry.excluded_account_count) || 0;
      const cycles = Number(entry.cycle_count) || 0;
      const coverage: string[] = [];
      coverage.push(accounts ? plural(accounts, "account") : "no contributing account");
      if (mode === "completed" && accounts) coverage.push(plural(cycles, "cycle"));
      if (estimated) coverage.push(`${estimated} estimated`);
      if (excluded) coverage.push(`${excluded} excluded`);
      return {
        provider: entry.provider,
        label: entry.label || PROVIDER_DISPLAY_NAMES[entry.provider] || entry.provider,
        usedPct: pctNumber(entry.used_pct),
        complementPct: pctNumber(entry.remaining_pct),
        usedText: pctText(entry.used_pct),
        complementLabel: complementLabel(mode),
        complementText: pctText(entry.remaining_pct),
        coverageText: coverage.join(" · "),
        accountCount: accounts,
        cycleCount: cycles,
        estimated: estimated > 0,
        empty: accounts === 0 || pctNumber(entry.used_pct) === null,
      };
    })
    .sort((a, b) => providerRank(a.provider) - providerRank(b.provider) || a.label.localeCompare(b.label));
}

// Primary windows first (the aggregate's inputs), in the estate provider
// order and then by account ordinal; model caps (a Claude Fable seven-day
// cap) after them, visibly separate — they are never a denominator entry.
export function sortWeeklyWindows(windows: readonly WeeklyWindow[]): WeeklyWindow[] {
  const rows = Array.isArray(windows) ? windows.slice() : [];
  return rows.sort((a, b) => (
    Number(a.role === "model_cap") - Number(b.role === "model_cap")
    || providerRank(a.provider) - providerRank(b.provider)
    || (Number(a.provider_account_index) || 0) - (Number(b.provider_account_index) || 0)
    || String(a.quota_name).localeCompare(String(b.quota_name))
  ));
}

export function weeklyWindowKey(row: Pick<WeeklyWindow, "provider" | "provider_account_index" | "quota_name">): string {
  return `${row.provider}-${row.provider_account_index}-${row.quota_name}`;
}

// A provider can replace a target and later return to that same timestamp.
// Keep those evidence records distinct during React reconciliation.
export function weeklyCycleKey(
  window: Pick<WeeklyWindow, "provider" | "provider_account_index" | "quota_name">,
  cycle: Pick<WeeklyCycle, "resets_at" | "last_observed_at">,
  index: number,
): string {
  return `${weeklyWindowKey(window)}-${cycle.resets_at}-${cycle.last_observed_at ?? "unknown"}-${index}`;
}

// The contract's exclusion reasons, in plain words. A decreased counter is
// reported as what was seen — never as a new week or a provider-side cause.
const CYCLE_REASON_TEXT: Readonly<Record<string, string>> = Object.freeze({
  counter_decreased: "counter decreased mid-window",
  missing_tail: "no sample within 3h of reset",
  collection_gap: "collection gap",
  early_boundary_replacement: "reset target replaced early",
  manual_reset: "manual reset",
  unconfirmed_completion: "rollover not confirmed",
});

export function cycleReasonText(reason: unknown): string {
  const key = String(reason ?? "").trim();
  if (!key) return "unspecified";
  return CYCLE_REASON_TEXT[key] || key.replace(/_+/g, " ");
}

export function humanizeToken(value: unknown): string {
  const text = String(value ?? "").trim();
  return text ? text.replace(/_+/g, " ") : "—";
}

// "20m before reset" — how close the last sample sat to the observed reset.
export function sampleLeadText(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s before reset`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m before reset`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return `${hours}h${rest ? ` ${rest}m` : ""} before reset`;
}

export interface WeeklyCompactLine {
  usedText: string;
  complementLabel: string;
  complementText: string;
  note: string;
}

export interface WeeklyCompactSummary {
  current: WeeklyCompactLine;
  completed: WeeklyCompactLine;
}

// The Now strip: two lines, no methodology. Exclusions and reasons live on
// the full panel the strip links to.
export function compactWeeklySummary(payload: WeeklyUtilizationPayload): WeeklyCompactSummary {
  const current = payload.current;
  const completed = payload.completed;
  const currentNote: string[] = [];
  const currentAccounts = Number(current.account_count) || 0;
  currentNote.push(currentAccounts ? plural(currentAccounts, "account") : "no contributing accounts");
  if (Number(current.estimated_account_count) > 0) currentNote.push(`${current.estimated_account_count} estimated`);
  const completedNote: string[] = [];
  const completedAccounts = Number(completed.account_count) || 0;
  completedNote.push(completedAccounts ? plural(completedAccounts, "account") : "no contributing accounts");
  if (completedAccounts) completedNote.push(plural(Number(completed.cycle_count) || 0, "cycle"));
  if (Number(completed.estimated_account_count) > 0) completedNote.push(`${completed.estimated_account_count} estimated`);
  completedNote.push(`last ${plural(Number(payload.weeks) || 0, "window")}`);
  return {
    current: {
      usedText: pctText(current.used_pct),
      complementLabel: complementLabel("current"),
      complementText: pctText(current.remaining_pct),
      note: currentNote.join(" · "),
    },
    completed: {
      usedText: pctText(completed.used_pct),
      complementLabel: complementLabel("completed"),
      complementText: pctText(completed.remaining_pct),
      note: completedNote.join(" · "),
    },
  };
}

function isSummary(value: unknown): value is WeeklySummary {
  return Boolean(value) && typeof value === "object" && Array.isArray((value as WeeklySummary).providers);
}

// The loader refuses a payload the panel could not render honestly, so a
// malformed response reads "unavailable" rather than an empty aggregate.
export function isWeeklyUtilizationPayload(value: unknown): value is WeeklyUtilizationPayload {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<WeeklyUtilizationPayload>;
  if (!isSummary(candidate.current) || !isSummary(candidate.completed)) return false;
  if (!Array.isArray(candidate.windows)) return false;
  if (!candidate.coverage || typeof candidate.coverage !== "object") return false;
  if (candidate.notes !== undefined && !Array.isArray(candidate.notes)) return false;
  return true;
}
