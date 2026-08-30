export interface LaneDetail {
  lane?: unknown;
  status?: unknown;
  ceded?: unknown;
  last_error_class?: unknown;
  [key: string]: unknown;
}

export interface Freshness {
  status?: unknown;
  latest_at?: unknown;
  lanes?: LaneDetail[];
  stale_lanes?: unknown[];
  threshold_seconds?: unknown;
  max_age_seconds?: unknown;
  [key: string]: unknown;
}

export function staleLaneSuffix(freshness: Freshness): string {
  // Name the lane that stopped reporting. A bare "stale" badge sent an
  // operator to the whole pipeline when one source was the problem.
  const stale = Array.isArray(freshness.stale_lanes) ? freshness.stale_lanes : [];
  if (!stale.length) return "";
  const detail = Array.isArray(freshness.lanes) ? freshness.lanes : [];
  const named = stale.map((name) => {
    const lane = detail.find((entry) => entry.lane === name) || {};
    if (lane.status === "never") return `${name} (no successful run on record)`;
    if (lane.last_error_class) return `${name} (${lane.last_error_class})`;
    return String(name);
  });
  return ` Stale lane${named.length > 1 ? "s" : ""}: ${named.join(", ")}.`;
}

export function cededLanes(freshness: Freshness): LaneDetail[] {
  const lanes = Array.isArray(freshness.lanes) ? freshness.lanes : [];
  return lanes.filter((lane) => lane && lane.ceded === true);
}
