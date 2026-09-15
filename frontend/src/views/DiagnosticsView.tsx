import { useMemo } from "react";
import type { DashboardState } from "../hooks/useDashboardData";
import { Fold, tableFoldSummary } from "../components/Fold";
import { SubscriptionRawLedgerFold } from "../components/SubscriptionsCard";
import { TextRow } from "../components/UnifiedUsageCard";
import { deriveBurnRows } from "../lib/burn";
import { formatTimestamp, intFormat } from "../lib/format";
import type { Freshness } from "../lib/freshness";

// Diagnostics: is the collection healthy, and what evidence do the other
// views rest on? Everything here is data the page already fetches — the
// two ledgers' freshness and lane health, unified coverage, the per-provider
// observation counts behind the quota charts, the burn sampler's raw
// observations and rises, and the raw quota observation ledger. No new
// endpoint, no promised content.

function LaneTable({ id, title, freshness, loading }: {
  id: string;
  title: string;
  freshness: Freshness | null;
  loading: boolean;
}) {
  const lanes = freshness && Array.isArray(freshness.lanes) ? freshness.lanes : [];
  const status = freshness ? String(freshness.status || "unknown") : null;
  const heartbeat = freshness ? String(freshness.heartbeat_status || status) : null;
  const maxAge = freshness ? Number(freshness.max_age_seconds) : NaN;
  const staleLanes = freshness && Array.isArray(freshness.stale_lanes) ? freshness.stale_lanes.map(String) : [];
  const neverLanes = freshness && Array.isArray(freshness.never_lanes) ? (freshness.never_lanes as unknown[]).map(String) : [];
  return (
    <section className="diagnostics-block" aria-labelledby={`${id}Title`}>
      <h3 id={`${id}Title`}>{title}</h3>
      <p className="chart-summary">
        {loading && !freshness
          ? "Loading freshness…"
          : freshness
            ? `Surface ${status} · writes ${heartbeat} · last write ${formatTimestamp(freshness.latest_at)}${Number.isFinite(maxAge) ? ` · stale after ${Math.round(maxAge / 60)} min` : ""}${staleLanes.length ? ` · stale lanes: ${staleLanes.join(", ")}` : ""}${neverLanes.length ? ` · never succeeded: ${neverLanes.join(", ")}` : ""}`
            : "Freshness unavailable."}
      </p>
      {lanes.length > 0 && (
        <div className="table-wrap">
          <table className="diagnostics-table">
            <caption>{`${title}: per-lane collector health`}</caption>
            <thead>
              <tr><th scope="col">Lane</th><th scope="col">Status</th><th scope="col">Last success</th><th scope="col">Last run</th><th scope="col">Last error</th><th scope="col">Ceded</th></tr>
            </thead>
            <tbody>
              {lanes.map((lane, index) => (
                <tr key={`${String(lane.lane)}-${index}`} className={lane.status === "stale" || lane.ceded === true ? "role-violation" : undefined}>
                  <th scope="row">{String(lane.lane || "—")}</th>
                  <td>{String(lane.status || "—")}</td>
                  <td>{formatTimestamp(lane.last_success_at)}</td>
                  <td>{formatTimestamp(lane.last_run_at)}</td>
                  <td>{lane.last_error_class ? String(lane.last_error_class) : "—"}</td>
                  <td>{lane.ceded === true ? "yes" : "no"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function DiagnosticsView({ state }: { state: DashboardState }) {
  const unified = state.unified.data;
  const coverage = (unified && unified.coverage) || null;
  const listPrice = (unified && unified.totals && unified.totals.list_price) || null;
  const subsData = state.subs.data;
  const subsFreshness: Freshness | null = subsData ? (subsData.freshness || {}) : null;
  const providers: any[] = subsData && subsData.time_series && Array.isArray(subsData.time_series.providers)
    ? subsData.time_series.providers
    : [];
  const truncation = subsData && subsData.time_series ? subsData.time_series.truncation : null;
  const burnRows = useMemo(
    () => (Array.isArray(state.burn.data?.series) ? deriveBurnRows(state.burn.data.series) : []),
    [state.burn.data],
  );
  const burnSeries: any[] = Array.isArray(state.burn.data?.series) ? state.burn.data.series : [];
  const burnObservations = burnSeries.flatMap((series, seriesIndex) => {
    const label = burnRows[seriesIndex] ? `${burnRows[seriesIndex].label} · ${burnRows[seriesIndex].windowName}` : String(series.quota_name || "");
    const observations: any[] = Array.isArray(series.observations) ? series.observations : [];
    return observations.map((observation, index) => ({
      key: `${seriesIndex}-${index}`,
      label,
      observedAt: observation.observed_at,
      usedPct: observation.used_pct,
    }));
  });

  return (
    <>
      <section className="card" aria-labelledby="diagnosticsTitle">
        <div className="card-header">
          <div className="card-title">
            <h2 id="diagnosticsTitle">Collection freshness and coverage</h2>
            <p className="caption">Whether each private ledger is being written on schedule, which collector lanes are quiet, and how much of the accounted traffic is exact and priced — the health behind every other view.</p>
          </div>
        </div>
        <LaneTable id="diagnosticsUsage" title="Usage ledger" freshness={state.freshness} loading={!unified} />
        <LaneTable id="diagnosticsQuota" title="Quota ledger" freshness={subsFreshness} loading={state.subs.loading} />
        <section className="diagnostics-block" aria-labelledby="diagnosticsCoverageTitle">
          <h3 id="diagnosticsCoverageTitle">Unified usage coverage · 7 days</h3>
          <div className="panel-grid">
            <div className="metric">
              <div className="label">Exact events</div>
              <div className="value">{coverage ? intFormat(coverage.exact_events) : "—"}</div>
              <div className="hint">events with a provider-reported token count</div>
            </div>
            <div className="metric">
              <div className="label">Reconstructed events</div>
              <div className="value">{coverage ? intFormat(coverage.reconstructed_events) : "—"}</div>
              <div className="hint">events whose tokens were reconstructed from transcripts</div>
            </div>
            <div className="metric">
              <div className="label">Priced token share</div>
              <div className="value">{listPrice && listPrice.priced_token_share !== undefined ? `${Math.round(Number(listPrice.priced_token_share) * 100)}%` : "—"}</div>
              <div className="hint">
                {listPrice && Array.isArray(listPrice.unpriced_providers) && listPrice.unpriced_providers.length
                  ? `no list price for ${listPrice.unpriced_providers.join(", ")}`
                  : "share of accounted tokens with a known list price"}
              </div>
            </div>
          </div>
          {state.unified.message && !unified ? <p className="empty-state">{state.unified.message}</p> : null}
        </section>
        <section className="diagnostics-block" aria-labelledby="diagnosticsProvidersTitle">
          <h3 id="diagnosticsProvidersTitle">Quota observation coverage by provider · 168 hours</h3>
          <p className="chart-summary">
            {providers.length
              ? `${providers.length} providers in the chart window${truncation && truncation.series_truncated ? ` · ${intFormat(truncation.series_omitted)} series omitted by the payload bound` : ""}.`
              : state.subs.loading ? "Loading provider coverage…" : "No provider coverage available."}
          </p>
          {providers.length > 0 && (
            <div className="table-wrap">
              <table className="diagnostics-table">
                <caption>Observations per provider in the subscriptions chart window</caption>
                <thead>
                  <tr><th scope="col">Provider</th><th scope="col">Raw ids</th><th scope="col">Status</th><th scope="col">Observations</th><th scope="col">Charted series</th><th scope="col">Uncharted series</th></tr>
                </thead>
                <tbody>
                  {providers.map((provider, index) => (
                    <TextRow
                      key={`${provider.provider}-${index}`}
                      values={[
                        provider.label || provider.provider,
                        Array.isArray(provider.raw_provider_ids) ? provider.raw_provider_ids.join(", ") : "",
                        String(provider.status || "").replace(/_/g, " "),
                        intFormat(provider.observation_count),
                        Array.isArray(provider.series) ? provider.series.length : 0,
                        Array.isArray(provider.unavailable_series) ? provider.unavailable_series.length : 0,
                      ]}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
        <section className="diagnostics-block" aria-labelledby="diagnosticsBurnTitle">
          <h3 id="diagnosticsBurnTitle">Burn sampling evidence · 6 hours</h3>
          <p className="chart-summary">
            {state.burn.message && !state.burn.data
              ? state.burn.message
              : `${burnRows.length} sampled windows · ${intFormat(burnObservations.length)} observations · ${intFormat(burnSeries.reduce((sum, series) => sum + (Array.isArray(series.deltas) ? series.deltas.length : 0), 0))} measured rises.`}
          </p>
          {burnRows.length > 0 && (
            <div className="table-wrap">
              <table className="diagnostics-table">
                <caption>Per-window sampling density and the projection claim it supports</caption>
                <thead>
                  <tr><th scope="col">Account window</th><th scope="col">Samples</th><th scope="col">Rises</th><th scope="col">Rate window</th><th scope="col">Latest sample</th><th scope="col">Projection status</th><th scope="col">Reason</th></tr>
                </thead>
                <tbody>
                  {burnRows.map((row, index) => (
                    <TextRow
                      key={row.key}
                      values={[
                        `${row.label} · ${row.windowName}`,
                        row.observationCount,
                        Array.isArray(burnSeries[index]?.deltas) ? burnSeries[index].deltas.length : 0,
                        `${row.rateWindowDeltas}Δ`,
                        formatTimestamp(row.latestObservedAt),
                        row.status.replace(/_/g, " "),
                        row.reason ? row.reason.replace(/_/g, " ") : "",
                      ]}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <Fold
            foldKey="burnObservations"
            label="Raw burn observations"
            summary={tableFoldSummary({ rows: burnObservations.length, singular: "observation" })}
            headingLevel={4}
          >
            <div className="table-wrap">
              <table className="diagnostics-table">
                <caption>Every used-percent sample the burn rows were derived from</caption>
                <thead>
                  <tr><th scope="col">Account window</th><th scope="col">Observed</th><th scope="col">Used %</th></tr>
                </thead>
                <tbody>
                  {burnObservations.map((observation) => (
                    <TextRow
                      key={observation.key}
                      values={[observation.label, formatTimestamp(observation.observedAt), observation.usedPct]}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          </Fold>
        </section>
      </section>
      <SubscriptionRawLedgerFold subs={state.subs} />
    </>
  );
}
