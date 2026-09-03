// Shared formatting helpers — ported verbatim from the pre-React inline
// dashboard script (templates/index.html, retired by the React + Vite
// conversion). Timezone policy: UTC-bucketed surfaces render through
// formatUtcBucket (15-minute buckets, keeper-ruled 2026-09-02: "15 minute
// buckets are better across all of the charts") or formatUtcHour where a
// surface is still hourly; point-in-time sample timestamps render local
// through formatTimestamp, which always carries month and day so the frame
// is readable.

export function formatTimestamp(ts: unknown): string {
  if (!ts) return "—";
  const dt = new Date(ts as string);
  if (!Number.isFinite(dt.getTime())) return "—";
  return dt.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatUtcHour(ts: unknown, compact = false): string {
  if (!ts) return "—";
  const dt = new Date(ts as string);
  if (!Number.isFinite(dt.getTime())) return "—";
  if (compact) {
    return `${dt.toLocaleString([], {
      timeZone: "UTC",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      hour12: false,
    }).replace(",", " ·")}h`;
  }
  return `${dt.toLocaleString([], {
    timeZone: "UTC",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  })} UTC`;
}

// Sub-hour bucket label: always carries minutes. Full form "Aug 27, 14:15
// UTC" (tooltip titles, table rows); compact "Aug 27 · 14:15" (axis ticks —
// the full form overran the next tick at 1036px, screenshot-verified).
export function formatUtcBucket(ts: unknown, compact = false): string {
  if (!ts) return "—";
  const dt = new Date(ts as string);
  if (!Number.isFinite(dt.getTime())) return "—";
  const text = dt.toLocaleString([], {
    timeZone: "UTC",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  return compact ? text.replace(",", " ·") : `${text} UTC`;
}

// The bucket width a time_series was cut at; an older backend that carries
// no bucket_minutes was hourly, so 60 keeps it rendering unchanged.
export function bucketMinutesOf(timeSeries: unknown): number {
  const raw = timeSeries && typeof timeSeries === "object"
    ? Number((timeSeries as { bucket_minutes?: unknown }).bucket_minutes)
    : NaN;
  return Number.isFinite(raw) && raw > 0 ? raw : 60;
}

// One label formatter for every bucketed surface, chosen by bucket width.
export function formatBucketLabel(ts: unknown, bucketMinutes: number, compact = false): string {
  return bucketMinutes >= 60 ? formatUtcHour(ts, compact) : formatUtcBucket(ts, compact);
}

// Wording derived from the bucket width so captions, fold labels, table
// headers and aria text describe what the backend actually cut.
export interface BucketWording {
  minutes: number;
  // "UTC hour" | "UTC bucket" — the table's first column header.
  header: string;
  // "complete UTC hours" | "complete 15-minute UTC buckets"
  completePlural: string;
  // "per complete UTC hour" | "per 15-minute UTC bucket"
  per: string;
  // "Hourly" | "15-minute" — the leading adjective on captions/fold labels.
  adjective: string;
  // "hour" | "15-minute bucket" — the fold summary's row noun (plural +s).
  singular: string;
  // "that hour" | "in that bucket"
  that: string;
}

export function bucketWording(bucketMinutes: number): BucketWording {
  if (bucketMinutes >= 60) {
    return {
      minutes: bucketMinutes,
      header: "UTC hour",
      completePlural: "complete UTC hours",
      per: "per complete UTC hour",
      adjective: "Hourly",
      singular: "hour",
      that: "that hour",
    };
  }
  const width = `${bucketMinutes}-minute`;
  return {
    minutes: bucketMinutes,
    header: "UTC bucket",
    completePlural: `complete ${width} UTC buckets`,
    per: `per ${width} UTC bucket`,
    adjective: width,
    singular: `${width} bucket`,
    that: "in that bucket",
  };
}

export function percent(value: unknown, digits = 1): string {
  if (value === null || value === undefined) return "—";
  const asNum = Number(value);
  if (!Number.isFinite(asNum)) return "—";
  return `${asNum.toFixed(digits)}%`;
}

export function intFormat(value: unknown): string {
  const asNum = Number(value);
  if (!Number.isFinite(asNum)) return "—";
  return new Intl.NumberFormat().format(Math.round(asNum));
}

export function compactIntFormat(value: unknown): string {
  const asNum = Number(value);
  if (!Number.isFinite(asNum)) return "—";
  return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(asNum);
}

export function formatApiModes(apiModes: unknown): string {
  // Dominant transport per (provider, model); "unknown" stays visible
  // rather than blank — a NULL here is a fact about the source, and
  // pre-2026-08-29 Hermes rows are NULL forever by decision.
  if (!apiModes || typeof apiModes !== "object") return "unknown";
  const entries = Object.entries(apiModes as Record<string, number>).sort((a, b) => b[1] - a[1]);
  if (!entries.length) return "unknown";
  const [top, count] = entries[0];
  if (entries.length === 1) return top;
  const share = count / entries.reduce((sum, [, n]) => sum + n, 0);
  return `${top} ${Math.round(share * 100)}% +${entries.length - 1}`;
}

export function formatCountdown(ms: number, weekly: boolean): string {
  if (ms <= 0) return "resetting…";
  const s = Math.floor(ms / 1000);
  if (weekly || s >= 86400) return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
  return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}
