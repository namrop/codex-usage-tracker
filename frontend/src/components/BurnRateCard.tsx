import { useMemo } from "react";
import type { BurnPanelState } from "../hooks/useDashboardData";
import { burnRowClass, deriveBurnRows, projectionText } from "../lib/burn";
import { formatTimestamp, percent } from "../lib/format";

// Burn panel (2026-09-02, keeper-approved from the Claude session-limit burn
// analysis; widened 2026-09-14): every per-account quota window the ledger
// samples — five-hour and weekly model windows, model-specific caps, monthly
// tool quotas and balances — with the observed used-percent trajectory, the
// burn rate at the observed active-use rate, and the projection the backend
// can honestly claim. One row per account window — the backend's identity
// rule keeps accounts separate, so the same provider can appear twice, and
// the account label is the same "Claude 2" the capacity summary uses.

// A tiny inline SVG sparkline — no chart dependency for what is a
// three-to-eight point trajectory.
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
  if (hours >= 48) return `${Math.floor(hours / 24)}d ${hours % 24}h`;
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
            Every per-account quota window the ledger samples — five-hour and weekly model windows, model-specific caps, monthly tool quotas and account balances — with its used-percent trajectory, its burn rate at the observed active-use rate, and a projection that never extends past the window’s own reset; balances carry no forecast and say so.
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
                <th scope="col">Confidence</th>
                <th scope="col">Trend</th>
                <th scope="col">Rate</th>
                <th scope="col">Resets in</th>
                <th scope="col">Projection</th>
              </tr>
            </thead>
            <tbody id="burnBody">
              {rows.map((row) => (
                <tr key={row.key} className={burnRowClass(row.status)} data-projection-status={row.status}>
                  <th scope="row">{`${row.label} · ${row.windowName}`}</th>
                  <td>{row.latestPct == null ? "—" : percent(row.latestPct)}</td>
                  <td>{row.confidenceLabel}</td>
                  <td><Sparkline points={row.spark} /></td>
                  <td>
                    {row.rate == null
                      ? "—"
                      : `${row.rate.toFixed(1)} pts/h`}
                    {row.rate != null && row.rateWindowDeltas > 0 ? <span className="caption"> · {row.rateWindowDeltas}Δ</span> : null}
                  </td>
                  <td>{countdown(row.secondsToReset)}</td>
                  <td>
                    {projectionText(row.status, row.reason, row.exhaustionAt ? formatTimestamp(row.exhaustionAt) : null)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="caption" id="burnFootnote">
          Rate is measured while the window is moving (idle spans do not dilute it). “Resets first” means the window is expected to reset before it empties at the observed active-use rate; “no rise measured” means no positive rate exists yet; “stale” means the latest sample is too old, or the window reset after it, to assert anything about now; balances and lifetime windows are never forecast. Confidence is the provider’s own: an estimated window is never shown as exact.
        </p>
      </div>
    </section>
  );
}
