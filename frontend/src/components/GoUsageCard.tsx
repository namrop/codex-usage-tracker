import { useMemo, useState } from "react";
import type { GoUsagePanelState } from "../hooks/useDashboardData";
import { compactIntFormat, formatTimestamp, intFormat } from "../lib/format";
import {
  GO_MODELED_BASIS_TEXT,
  GO_USAGE_MODES,
  goCoverageLine,
  goModelKey,
  goModelRows,
  goTotalsLine,
  goUsageModeLabel,
  nativeHeadline,
  reconciliationText,
  type GoModelRow,
  type GoUsageMode,
} from "../lib/goUsage";

// OpenCode Go usage explanation (keeper request 2026-09-14: "what consumes
// the shared allowance"). A small card right after the weekly allowance
// panel on History — not a new page. The headline is the provider's own
// weekly percent (an integer, whole-point resolution); the expandable
// detail lists each model's canonical token volume, its multiplier and a
// current-tariff estimate of its weighted share of the 30-unit pool. Rows
// are never scaled to match the native total, and the estimate is never
// money owed. Everything renders what lib/goUsage.ts derives; bars are
// plain elements in the weekly vocabulary, no chart library.

function ModeSwitch({ mode, onChange }: { mode: GoUsageMode; onChange: (next: GoUsageMode) => void }) {
  return (
    <div className="weekly-mode" role="group" aria-label="Bar basis">
      {GO_USAGE_MODES.map((candidate) => (
        <button
          key={candidate}
          type="button"
          aria-pressed={mode === candidate}
          onClick={() => onChange(candidate)}
        >
          {goUsageModeLabel(candidate)}
        </button>
      ))}
    </div>
  );
}

function ModelBar({ row, mode }: { row: GoModelRow; mode: GoUsageMode }) {
  const tokensMode = mode === "tokens";
  const classes = ["weekly-provider", "go-usage-model"];
  // In quota mode an unpriced model has no bar to draw; in token mode its
  // tokens still count, so the bar stays.
  if (!row.priced && !tokensMode) classes.push("is-empty");
  if (!row.priced) classes.push("is-unpriced");
  const width = row.barPct === null ? 0 : Math.min(Math.max(row.barPct, 0), 100);
  const valueText = tokensMode
    ? (row.barPct === null ? "no tokens" : `${row.barPct.toFixed(1)}% of this week's tokens`)
    : (row.barPct === null ? "unpriced" : `${row.quotaPctText} of the weekly pool, ${GO_MODELED_BASIS_TEXT}`);
  return (
    <li className={classes.join(" ")}>
      <div className="weekly-provider-head">
        <strong>{row.label}</strong>
        <span className="weekly-provider-coverage">{`${row.multiplierText} multiplier · ${row.coverageText}`}</span>
      </div>
      <div
        className="weekly-bar"
        role="progressbar"
        aria-label={`${row.label} ${mode === "quota" ? "modeled share of the weekly pool" : "share of this week's tokens"}`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={row.barPct === null ? undefined : Number(row.barPct.toFixed(1))}
        aria-valuetext={valueText}
      >
        <div className="weekly-bar-fill" style={{ width: `${width}%` }} />
      </div>
      <div className="weekly-bar-labels">
        <span>{mode === "quota" ? `modeled ${row.quotaPctText} · ${row.quotaUnitsText} units` : `${compactIntFormat(row.totalTokens)} tokens`}</span>
        <span>{mode === "quota" ? `list ${row.listCostText}` : `share ${row.barPct === null ? "—" : `${row.barPct.toFixed(1)}%`}`}</span>
      </div>
    </li>
  );
}

export function GoUsageCard({ goUsage }: { goUsage: GoUsagePanelState }) {
  const [mode, setMode] = useState<GoUsageMode>("quota");
  const payload = goUsage.data;
  const headline = useMemo(() => (payload ? nativeHeadline(payload) : null), [payload]);
  const rows = useMemo(() => (payload ? goModelRows(payload, mode) : []), [payload, mode]);
  const tariffModels = payload ? Object.entries(payload.tariff.models || {}) : [];

  return (
    <section className="card weekly-card go-usage-card" id="goUsage" tabIndex={-1} aria-labelledby="goUsageTitle">
      <div className="card-header">
        <div className="card-title">
          <h2 id="goUsageTitle">OpenCode Go: what used the weekly pool</h2>
          <p className="caption">
            The provider’s own weekly percent (provider-reported, whole-point resolution), with a current-tariff estimate of how much of the shared 30-unit pool each model’s tokens consumed this week. Current week only.
          </p>
        </div>
      </div>
      <div id="goUsageState" className="empty-state" hidden={!goUsage.message}>{goUsage.message || ""}</div>
      <div id="goUsageContent" hidden={!goUsage.contentVisible}>
        {headline && payload && (
          <>
            <div className="weekly-headline" aria-live="polite">
              <strong className="weekly-headline-value">{headline.percentText}</strong>
              <span className="weekly-headline-complement">
                {`native weekly use · ${headline.boundaryText} · ${headline.statusText}`}
              </span>
              <span className="weekly-coverage">
                {`${headline.sourceText} · ${headline.freshnessText} · observed ${formatTimestamp(headline.observedAt)} · resets ${formatTimestamp(headline.resetsAt)}`}
              </span>
            </div>
            <p className="go-usage-reconcile">{reconciliationText(payload)}</p>
            <details className="weekly-detail go-usage-detail">
              <summary>{`Model contributions (${rows.length}) — ${GO_MODELED_BASIS_TEXT}`}</summary>
              <ModeSwitch mode={mode} onChange={setMode} />
              <p className="caption">{goTotalsLine(payload)}</p>
              <ul className="weekly-providers" aria-label={`Model contributions by ${goUsageModeLabel(mode).toLowerCase()}`}>
                {rows.map((row) => (
                  <ModelBar key={goModelKey(row)} row={row} mode={mode} />
                ))}
              </ul>
              <div className="table-wrap">
                <table className="weekly-table go-usage-table" aria-describedby="goUsageTitle">
                  <thead>
                    <tr>
                      <th scope="col">Model</th>
                      <th scope="col">Events</th>
                      <th scope="col">Multiplier</th>
                      <th scope="col">Tokens</th>
                      <th scope="col">Reasoning (diagnostic)</th>
                      <th scope="col">List cost (current tariff)</th>
                      <th scope="col">Quota units</th>
                      <th scope="col">Modeled points</th>
                      <th scope="col">Share of modeled</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row) => (
                      <tr key={goModelKey(row)} className={row.priced ? "is-priced" : "is-unpriced"}>
                        <th scope="row">{row.label}</th>
                        <td>{row.coverageText}</td>
                        <td>{row.multiplierText}</td>
                        <td>
                          {intFormat(row.totalTokens)}
                          <div className="go-usage-buckets">
                            {row.buckets.map((bucket) => (
                              <span key={bucket.label}>{`${bucket.label} ${compactIntFormat(bucket.tokens)}`}</span>
                            ))}
                          </div>
                        </td>
                        <td>{intFormat(row.reasoningTokens)}</td>
                        <td>{row.listCostText}</td>
                        <td>{row.quotaUnitsText}</td>
                        <td>{row.quotaPctText}</td>
                        <td>{row.shareText}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <span className="weekly-coverage">{goCoverageLine(payload)}</span>
            </details>
            <details className="weekly-detail">
              <summary>How this is computed</summary>
              <ul className="weekly-notes">
                <li>{`Native: the Go usage API’s weekly percent, whole points, floor-rounded and capped at 100 — a value p below 100 stands for the band [p, p+1); 100 means at least 100%. Missing or stale readings are shown as such, never as 0%.`}</li>
                <li>{`Modeled: this week’s canonical tokens (input, cache read, cache write, output; reasoning is diagnostic only) priced at the tariff verified ${formatTimestamp(payload.tariff.as_of)}, weighted by each model’s multiplier, over the ${payload.tariff.weekly_limit_quota_units}-unit weekly pool. A current-tariff estimate, not a measured charge; the schedule could have changed within the week. Rows are never rescaled to the native total.`}</li>
                {tariffModels.map(([model, rates]) => (
                  <li key={model}>{`Tariff ${model}: input $${rates.input_usd_per_m}/M · output $${rates.output_usd_per_m}/M · cache read $${rates.cache_read_usd_per_m}/M · cache write $${rates.cache_write_usd_per_m}/M · multiplier ${rates.multiplier}× (${payload.tariff.source_url}).`}</li>
                ))}
                <li>{`Coverage: ${goCoverageLine(payload)} · ${payload.coverage.native_candidates} native readings this week · window ${formatTimestamp(payload.window.started_at)} to ${formatTimestamp(payload.window.ends_at)} · generated ${formatTimestamp(payload.generated_at)}.`}</li>
                {(payload.notes || []).map((note, index) => (
                  <li key={index}>{note}</li>
                ))}
              </ul>
            </details>
          </>
        )}
      </div>
    </section>
  );
}
