import type { Palette } from "../lib/palette";
import { formatUtcHour, intFormat } from "../lib/format";
import { subscriptionUnifiedProviderColor } from "../lib/providers";

// Provider API status strip · 48 hours: provider status-feed badges over
// observed per-UTC-hour call health, where a dim cell means no traffic
// rather than a clean hour. Noise fold (polish round item 4b): providers
// named "unknown" and providers with under 10 events in the shown window
// collapse into one dim line; aria still names each with its call count.

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
  const buckets: unknown[] = Array.isArray(timeSeries.bucket_starts) ? timeSeries.bucket_starts : [];
  const CELLS = 48;
  const offset = Math.max(0, buckets.length - CELLS);
  const ranked = ([...timeSeries.provider_status_series] as any[])
    .filter((row) => Number(row.total_events) > 0)
    .sort((a, b) => Number(b.total_events) - Number(a.total_events))
    .map((row) => ({
      row,
      windowEvents: row.events.slice(offset).reduce((sum: number, n: number) => sum + n, 0),
      windowErrors: row.errors.slice(offset).reduce((sum: number, n: number) => sum + n, 0),
    }));
  const shown = ranked.filter((entry) => entry.row.provider !== "unknown" && entry.windowEvents >= 10);
  const folded = ranked.filter((entry) => entry.row.provider === "unknown" || entry.windowEvents < 10);
  const foldedNames = folded
    .map((entry) => `${entry.row.provider} (${intFormat(entry.windowEvents)} calls, ${intFormat(entry.windowErrors)} errors)`)
    .join(", ");
  const totalErrors = ranked.reduce((sum, entry) => sum + Number(entry.row.total_errors), 0);
  const summary = ranked.length
    ? `${ranked.length} providers · ${intFormat(totalErrors)} errored calls in the seven-day window.`
    : "No provider traffic in the window.";

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
          return (
            <div className="status-row" key={row.provider}>
              <div className="status-row-head">
                <span
                  className="swatch-dot"
                  style={{ background: subscriptionUnifiedProviderColor(palette, { provider: row.provider_canonical }, index) }}
                />
                <span>{row.provider}</span>
                <ProviderStatusBadge feedEntry={feedMap[row.provider]} />
              </div>
              <div
                className="status-cells"
                role="img"
                aria-label={
                  `${row.provider}: ${intFormat(entry.windowEvents)} calls, ${intFormat(entry.windowErrors)} errors over the last ${CELLS} complete UTC hours` +
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
