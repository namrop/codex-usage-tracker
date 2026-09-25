import type { Palette } from "../lib/palette";
import { bucketMinutesOf, formatUtcHour, intFormat } from "../lib/format";
import { providerDisplayName, subscriptionUnifiedProviderColor } from "../lib/providers";
import { quotaRowProviderKey } from "../lib/quota";

// Provider API status strip · 48 hours: provider status-feed badges over
// observed per-UTC-hour call health, where a dim cell means no traffic
// rather than a clean hour. The strip stays hourly (48 cells) over a
// 15-minute time_series by summing each hour's sub-buckets. Noise fold
// (polish round item 4b): providers named "unknown" and providers with
// under 10 events in the shown window collapse into one dim line; aria
// still names each with its call count.

const STATUS_INDICATOR_ROLE: Record<string, [string, string]> = {
  none: ["role-stability", "operational"],
  minor: ["role-threshold", "degraded"],
  major: ["role-violation", "major outage"],
  critical: ["role-violation", "critical outage"],
  maintenance: ["role-threshold", "maintenance"],
};

function ProviderStatusBadge({ feedEntry }: { feedEntry: any }) {
  if (!feedEntry) {
    return <span className="status-badge state-idle">no status feed</span>;
  }
  if (feedEntry.unreachable) {
    return <span className="status-badge role-threshold">feed unreachable</span>;
  }
  const [role, label] = STATUS_INDICATOR_ROLE[feedEntry.indicator] || ["role-threshold", feedEntry.indicator];
  const text = feedEntry.incidents && feedEntry.incidents.length
    ? `${label} · ${feedEntry.incidents[0]}`
    : label;
  return <span className={`status-badge ${role}`} title={feedEntry.description || ""}>{text}</span>;
}

interface HourlyRow {
  events: number[];
  errors: number[];
  top_error_classes: unknown[];
}

function utcHourFloor(value: unknown): string {
  const dt = new Date(value as string);
  if (!Number.isFinite(dt.getTime())) return String(value);
  dt.setUTCMinutes(0, 0, 0);
  return dt.toISOString();
}

// Fold sub-hour buckets into hourly cells: events and errors sum, and the
// hour's error class is the worst sub-bucket's. Hourly input passes through
// untouched.
function hourlyCells(
  bucketStarts: unknown[],
  bucketMinutes: number,
  rows: any[],
): { hourStarts: unknown[]; hourly: HourlyRow[] } {
  if (bucketMinutes >= 60) {
    return {
      hourStarts: bucketStarts,
      hourly: rows.map((row) => ({
        events: row.events,
        errors: row.errors,
        top_error_classes: Array.isArray(row.top_error_classes) ? row.top_error_classes : [],
      })),
    };
  }
  const hourStarts: string[] = [];
  const hourOf: number[] = [];
  bucketStarts.forEach((start) => {
    const hour = utcHourFloor(start);
    if (hourStarts[hourStarts.length - 1] !== hour) hourStarts.push(hour);
    hourOf.push(hourStarts.length - 1);
  });
  const hourly = rows.map((row) => {
    const events = new Array<number>(hourStarts.length).fill(0);
    const errors = new Array<number>(hourStarts.length).fill(0);
    const worst = new Array<number>(hourStarts.length).fill(0);
    const top = new Array<unknown>(hourStarts.length).fill(null);
    const classes = Array.isArray(row.top_error_classes) ? row.top_error_classes : [];
    hourOf.forEach((hour, index) => {
      const bad = Number(row.errors[index] || 0);
      events[hour] += Number(row.events[index] || 0);
      errors[hour] += bad;
      if (bad > worst[hour]) {
        worst[hour] = bad;
        top[hour] = classes[index] ?? null;
      }
    });
    return { events, errors, top_error_classes: top };
  });
  return { hourStarts, hourly };
}

export function ProviderStatus({ timeSeries, feeds, palette, loading }: {
  timeSeries: any;
  feeds: Record<string, any> | null;
  palette: Palette;
  loading: boolean;
}) {
  if (loading || !timeSeries || !Array.isArray(timeSeries.provider_status_series)) {
    return (
      <>
        <p id="providerStatusSummary" className="chart-summary" aria-live="polite">Loading provider status…</p>
        <div id="providerStatusRows" className="status-rows" />
      </>
    );
  }
  const feedMap = feeds || {};
  const bucketStarts: unknown[] = Array.isArray(timeSeries.bucket_starts) ? timeSeries.bucket_starts : [];
  const sourceRows = (timeSeries.provider_status_series as any[]).filter((row) => Number(row.total_events) > 0);
  const { hourStarts: buckets, hourly } = hourlyCells(bucketStarts, bucketMinutesOf(timeSeries), sourceRows);
  const CELLS = 48;
  const offset = Math.max(0, buckets.length - CELLS);
  const ranked = sourceRows
    .map((row, index) => ({
      // Rows may arrive raw or canonical; fill provider_canonical once so
      // the color slot and the feed fallback key on it either way. The
      // per-cell arrays are the hourly fold, not the raw sub-buckets.
      row: {
        ...row,
        ...hourly[index],
        provider_canonical: quotaRowProviderKey(row) || String(row.provider || "unknown"),
      },
      windowEvents: hourly[index].events.slice(offset).reduce((sum: number, n: number) => sum + n, 0),
      windowErrors: hourly[index].errors.slice(offset).reduce((sum: number, n: number) => sum + n, 0),
    }))
    .sort((a, b) => Number(b.row.total_events) - Number(a.row.total_events));
  const shown = ranked.filter((entry) => entry.row.provider !== "unknown" && entry.windowEvents >= 10);
  const folded = ranked.filter((entry) => entry.row.provider === "unknown" || entry.windowEvents < 10);
  const foldedNames = folded
    .map((entry) => `${providerDisplayName(entry.row.provider_canonical || entry.row.provider)} (${intFormat(entry.windowEvents)} calls, ${intFormat(entry.windowErrors)} errors)`)
    .join(", ");
  // The summary reads the same 48-cell window the strip draws — never the
  // seven-day totals the series also carries.
  const inWindow = ranked.filter((entry) => entry.windowEvents > 0);
  const windowCalls = inWindow.reduce((sum, entry) => sum + entry.windowEvents, 0);
  const windowErrors = inWindow.reduce((sum, entry) => sum + entry.windowErrors, 0);
  const summary = inWindow.length
    ? `${inWindow.length} provider${inWindow.length === 1 ? "" : "s"} · ${intFormat(windowCalls)} calls, ${intFormat(windowErrors)} errored, over the last ${CELLS} complete UTC hours.`
    : `No provider traffic over the last ${CELLS} complete UTC hours.`;

  return (
    <>
      <p id="providerStatusSummary" className="chart-summary" aria-live="polite">{summary}</p>
      <div id="providerStatusRows" className="status-rows">
        {shown.map((entry, index) => {
          const row = entry.row;
          const cells: { key: number; className: string; title: string }[] = [];
          for (let i = offset; i < buckets.length; i += 1) {
            const total = row.events[i] || 0;
            const bad = row.errors[i] || 0;
            let stateClass = "state-idle";
            if (total > 0) {
              stateClass = bad === 0 ? "state-ok" : (bad / total >= 0.5 ? "state-failing" : "state-degraded");
            }
            const hourLabel = formatUtcHour(buckets[i], false);
            cells.push({
              key: i,
              className: `status-cell ${stateClass}`,
              title: total
                ? `${hourLabel} — ${intFormat(total)} calls, ${intFormat(bad)} errors${bad && row.top_error_classes[i] ? ` (${row.top_error_classes[i]})` : ""}`
                : `${hourLabel} — no traffic`,
            });
          }
          // The feed map is keyed by the raw provider id today; try that
          // first, then the canonical id so a canonical-grouped row still
          // finds its badge.
          const feedEntry = feedMap[row.provider] ?? feedMap[row.provider_canonical];
          return (
            <div className="status-row" key={`${row.provider_canonical}-${index}`}>
              <div className="status-row-head">
                <span
                  className="swatch-dot"
                  style={{ background: subscriptionUnifiedProviderColor(palette, { provider: row.provider_canonical }, index) }}
                />
                <span>{providerDisplayName(row.provider_canonical || row.provider)}</span>
                <ProviderStatusBadge feedEntry={feedEntry} />
              </div>
              <div
                className="status-cells"
                role="img"
                aria-label={
                  `${providerDisplayName(row.provider_canonical || row.provider)}: ${intFormat(entry.windowEvents)} calls, ${intFormat(entry.windowErrors)} errors over the last ${CELLS} complete UTC hours` +
                  (entry.windowErrors && row.dominant_error_class ? `; dominant error ${row.dominant_error_class}` : "")
                }
              >
                {cells.map((cell) => (
                  <span key={cell.key} className={cell.className} title={cell.title} />
                ))}
              </div>
            </div>
          );
        })}
        {folded.length > 0 && (
          <div
            className="status-row low-traffic"
            role="note"
            title={foldedNames}
            aria-label={`Low-traffic providers folded from the strip: ${foldedNames}`}
          >
            {`+${folded.length} low-traffic provider${folded.length === 1 ? "" : "s"}`}
          </div>
        )}
      </div>
    </>
  );
}
