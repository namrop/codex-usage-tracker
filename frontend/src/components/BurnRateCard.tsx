import { useMemo } from "react";
import { formatTimestamp, percent } from "../lib/format";

// Burn panel (2026-09-02, keeper-approved from the Claude session-limit burn
// analysis): per-account five-hour windows with the observed used-percent
// trajectory, the derived burn rate, and an exhaustion projection that the
// backend only emits when it lands before the window's own reset. One row
// per account window — the backend's identity rule keeps accounts separate,
// so the same provider can appear twice.
export interface BurnPanelState {
  data: any | null;
  message: string | null;
  contentVisible: boolean;
}

interface BurnRowModel {
  key: string;
  label: string;
  windowName: string;
  latestPct: number | null;
  rate: number | null;
  rateWindowDeltas: number;
  resetsAt: string | null;
  secondsToReset: number | null;
  exhaustionAt: string | null;
  exhausted: boolean;
  spark: { pct: number; at: string }[];
}

function windowDisplayName(quotaName: string): string {
  return quotaName.replace(/_/g, " ");
}

function deriveBurnRows(series: any[]): BurnRowModel[] {
  return series.map((row, index) => {
    const observations: any[] = Array.isArray(row.observations) ? row.observations : [];
    return {
      key: `${row.provider}-${row.quota_name}-${index}`,
      label: String(row.label || row.provider || "unknown"),
      windowName: windowDisplayName(String(row.quota_name || "window")),
      latestPct: row.latest_used_pct == null ? null : Number(row.latest_used_pct),
      rate: row.burn_rate_pct_per_hour == null ? null : Number(row.burn_rate_pct_per_hour),
      rateWindowDeltas: Number(row.rate_window_deltas || 0),
      resetsAt: row.resets_at ? String(row.resets_at) : null,
      secondsToReset: row.seconds_to_reset == null ? null : Number(row.seconds_to_reset),
      exhaustionAt: row.projected_exhaustion_at ? String(row.projected_exhaustion_at) : null,
      exhausted: Boolean(row.exhausted),
      spark: observations
        .filter((observation) => observation && observation.used_pct != null)
        .map((observation) => ({
          pct: Number(observation.used_pct),
          at: String(observation.observed_at || ""),
        })),
    };
  });
}

// A tiny inline SVG sparkline — no chart dependency for what is a
// three-to-eight point trajectory. Dashed leading segment: observations
// before the burn deltas' window.
function Sparkline({ points }: { points: { pct: number; at: string }[] }) {
  const width = 132;
  const height = 30;
  if (points.length < 2) {
    return <svg className="burn-spark" width={width} height={height} aria-hidden="true" />;
  }
  const xs = points.map((_, index) => (index / (points.length - 1)) * (width - 2) + 1);
  const ys = points.map((point) => height - 1 - (Math.min(Math.max(point.pct, 0), 100) / 100) * (height - 2));
  const path = xs.map((x, index) => `${index === 0 ? "M" : "L"}${x.toFixed(1)},${ys[index].toFixed(1)}`).join(" ");
  return (
    <svg className="burn-spark" width={width} height={height} role="img" aria-label="used-percent trajectory">
      <path d={path} fill="none" strokeWidth="1.6" />
    </svg>
  );
}

function countdown(seconds: number | null): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return "—";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return `${hours}h ${rest ? `${rest}m` : ""}`.trim();
}

export function BurnRateCard({ burn }: { burn: BurnPanelState }) {
  const rows = useMemo(
    () => (Array.isArray(burn.data?.series) ? deriveBurnRows(burn.data.series) : []),
    [burn.data],
  );

  return (
    <section className="card" aria-labelledby="burnTitle">
      <div className="card-header">
        <div className="card-title">
          <h2 id="burnTitle">Window burn rate</h2>
          <p className="caption">
            Per-account five-hour windows: used-percent trajectory, burn rate while the window is moving, and an exhaustion projection that never extends past the window's own reset. Five-minute samples land while a window is actively burning.
          </p>
        </div>
      </div>
      <div id="burnState" className="empty-state" hidden={!burn.message}>{burn.message || ""}</div>
      <div id="burnContent" hidden={!burn.contentVisible}>
        <div className="table-wrap">
          <table className="burn-table" aria-describedby="burnTitle">
            <thead>
              <tr>
                <th scope="col">Account window</th>
                <th scope="col">Used</th>
                <th scope="col">Trend</th>
                <th scope="col">Rate</th>
                <th scope="col">Resets in</th>
                <th scope="col">Projected empty</th>
              </tr>
            </thead>
            <tbody id="burnBody">
              {rows.map((row) => (
                <tr key={row.key} className={row.exhausted ? "burn-exhausted" : row.exhaustionAt ? "burn-projected" : undefined}>
                  <th scope="row">{`${row.label} · ${row.windowName}`}</th>
                  <td>{row.latestPct == null ? "—" : percent(row.latestPct)}</td>
                  <td><Sparkline points={row.spark} /></td>
                  <td>
                    {row.rate == null
                      ? "—"
                      : `${row.rate.toFixed(1)} pts/h`}
                    {row.rate != null && row.rateWindowDeltas > 0 ? <span className="caption"> · {row.rateWindowDeltas}Δ</span> : null}
                  </td>
                  <td>{countdown(row.secondsToReset)}</td>
                  <td>{row.exhausted ? "exhausted" : row.exhaustionAt ? formatTimestamp(row.exhaustionAt) : "unknown"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="caption" id="burnFootnote">
          Rate is measured while the window is moving (idle spans do not dilute it). “Unknown” means no honest projection exists: the window will likely reset before burning out, or there is no positive measured rate.
        </p>
      </div>
    </section>
  );
}
