import { accountDisplayLabel } from "./providers";
import { quotaConfidenceLabel } from "./quota";

// Burn rows (2026-09-14): the /api/quota-burn series projected for the
// table. The backend owns the arithmetic (rate at the observed active-use
// rate over the last monotonic rises, no forecast beyond the window's own
// reset) and the claim it can make — `projection_status` — and this module
// only turns that enum into words. Nothing here recomputes a rate.

export type BurnProjectionStatus =
  | "exhausted"
  | "projected_before_reset"
  | "resets_first"
  | "insufficient_observations"
  | "not_applicable"
  | "stale";

export const BURN_PROJECTION_STATUSES: readonly BurnProjectionStatus[] = Object.freeze([
  "exhausted",
  "projected_before_reset",
  "resets_first",
  "insufficient_observations",
  "not_applicable",
  "stale",
]);

export interface BurnRowModel {
  key: string;
  // "Claude 2" — the same label the capacity summary and the rail use.
  label: string;
  provider: string;
  quotaName: string;
  windowName: string;
  windowKind: string;
  unit: string;
  harness: string;
  accountIndex: number;
  accountCount: number;
  confidenceStatus: string;
  confidenceLabel: string;
  latestPct: number | null;
  latestObservedAt: string | null;
  rate: number | null;
  rateWindowDeltas: number;
  observationCount: number;
  resetsAt: string | null;
  secondsToReset: number | null;
  exhaustionAt: string | null;
  exhausted: boolean;
  status: BurnProjectionStatus;
  reason: string | null;
  spark: { pct: number; at: string }[];
}

export function windowDisplayName(quotaName: string): string {
  return quotaName.replace(/_/g, " ");
}

function isStatus(value: unknown): value is BurnProjectionStatus {
  return typeof value === "string" && (BURN_PROJECTION_STATUSES as readonly string[]).includes(value);
}

// Servers before 2026-09-14 carry no projection_status; derive the closest
// honest reading from the fields they do send. Only the two states the old
// payload could actually assert are reconstructed — everything else is
// "insufficient_observations" with no reason, never a guessed forecast.
export function projectionStatusOf(row: any): { status: BurnProjectionStatus; reason: string | null } {
  if (isStatus(row && row.projection_status)) {
    const reason = row.projection_reason;
    return { status: row.projection_status, reason: reason == null ? null : String(reason) };
  }
  if (row && row.exhausted === true) return { status: "exhausted", reason: "used_at_cap" };
  if (row && row.projected_exhaustion_at) {
    return { status: "projected_before_reset", reason: "empty_before_reset_at_observed_rate" };
  }
  return { status: "insufficient_observations", reason: null };
}

export function deriveBurnRows(series: any[]): BurnRowModel[] {
  return (Array.isArray(series) ? series : []).map((row, index) => {
    const observations: any[] = Array.isArray(row.observations) ? row.observations : [];
    const { status, reason } = projectionStatusOf(row);
    const accountIndex = Number(row.provider_account_index);
    const accountCount = Number(row.provider_account_count);
    const confidenceStatus = String(row.confidence_status || row.measurement_confidence || "unknown").toLowerCase();
    return {
      key: `${row.provider}-${row.quota_name}-${row.harness || ""}-${Number.isSafeInteger(accountIndex) ? accountIndex : index}`,
      label: accountDisplayLabel(row),
      provider: String(row.provider || "unknown"),
      quotaName: String(row.quota_name || "window"),
      windowName: windowDisplayName(String(row.quota_name || "window")),
      windowKind: String(row.window_kind || "unknown"),
      unit: String(row.unit || ""),
      harness: String(row.harness || ""),
      accountIndex: Number.isSafeInteger(accountIndex) && accountIndex >= 1 ? accountIndex : 1,
      accountCount: Number.isSafeInteger(accountCount) && accountCount >= 1 ? accountCount : 1,
      confidenceStatus,
      confidenceLabel: quotaConfidenceLabel(confidenceStatus),
      latestPct: row.latest_used_pct == null ? null : Number(row.latest_used_pct),
      latestObservedAt: row.latest_observed_at ? String(row.latest_observed_at) : null,
      rate: row.burn_rate_pct_per_hour == null ? null : Number(row.burn_rate_pct_per_hour),
      rateWindowDeltas: Number(row.rate_window_deltas || 0),
      observationCount: Number(row.observation_count || observations.length || 0),
      resetsAt: row.resets_at ? String(row.resets_at) : null,
      secondsToReset: row.seconds_to_reset == null ? null : Number(row.seconds_to_reset),
      exhaustionAt: row.projected_exhaustion_at ? String(row.projected_exhaustion_at) : null,
      exhausted: status === "exhausted",
      status,
      reason,
      spark: observations
        .filter((observation) => observation && observation.used_pct != null)
        .map((observation) => ({
          pct: Number(observation.used_pct),
          at: String(observation.observed_at || ""),
        })),
    };
  });
}

// The projection column's words. `exhaustionAtText` is the caller's
// formatted timestamp for projected_before_reset (formatting is a display
// concern with a locale; this module stays pure).
export function projectionText(
  status: BurnProjectionStatus,
  reason: string | null,
  exhaustionAtText: string | null,
): string {
  switch (status) {
    case "exhausted":
      return "exhausted";
    case "projected_before_reset":
      return exhaustionAtText ? `empty ${exhaustionAtText}` : "empty before reset";
    case "resets_first":
      return "resets first";
    case "insufficient_observations":
      return reason === "single_observation" ? "one sample only" : "no rise measured";
    case "not_applicable":
      if (reason === "balance_window") return "n/a · balance";
      if (reason === "no_reset_reported") return "n/a · no reset reported";
      if (reason === "window_longer_than_seven_days") return "n/a · window longer than 7 days";
      if (reason === "reset_not_in_future") return "n/a · reset already passed";
      if (reason === "no_utilization_ratio") return "n/a · no ratio";
      return "n/a";
    case "stale":
      return reason === "reset_passed_since_observation"
        ? "stale · reset passed since sample"
        : "stale sample";
    default:
      return "no rise measured";
  }
}

// Row role for the style layer: the projection is a claim, and only the
// claims that need the reader's eye wear a class.
export function burnRowClass(status: BurnProjectionStatus): string | undefined {
  switch (status) {
    case "exhausted":
      return "burn-exhausted";
    case "projected_before_reset":
      return "burn-projected";
    case "stale":
      return "burn-stale";
    case "not_applicable":
      return "burn-not-applicable";
    default:
      return undefined;
  }
}
