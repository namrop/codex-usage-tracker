import { useMemo } from "react";
import type { SearchesPanelState } from "../hooks/useDashboardData";
import { coverageNote, deriveSearchRows } from "../lib/searches";
import { money } from "../lib/subscriptionValue";
import { TextRow } from "./UnifiedUsageCard";

// Web searches by backend (keeper msg 1553067845766742228, 2026-09-25:
// "brave needs usage tracking too"). Brave bills per request, so this month's
// requests at its receipt rate are the running bill.
export function SearchesCard({ searches }: { searches: SearchesPanelState }) {
  const rows = useMemo(() => deriveSearchRows(searches.data), [searches.data]);
  const note = coverageNote(searches.data);
  const brave = rows.find((row) => row.backend === "brave");
  return (
    <section className="card" aria-labelledby="searchesTitle">
      <div className="card-header">
        <div className="card-title">
          <h2 id="searchesTitle">Web searches · this month</h2>
          <p className="caption">
            Search calls by backend from Hermes and OpenClaw. Brave bills each request it serves, monthly, so its requests at the receipt rate are the running bill.
          </p>
        </div>
      </div>
      <div id="searchesState" className="empty-state" hidden={!searches.message}>{searches.message || ""}</div>
      <div id="searchesContent" hidden={!searches.contentVisible}>
        {brave && brave.estimatedUsd !== null && (
          <p className="chart-summary">
            {`Brave: ${brave.billableCalls.toLocaleString("en-US")} requests this month · about ${money(brave.estimatedUsd)} at $${brave.rateUsd} each`}
          </p>
        )}
        {note && <p className="caption">{note}</p>}
        <div className="table-wrap">
          <table className="tier-searches">
            <thead>
              <tr><th>Backend</th><th>This month</th><th>By harness</th><th>Est. cost</th><th>Last 7 days</th><th>Rate-limited · 7 days</th><th>Errors · 7 days</th></tr>
            </thead>
            <tbody id="searchesBody">
              {rows.map((row) => (
                <TextRow
                  key={row.backend}
                  values={[
                    row.label,
                    row.monthCalls.toLocaleString("en-US"),
                    row.byHarness.map(([name, count]) => `${name} ${count.toLocaleString("en-US")}`).join(" · ") || "—",
                    row.estimatedUsd === null ? "—" : money(row.estimatedUsd),
                    row.weekCalls.toLocaleString("en-US"),
                    row.weekRateLimited ? row.weekRateLimited.toLocaleString("en-US") : "—",
                    row.weekErrors ? row.weekErrors.toLocaleString("en-US") : "—",
                  ]}
                />
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
