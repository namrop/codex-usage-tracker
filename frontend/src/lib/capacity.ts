import { PROVIDER_ORDER, accountDisplayLabel } from "./providers";
import { QUOTA_WEEKLY_NAMES, isPercentQuotaRow, quotaRowProviderKey, type QuotaRow } from "./quota";
import { projectionStatusOf, type BurnProjectionStatus } from "./burn";
import { isWholePointNative, nativePercentText, wholePointLabel } from "./nativePercent";

// Account-level capacity summary (keeper authorization 2026-09-14). One
// account per row, every window the provider reports in that window's OWN
// units, and the window that binds named outright. The laws this module
// keeps, in the order they cost real reading on the live page:
//
//   - raw units stay raw: a percentage of a cap, an amount of a balance and
//     a count of requests are three different things and are never summed,
//     averaged or combined across accounts or providers;
//   - a model-specific cap (Claude's Fable week) at 100% does not make the
//     account exhausted — it constrains that model and says so — and a tool
//     quota (Z.AI's monthly web search) at its cap says nothing about model
//     inference;
//   - a zero balance binds no matter what other allowances say (OpenRouter's
//     API-key limit does not spend credit the account does not hold), and a
//     key allowance is metered by what the provider says remains of it,
//     never by the key's lifetime usage;
//   - a request-counted limit with no cap denominator (a rate-limited search
//     backend reporting "0 remaining") is a tool constraint, not a balance;
//   - a stale observation asserts nothing about now: it is shown, marked, and
//     excluded from the state the row claims.
//
// This is presentation over the subscriptions `latest` rows and the burn
// series' projection status. It steers nothing, enforces nothing and
// alerts no one.

export type CapacityWindowKind = "model" | "model_specific" | "tool" | "allowance" | "balance";
export type CapacityState = "exhausted" | "constrained" | "available" | "stale" | "unknown";
// The row's confidence with the backend's precedence: partial coverage
// outranks the measurement (dashboard._quota_row_confidence).
export type CapacityConfidence = "exact" | "estimated" | "partial" | "unknown";
// What the provider measured, kept beside the coverage so a partial row
// still says whether its figures were exact or estimated.
export type CapacityMeasurement = "exact" | "estimated" | "unknown";

export interface CapacityProjection {
  status: BurnProjectionStatus;
  reason: string | null;
  exhaustionAt: string | null;
  ratePctPerHour: number | null;
}

export interface CapacityWindow {
  quotaName: string;
  displayName: string;
  kind: CapacityWindowKind;
  // "Fable" for seven_day_fable; null for general windows.
  modelTag: string | null;
  unit: string;
  harness: string;
  usedValue: number | null;
  limitValue: number | null;
  remainingValue: number | null;
  // Fraction of the cap used, for windows that HAVE a cap (never balances).
  usedPct: number | null;
  // The used percentage as text for percent windows: an integer when the
  // provider reported whole points (native OpenCode Go), one decimal
  // otherwise; null for balances, dollar caps and counted quotas.
  usedText: string | null;
  // The provider reported this reading at whole-point resolution.
  wholePoint: boolean;
  // "provider-reported · whole points" for such rows, else null.
  resolutionLabel: string | null;
  // The provider's own state on the row ("ok", "rate_limited"), or null.
  providerState: string | null;
  // What remains, in the window's own units ("94% left", "$16.00 of $20.00
  // left", "0 of 4000 requests left", "$0.00 balance").
  remainingText: string;
  confidence: CapacityConfidence;
  measured: CapacityMeasurement;
  observedAt: number | null;
  ageSeconds: number | null;
  stale: boolean;
  staleReason: "observation_stale" | "reset_passed_since_observation" | null;
  resetAt: number | null;
  weekly: boolean;
  // At its cap (percent/count windows) or empty (balances).
  atCap: boolean;
  projection: CapacityProjection | null;
}

export interface CapacityAccount {
  key: string;
  // Five-hour windows left out because a weekly or monthly cap of this
  // account is spent (see spentLongCap).
  hiddenFiveHour: number;
  provider: string;
  label: string;
  accountIndex: number;
  accountCount: number;
  state: CapacityState;
  // The window that decides the state, first in the row.
  binding: CapacityWindow | null;
  headline: string;
  // A semantic note the headline cannot carry (Fable-only, tool-only,
  // balance-binds), or null.
  note: string | null;
  windows: CapacityWindow[];
  newestObservedAt: number | null;
}

export interface CapacityOptions {
  now: number;
  // Observations older than this are stale — the same threshold chain the
  // subscription cards and the reset rail use.
  maxAgeSeconds: number;
  burnSeries?: any[];
}

// The visual "near limit" convention shared with the rail chips
// (chipRole's 90% violation band). A display band, not a policy: nothing
// acts on it.
export const NEAR_LIMIT_PCT = 90;

const BALANCE_UNITS: ReadonlySet<string> = new Set(["usd", "credits"]);
const BALANCE_WINDOW_KINDS: ReadonlySet<string> = new Set(["lifetime", "unknown"]);
const TOOL_UNITS: ReadonlySet<string> = new Set(["requests", "searches", "calls"]);
const TOOL_NAME_RE = /(search|tool|extract|fetch)/i;
// General window names the providers report; a name that extends one of
// these with a suffix ("seven_day_fable") is a model-specific cap on that
// same window. Data-driven so a new model cap needs no code change.
const GENERAL_WINDOW_NAMES: readonly string[] = Object.freeze([
  "five_hour", "seven_day", "week", "month", "day", "session",
]);

function num(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function splitModelSpecific(quotaName: string): { base: string; tag: string } | null {
  for (const base of GENERAL_WINDOW_NAMES) {
    if (quotaName.startsWith(`${base}_`) && quotaName.length > base.length + 1) {
      const suffix = quotaName.slice(base.length + 1);
      if (TOOL_NAME_RE.test(suffix)) return null;
      return { base, tag: suffix };
    }
  }
  return null;
}

function titleCase(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function capacityWindowKind(row: {
  quota_name?: unknown;
  unit?: unknown;
  window_kind?: unknown;
  used_value?: unknown;
  limit_value?: unknown;
} | null | undefined): CapacityWindowKind {
  if (!row) return "model";
  const quotaName = String(row.quota_name || "");
  const unit = String(row.unit || "").trim().toLowerCase();
  const windowKind = String(row.window_kind || "").trim().toLowerCase();
  const hasLimit = num(row.limit_value) !== null;
  const hasRatio = num(row.used_value) !== null && hasLimit;
  // A lifetime window is a balance outright.
  if (windowKind === "lifetime") return "balance";
  // A tool quota is a tool quota whatever its window says: a rate-limited
  // search backend reports "unknown" window, "requests" unit and nothing
  // but a zero remaining — that is a constraint on the tool, not an empty
  // account balance.
  if (TOOL_NAME_RE.test(quotaName) || TOOL_UNITS.has(unit)) return "tool";
  // A balance-denominated "unknown" window with a provider-reported limit
  // is a key allowance (OpenRouter's api_key_quota): a cap listed beside
  // the credit balance, never folded into it and never a model window.
  if (BALANCE_UNITS.has(unit) && BALANCE_WINDOW_KINDS.has(windowKind) && hasLimit) return "allowance";
  // An "unknown" window with no cap to fill toward is a balance.
  if (BALANCE_WINDOW_KINDS.has(windowKind) && !hasRatio) return "balance";
  if (BALANCE_UNITS.has(unit) && !hasRatio) return "balance";
  if (splitModelSpecific(quotaName)) return "model_specific";
  return "model";
}

function amountText(value: number, unit: string): string {
  const lowered = unit.trim().toLowerCase();
  if (lowered === "usd") return `${value < 0 ? "−" : ""}$${Math.abs(value).toFixed(2)}`;
  const shown = Number.isInteger(value) ? String(value) : value.toFixed(2);
  if (lowered === "percent" || lowered === "%") return `${shown}%`;
  return unit ? `${shown} ${unit}` : shown;
}

function measurementOf(row: any): CapacityMeasurement {
  const measured = String(row.measurement_confidence || "").trim().toLowerCase();
  if (measured === "exact") return "exact";
  if (measured === "estimated") return "estimated";
  return "unknown";
}

// Same precedence as the backend's _quota_row_confidence: partial coverage
// first, then the measurement. A partial row is never "Exact" alone.
function confidenceOf(row: any): CapacityConfidence {
  if (String(row.coverage_status || "").trim().toLowerCase() === "partial") return "partial";
  const measured = String(row.measurement_confidence || "").trim().toLowerCase();
  if (measured === "exact") return "exact";
  if (measured === "estimated") return "estimated";
  if (measured === "partial") return "partial";
  return "unknown";
}

// The badge text: "Exact", "Estimated", or for partial coverage "Partial ·
// exact measurement" / "Partial · estimated measurement", so the coverage
// gap and the measurement quality both stay visible.
export function confidenceText(window: Pick<CapacityWindow, "confidence" | "measured">): string {
  if (window.confidence === "partial" && window.measured !== "unknown") {
    return `Partial · ${window.measured} measurement`;
  }
  return confidenceLabel(window.confidence);
}

export function confidenceLabel(confidence: CapacityConfidence): string {
  switch (confidence) {
    case "exact":
      return "Exact";
    case "estimated":
      return "Estimated";
    case "partial":
      return "Partial";
    default:
      return "Unknown";
  }
}

function findProjection(row: any, canonical: string, accountIndex: number, burnSeries: any[] | undefined): CapacityProjection | null {
  if (!Array.isArray(burnSeries) || !burnSeries.length) return null;
  const quotaName = String(row.quota_name || "");
  const harness = String(row.harness || "");
  const match = burnSeries.find((series) => {
    if (!series || String(series.provider || "") !== canonical) return false;
    if (String(series.quota_name || "") !== quotaName) return false;
    if (series.harness !== undefined && String(series.harness || "") !== harness) return false;
    const seriesIndex = Number(series.provider_account_index);
    const normalized = Number.isSafeInteger(seriesIndex) && seriesIndex >= 1 ? seriesIndex : 1;
    return normalized === accountIndex;
  });
  if (!match) return null;
  const { status, reason } = projectionStatusOf(match);
  return {
    status,
    reason,
    exhaustionAt: match.projected_exhaustion_at ? String(match.projected_exhaustion_at) : null,
    ratePctPerHour: match.burn_rate_pct_per_hour == null ? null : Number(match.burn_rate_pct_per_hour),
  };
}

export function deriveCapacityWindow(row: any, options: CapacityOptions, canonical: string, accountIndex: number): CapacityWindow {
  const quotaName = String(row.quota_name || "unknown");
  const kind = capacityWindowKind(row);
  const split = kind === "model_specific" ? splitModelSpecific(quotaName) : null;
  const unit = String(row.unit || "");
  const used = num(row.used_value);
  const limit = num(row.limit_value);
  const remaining = num(row.remaining_value);
  const percentRow = isPercentQuotaRow(row);
  let usedPct: number | null = null;
  if (kind === "allowance") {
    // The provider reports what remains of the key's current allowance;
    // used_value on this row is the key's LIFETIME usage (OpenRouter
    // `usage`), which says nothing about the current allowance and is kept
    // only as a raw source field.
    if (remaining !== null && limit !== null && limit > 0) {
      usedPct = Math.max(0, Math.min(100, ((limit - remaining) / limit) * 100));
    }
  } else if (kind !== "balance" && used !== null && limit !== null && limit > 0) {
    usedPct = Math.max(0, Math.min(100, (used / limit) * 100));
  }
  const displayUnit = percentRow ? "percent" : unit;

  let remainingText = "Unavailable";
  if (kind === "balance") {
    if (remaining !== null) remainingText = `${amountText(remaining, unit)} balance`;
    else if (used !== null && limit !== null) remainingText = `${amountText(Math.max(0, limit - used), unit)} balance`;
  } else if (kind === "allowance") {
    if (limit !== null && remaining !== null) {
      remainingText = displayUnit.trim().toLowerCase() === "usd"
        ? `${amountText(remaining, displayUnit)} of ${amountText(limit, displayUnit)} left`
        : `${amountText(remaining, "")} of ${amountText(limit, displayUnit)} left`;
    } else if (limit !== null) {
      remainingText = `${amountText(limit, displayUnit)} allowance · remaining not reported`;
    }
  } else if (percentRow) {
    if (usedPct !== null) remainingText = `${(100 - usedPct).toFixed(0)}% left`;
    else if (remaining !== null) remainingText = `${remaining.toFixed(0)}% left`;
  } else if (limit !== null && (remaining !== null || used !== null)) {
    const left = remaining !== null ? remaining : Math.max(0, limit - (used as number));
    // Dollar caps carry the sign on each figure; counted units name the
    // unit once ("0 of 4000 requests left").
    remainingText = displayUnit.trim().toLowerCase() === "usd"
      ? `${amountText(left, displayUnit)} of ${amountText(limit, displayUnit)} left`
      : `${amountText(left, "")} of ${amountText(limit, displayUnit)} left`;
  } else if (remaining !== null) {
    remainingText = `${amountText(remaining, displayUnit)} left`;
  }

  const observedAt = Date.parse(String(row.observed_at || ""));
  const observed = Number.isFinite(observedAt) ? observedAt : null;
  const ageSeconds = observed === null ? null : Math.max(0, (options.now - observed) / 1000);
  const resetParsed = Date.parse(String(row.resets_at || ""));
  const resetAt = Number.isFinite(resetParsed) ? resetParsed : null;

  let staleReason: CapacityWindow["staleReason"] = null;
  if (ageSeconds === null || ageSeconds > options.maxAgeSeconds) {
    staleReason = "observation_stale";
  } else if (kind !== "balance" && resetAt !== null && observed !== null && resetAt <= options.now && resetAt > observed) {
    staleReason = "reset_passed_since_observation";
  }

  // At its cap: a balance with nothing left; an allowance the provider
  // reports as spent; a capped window at 100%; or a window that reports a
  // zero remaining without a denominator (no percentage, still a
  // constraint).
  let atCap: boolean;
  if (kind === "balance") {
    atCap = remaining !== null ? remaining <= 0 : used !== null && limit !== null && used >= limit;
  } else if (kind === "allowance") {
    atCap = remaining !== null && remaining <= 0;
  } else {
    atCap = usedPct !== null ? usedPct >= 100 : remaining !== null && remaining <= 0;
  }

  const base = split ? split.base : quotaName;
  const providerState = row.provider_state === null || row.provider_state === undefined || row.provider_state === ""
    ? null
    : String(row.provider_state);
  return {
    quotaName,
    displayName: split ? `${base.replace(/_/g, " ")} · ${titleCase(split.tag)}` : quotaName.replace(/_/g, " "),
    kind,
    modelTag: split ? titleCase(split.tag) : null,
    unit,
    harness: String(row.harness || ""),
    usedValue: used,
    limitValue: limit,
    remainingValue: remaining,
    usedPct,
    usedText: percentRow && kind !== "balance" && usedPct !== null ? nativePercentText(usedPct, row) : null,
    wholePoint: isWholePointNative(row),
    resolutionLabel: wholePointLabel(row),
    providerState,
    remainingText,
    confidence: confidenceOf(row),
    measured: measurementOf(row),
    observedAt: observed,
    ageSeconds,
    stale: staleReason !== null,
    staleReason,
    resetAt,
    weekly: QUOTA_WEEKLY_NAMES.has(quotaName) || QUOTA_WEEKLY_NAMES.has(base),
    atCap,
    projection: findProjection(row, canonical, accountIndex, options.burnSeries),
  };
}

// When to show five-hour windows. Keeper, 2026-09-25 (Discord unified usage
// ledger thread, msg 1553031185041461300): "I think we need more
// intelligence about when to show 5 hour cards. They are not useful if the
// weekly or monthly cap is hit, across all providers". An account whose
// fresh general weekly or monthly window is at its cap shows no five-hour
// window on the Now view (capacity, burn rate, reset rail, provider detail)
// until that cap resets or its reading goes stale. A model-specific cap
// (Claude's Fable week) or a tool quota (Z.AI's monthly web search) does not
// stop the account, so it hides nothing. The History charts and the raw
// observation ledger keep every window.
const LONG_CAP_NAMES: ReadonlySet<string> = new Set(["week", "seven_day", "month"]);

export function isFiveHourQuotaName(name: unknown): boolean {
  const value = String(name || "");
  return value === "five_hour" || value.startsWith("five_hour_");
}

// The account key every per-account surface shares ("kimi-k3-coding:1").
// Works for subscriptions `latest` rows and quota-burn series alike.
export function quotaAccountKey(row: { provider?: unknown; provider_canonical?: unknown; provider_account_index?: unknown }): string {
  const index = Number(row.provider_account_index);
  return `${quotaRowProviderKey(row) || "unknown"}:${Number.isSafeInteger(index) && index >= 1 ? index : 1}`;
}

// The spent weekly or monthly cap that makes this account's five-hour
// windows moot, or null.
export function spentLongCap(windows: CapacityWindow[]): CapacityWindow | null {
  return windows.find((window) => (
    !window.stale && window.kind === "model" && LONG_CAP_NAMES.has(window.quotaName) && window.atCap
  )) || null;
}

// Account key → the spent cap's display name, for every account whose
// five-hour windows are hidden.
export function fiveHourHiddenAccounts(
  rows: QuotaRow[] | undefined | null,
  options: Pick<CapacityOptions, "now" | "maxAgeSeconds">,
): Map<string, string> {
  const byAccount = new Map<string, CapacityWindow[]>();
  (Array.isArray(rows) ? rows : []).forEach((row) => {
    if (!row) return;
    const key = quotaAccountKey(row);
    const index = Number(key.slice(key.lastIndexOf(":") + 1));
    const window = deriveCapacityWindow(row, options, quotaRowProviderKey(row) || "unknown", index);
    if (!byAccount.has(key)) byAccount.set(key, []);
    byAccount.get(key)!.push(window);
  });
  const hidden = new Map<string, string>();
  byAccount.forEach((windows, key) => {
    const cap = spentLongCap(windows);
    if (cap) hidden.set(key, cap.displayName);
  });
  return hidden;
}

// True for a five-hour row or series of an account in `hidden`.
export function hidesFiveHour(
  hidden: ReadonlyMap<string, string> | null | undefined,
  row: { quota_name?: unknown; provider?: unknown; provider_canonical?: unknown; provider_account_index?: unknown } | null | undefined,
): boolean {
  if (!hidden || !hidden.size || !row) return false;
  return isFiveHourQuotaName(row.quota_name) && hidden.has(quotaAccountKey(row));
}

const STATE_RANK: Record<CapacityState, number> = {
  exhausted: 0,
  constrained: 1,
  available: 2,
  stale: 3,
  unknown: 4,
};

function providerRank(provider: string): number {
  const index = PROVIDER_ORDER.indexOf(provider);
  return index === -1 ? PROVIDER_ORDER.length : index;
}

function highestUsed(windows: CapacityWindow[]): CapacityWindow | null {
  let best: CapacityWindow | null = null;
  let bestPct = -1;
  for (const window of windows) {
    if (window.usedPct === null) continue;
    if (window.usedPct > bestPct) {
      best = window;
      bestPct = window.usedPct;
    }
  }
  return best;
}

export function ageText(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "never observed";
  if (seconds < 90) return "observed just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `observed ${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `observed ${hours}h ${String(minutes % 60).padStart(2, "0")}m ago`;
  return `observed ${Math.floor(hours / 24)}d ago`;
}

// One clause per window, in the window's own units: percent windows say
// "% used"; balances, dollar caps and counted quotas say what is left.
function windowSummary(window: CapacityWindow): string {
  const percentUnit = window.unit.trim().toLowerCase() === "percent"
    || (window.unit.trim().toLowerCase() === "provider_unit" && window.limitValue === 100);
  if (window.kind !== "balance" && percentUnit && window.usedPct !== null) {
    return `${window.displayName} ${window.usedPct.toFixed(0)}% used`;
  }
  return `${window.displayName} ${window.remainingText}`;
}

function accountState(windows: CapacityWindow[]): {
  state: CapacityState;
  binding: CapacityWindow | null;
  headline: string;
  note: string | null;
} {
  if (!windows.length) {
    return { state: "unknown", binding: null, headline: "No quota windows observed.", note: null };
  }
  const fresh = windows.filter((window) => !window.stale);
  if (!fresh.length) {
    const newest = windows.reduce<CapacityWindow | null>((best, window) => (
      !best || (window.observedAt || 0) > (best.observedAt || 0) ? window : best
    ), null);
    const reason = newest && newest.staleReason === "reset_passed_since_observation"
      ? "the window reset after the last sample"
      : `last sample ${ageText(newest ? newest.ageSeconds : null).replace(/^observed /, "")}`;
    return {
      state: "stale",
      binding: newest,
      headline: `No fresh observation (${reason}); current capacity cannot be asserted.`,
      note: null,
    };
  }

  const emptyBalance = fresh.find((window) => window.kind === "balance" && window.atCap);
  if (emptyBalance) {
    const allowances = fresh.filter((window) => window !== emptyBalance && window.kind !== "balance");
    return {
      state: "exhausted",
      binding: emptyBalance,
      headline: `${windowSummary(emptyBalance)} — no usable balance.`,
      note: allowances.length
        ? `${allowances.map(windowSummary).join("; ")} — an allowance does not restore a balance the account does not hold.`
        : null,
    };
  }

  const generalWindows = fresh.filter((window) => window.kind === "model");
  const generalAtCap = generalWindows.find((window) => window.atCap);
  if (generalAtCap) {
    return {
      state: "exhausted",
      binding: generalAtCap,
      headline: `${windowSummary(generalAtCap)} — exhausted until it resets.`,
      note: null,
    };
  }

  const busiestGeneral = highestUsed(generalWindows);
  const modelCap = fresh.find((window) => window.kind === "model_specific" && window.atCap);
  if (modelCap) {
    const tag = modelCap.modelTag || "that model";
    return {
      state: "constrained",
      binding: modelCap,
      headline: `${windowSummary(modelCap)} — ${tag} requests are capped; the account is not exhausted.`,
      note: busiestGeneral
        ? `General windows still have room (${windowSummary(busiestGeneral)}); only ${tag} is blocked.`
        : `Only ${tag} is blocked by this window.`,
    };
  }
  const toolCap = fresh.find((window) => window.kind === "tool" && window.atCap);
  if (toolCap) {
    return {
      state: "constrained",
      binding: toolCap,
      headline: `${windowSummary(toolCap)} — tool quota exhausted.`,
      note: busiestGeneral
        ? `Model inference is unaffected by this quota (${windowSummary(busiestGeneral)}).`
        : "Model inference is unaffected by this quota.",
    };
  }
  // A spent key allowance blocks that key while the account still holds
  // credit: constrained, and the balance that remains is named.
  const allowanceCap = fresh.find((window) => window.kind === "allowance" && window.atCap);
  if (allowanceCap) {
    const balance = fresh.find((window) => window.kind === "balance");
    return {
      state: "constrained",
      binding: allowanceCap,
      headline: `${windowSummary(allowanceCap)} — key allowance spent; the account balance is not exhausted.`,
      note: balance ? `${windowSummary(balance)} remains; only this key's allowance is spent.` : null,
    };
  }
  if (busiestGeneral && busiestGeneral.usedPct !== null && busiestGeneral.usedPct >= NEAR_LIMIT_PCT) {
    return {
      state: "constrained",
      binding: busiestGeneral,
      headline: `${windowSummary(busiestGeneral)} — near its cap.`,
      note: null,
    };
  }
  const binding = busiestGeneral || highestUsed(fresh) || fresh[0];
  return {
    state: "available",
    binding,
    headline: `${windowSummary(binding)}.`,
    note: null,
  };
}

function orderWindows(windows: CapacityWindow[], binding: CapacityWindow | null): CapacityWindow[] {
  return windows.slice().sort((left, right) => {
    if (left === binding) return -1;
    if (right === binding) return 1;
    if (left.stale !== right.stale) return left.stale ? 1 : -1;
    if (left.atCap !== right.atCap) return left.atCap ? -1 : 1;
    const leftPct = left.usedPct === null ? -1 : left.usedPct;
    const rightPct = right.usedPct === null ? -1 : right.usedPct;
    if (leftPct !== rightPct) return rightPct - leftPct;
    return left.quotaName.localeCompare(right.quotaName);
  });
}

export function deriveCapacityAccounts(rows: QuotaRow[] | undefined | null, options: CapacityOptions): CapacityAccount[] {
  const grouped = new Map<string, { provider: string; accountIndex: number; accountCount: number; label: string; rows: any[] }>();
  (Array.isArray(rows) ? rows : []).forEach((row) => {
    if (!row) return;
    const canonical = quotaRowProviderKey(row) || "unknown";
    const index = Number(row.provider_account_index);
    const count = Number(row.provider_account_count);
    const accountIndex = Number.isSafeInteger(index) && index >= 1 ? index : 1;
    const accountCount = Number.isSafeInteger(count) && count >= 1 ? count : 1;
    const key = `${canonical}:${accountIndex}`;
    if (!grouped.has(key)) {
      grouped.set(key, {
        provider: canonical,
        accountIndex,
        accountCount,
        label: accountDisplayLabel(row),
        rows: [],
      });
    }
    grouped.get(key)!.rows.push(row);
  });

  const accounts: CapacityAccount[] = Array.from(grouped.entries()).map(([key, group]) => {
    const derived = group.rows.map((row) => deriveCapacityWindow(row, options, group.provider, group.accountIndex));
    const windows = spentLongCap(derived)
      ? derived.filter((window) => !isFiveHourQuotaName(window.quotaName))
      : derived;
    const { state, binding, headline, note } = accountState(windows);
    const observedTimes = windows.map((window) => window.observedAt).filter((value): value is number => value !== null);
    return {
      key,
      hiddenFiveHour: derived.length - windows.length,
      provider: group.provider,
      label: group.label,
      accountIndex: group.accountIndex,
      accountCount: group.accountCount,
      state,
      binding,
      headline,
      note,
      windows: orderWindows(windows, binding),
      newestObservedAt: observedTimes.length ? Math.max(...observedTimes) : null,
    };
  });

  return accounts.sort((left, right) => {
    const rank = STATE_RANK[left.state] - STATE_RANK[right.state];
    if (rank !== 0) return rank;
    const provider = providerRank(left.provider) - providerRank(right.provider);
    if (provider !== 0) return provider;
    if (left.provider !== right.provider) return left.provider.localeCompare(right.provider);
    return left.accountIndex - right.accountIndex;
  });
}

export function capacityStateLabel(state: CapacityState): string {
  switch (state) {
    case "exhausted":
      return "Exhausted";
    case "constrained":
      return "Constrained";
    case "available":
      return "Available";
    case "stale":
      return "Stale";
    default:
      return "Unknown";
  }
}
