// Shared formatting helpers — ported verbatim from the pre-React inline
// dashboard script (templates/index.html, retired by the React + Vite
// conversion). Timezone policy: UTC-bucketed surfaces (anything headed
// "UTC hour") render through formatUtcHour; point-in-time sample timestamps
// render local through formatTimestamp, which always carries month and day
// so the frame is readable.

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
