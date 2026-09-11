export interface LaneDetail {
  lane?: unknown;
  status?: unknown;
  ceded?: unknown;
  last_error_class?: unknown;
  [key: string]: unknown;
}

export interface Freshness {
  status?: unknown;
  heartbeat_status?: unknown;
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

// The write-level state on its own. `status` folds lane staleness in (one
// quiet lane makes the surface stale so the badge and notices stay honest);
// the subscription charts gate on this instead, so one ceded lane is named
// in the notice and falls off the chart at the hold window without hiding
// every other provider's series (2026-09-10: a Z.AI lane dead for twelve
// hours blanked both utilization charts while eight lanes wrote on
// schedule). Servers before that date carry no heartbeat_status; derive it
// from the heartbeat write time against the same threshold.
export function heartbeatStatus(freshness: Freshness, now: number): string {
  const explicit = freshness.heartbeat_status;
  if (typeof explicit === "string" && explicit) return explicit;
  const status = String(freshness.status || "unknown");
  if (status === "empty" || status === "unknown") return status;
  const latest = Date.parse(String(freshness.latest_at || ""));
  if (!Number.isFinite(latest)) return status;
  const maxAge = Number(freshness.max_age_seconds);
  const threshold = Number.isFinite(maxAge) && maxAge > 0 ? maxAge : 10800;
  return (now - latest) / 1000 > threshold ? "stale" : "fresh";
}
