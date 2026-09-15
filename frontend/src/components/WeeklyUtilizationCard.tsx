import { useMemo, useState } from "react";
import type { WeeklyPanelState } from "../hooks/useDashboardData";
import { formatTimestamp } from "../lib/format";
import { WHOLE_POINT_LABEL, providerStateText } from "../lib/nativePercent";
import {
  NO_COMPLETED_NATIVE_TEXT,
  WEEKLY_MODES,
  compactWeeklySummary,
  currentPctText,
  cycleReasonText,
  humanizeToken,
  isWholePointCurrent,
  pctText,
  sampleLeadText,
  sortWeeklyWindows,
  summaryHeadline,
  weeklyCoverageLine,
  weeklyModeLabel,
  weeklyProviderRows,
  weeklyCycleKey,
  weeklyWindowKey,
  type WeeklyMode,
  type WeeklyProviderRow,
  type WeeklyWindow,
} from "../lib/weeklyUtilization";

// Weekly allowance use (keeper request 2026-09-14): how much of each weekly
// window is being used, and how much is left on the table — aggregate and
// per provider. Two surfaces share one loader slot in the dashboard hook:
// a compact strip on Now (two lines, a link) and the full panel that leads
// History. Everything here renders what lib/weeklyUtilization.ts derives
// from the backend payload; no chart library, bars are plain elements
// labelled directly.

function ModeSwitch({ mode, onChange }: { mode: WeeklyMode; onChange: (next: WeeklyMode) => void }) {
  return (
    <div className="weekly-mode" role="group" aria-label="Window set">
      {WEEKLY_MODES.map((candidate) => (
        <button
          key={candidate}
          type="button"
          aria-pressed={mode === candidate}
          onClick={() => onChange(candidate)}
        >
          {weeklyModeLabel(candidate)}
        </button>
      ))}
    </div>
  );
}

function ProviderBar({ row }: { row: WeeklyProviderRow }) {
  const classes = ["weekly-provider"];
  if (row.estimated) classes.push("is-estimated");
  if (row.empty) classes.push("is-empty");
  const used = row.usedPct === null ? 0 : Math.min(Math.max(row.usedPct, 0), 100);
  return (
    <li className={classes.join(" ")}>
      <div className="weekly-provider-head">
        <strong>{row.label}</strong>
        <span className="weekly-provider-coverage">{row.coverageText}</span>
      </div>
      <div
        className="weekly-bar"
        role="progressbar"
        aria-label={`${row.label} average weekly allowance used`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={row.usedPct === null ? undefined : Number(row.usedPct.toFixed(1))}
        aria-valuetext={row.usedPct === null ? "no contributing account" : `${row.usedText} used, ${row.complementText} ${row.complementLabel}`}
      >
        <div className="weekly-bar-fill" style={{ width: `${used}%` }} />
      </div>
      <div className="weekly-bar-labels">
        <span>{row.empty ? "no value" : `used ${row.usedText}`}</span>
        <span>{row.empty ? "" : `${row.complementLabel} ${row.complementText}`}</span>
      </div>
    </li>
  );
}

function WindowLabel({ window }: { window: WeeklyWindow }) {
  return (
    <>
      {window.label}
      {window.role === "model_cap" && <span className="weekly-tag">not included in aggregate</span>}
    </>
  );
}

function WindowRows({ window, mode }: { window: WeeklyWindow; mode: WeeklyMode }) {
  const rowClasses = [`role-${window.role === "model_cap" ? "model-cap" : "primary"}`];
  if (mode === "current" && !window.current.eligible) rowClasses.push("is-excluded");
  // A native whole-point reading (OpenCode Go usage API) prints the
  // provider's integer and says so; every other window keeps one decimal.
  const wholePoint = isWholePointCurrent(window.current);
  if (mode === "current") {
    const current = window.current;
    const state = wholePoint ? providerStateText(current) : null;
    return (
      <tr className={rowClasses.join(" ")}>
        <th scope="row"><WindowLabel window={window} /></th>
        <td>{humanizeToken(window.quota_name)}</td>
        <td>{currentPctText(current, "used_pct")}</td>
        <td>{currentPctText(current, "remaining_pct")}</td>
        <td>{`${humanizeToken(current.confidence)}${wholePoint ? ` · ${WHOLE_POINT_LABEL}` : ""}`}</td>
        <td>{`${humanizeToken(current.status)}${current.eligible ? "" : " · excluded"}${state ? ` · provider state ${state}` : ""}`}</td>
        <td>{`observed ${formatTimestamp(current.observed_at)} · resets ${formatTimestamp(current.resets_at)}`}</td>
      </tr>
    );
  }
  const completed = window.completed;
  const cycles = Array.isArray(window.cycles) ? window.cycles : [];
  return (
    <>
      <tr className={rowClasses.join(" ")}>
        <th scope="row"><WindowLabel window={window} /></th>
        <td>{humanizeToken(window.quota_name)}</td>
        <td>{pctText(completed.used_pct)}</td>
        <td>{pctText(completed.remaining_pct)}</td>
        <td>mean of eligible cycles</td>
        <td>{`${completed.cycle_count} eligible · ${completed.excluded_cycle_count} excluded`}</td>
        <td>
          {cycles.length
            ? `${cycles.length} boundary records`
            : wholePoint
              ? `${NO_COMPLETED_NATIVE_TEXT} · waiting for provider-observed completed windows`
              : "no boundary record"}
        </td>
      </tr>
      {cycles.map((cycle, cycleIndex) => (
        <tr key={weeklyCycleKey(window, cycle, cycleIndex)} className={`weekly-cycle${cycle.eligible ? "" : " is-excluded"}`}>
          <th scope="row">{`window end ${formatTimestamp(cycle.resets_at)}`}</th>
          <td>cycle</td>
          <td>{pctText(cycle.used_pct)}</td>
          <td>{pctText(cycle.remaining_pct)}</td>
          <td>{humanizeToken(cycle.confidence)}</td>
          <td>{cycle.eligible ? "eligible" : `excluded · ${(cycle.reasons || []).map((reason) => cycleReasonText(reason)).join(", ") || "unspecified"}`}</td>
          <td>{`last sample ${formatTimestamp(cycle.last_observed_at)} · ${sampleLeadText(cycle.last_sample_seconds_before_reset)}`}</td>
        </tr>
      ))}
    </>
  );
}

export function WeeklyUtilizationCard({ weekly }: { weekly: WeeklyPanelState }) {
  const [mode, setMode] = useState<WeeklyMode>("current");
  const payload = weekly.data;
  const summary = payload ? (mode === "completed" ? payload.completed : payload.current) : null;
  const headline = useMemo(() => (summary ? summaryHeadline(summary, mode) : null), [summary, mode]);
  // The provider rows read the windows' native whole-point metadata (the
  // summaries carry none) so a single native Go account prints its integer.
  const providerRows = useMemo(
    () => (summary && payload ? weeklyProviderRows(summary, mode, payload.windows) : []),
    [summary, mode, payload],
  );
  const windows = useMemo(() => (payload ? sortWeeklyWindows(payload.windows) : []), [payload]);
  const modelCaps = windows.filter((window) => window.role === "model_cap").length;

  return (
    <section className="card weekly-card" id="weeklyUtilization" tabIndex={-1} aria-labelledby="weeklyUtilizationTitle">
      <div className="card-header">
        <div className="card-title">
          <h2 id="weeklyUtilizationTitle">Weekly allowance use</h2>
          <p className="caption">
            Average weekly allowance used across accounts, equal weight per account: each account’s primary weekly window as a percent of its own cap. Current windows show what remains; completed windows show what was unused at reset.
          </p>
        </div>
      </div>
      <div id="weeklyUtilizationState" className="empty-state" hidden={!weekly.message}>{weekly.message || ""}</div>
      <div id="weeklyUtilizationContent" hidden={!weekly.contentVisible}>
        <ModeSwitch mode={mode} onChange={setMode} />
        {headline && summary && payload && (
          <>
            <div className="weekly-headline" aria-live="polite">
              <strong className="weekly-headline-value">{headline.usedText}</strong>
              <span className="weekly-headline-complement">
                {`${headline.usedLabel.toLowerCase()} · ${headline.complementText} ${headline.complementLabel}`}
              </span>
              <span className="weekly-coverage">{weeklyCoverageLine(summary, mode)}</span>
            </div>
            <ul className="weekly-providers" aria-label={`${weeklyModeLabel(mode)} by provider`}>
              {providerRows.map((row) => (
                <ProviderBar key={row.provider} row={row} />
              ))}
            </ul>
            <details className="weekly-detail">
              <summary>
                {mode === "current"
                  ? `Account windows (${windows.length}${modelCaps ? `, ${modelCaps} model cap${modelCaps === 1 ? "" : "s"} shown separately` : ""})`
                  : `Account windows and completed-cycle evidence (${windows.length}${modelCaps ? `, ${modelCaps} model cap${modelCaps === 1 ? "" : "s"} shown separately` : ""})`}
              </summary>
              <div className="table-wrap">
                <table className="weekly-table" aria-describedby="weeklyUtilizationTitle">
                  <thead>
                    <tr>
                      <th scope="col">Account</th>
                      <th scope="col">Window</th>
                      <th scope="col">Used</th>
                      <th scope="col">{mode === "current" ? "Remaining" : "Unused at reset"}</th>
                      <th scope="col">Confidence</th>
                      <th scope="col">Coverage</th>
                      <th scope="col">{mode === "current" ? "Observed / reset" : "Evidence"}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {windows.map((window) => (
                      <WindowRows key={weeklyWindowKey(window)} window={window} mode={mode} />
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="caption">
                Model caps (a Claude Fable seven-day cap) are listed for reference and marked “not included in aggregate”, so a Claude account is never counted twice. Each cycle row is an observed reset-target record (its window end), not proof a reset occurred; a completed cycle is the last sample before that window end — a polling estimate, not the provider’s terminal figure. Excluded cycles stay visible with their reason.
              </p>
            </details>
            <details className="weekly-detail">
              <summary>How this is computed</summary>
              <ul className="weekly-notes">
                <li>{`Weighting: ${humanizeToken(payload.weighting)} — the arithmetic mean of account percentages, never pooled tokens or dollars, and never a mean of provider means.`}</li>
                <li>{`Completed windows: the last ${payload.weeks} inferred provider reset periods per account (lookback ${payload.lookback_days} days), reset targets clustered within five minutes; each account’s cycles average first, then accounts average equally.`}</li>
                <li>Estimated and partial readings count in the aggregate and are labelled; stale, missing and invalid readings are excluded with counts, never entered as zero.</li>
                <li>{`Coverage: ${payload.coverage.source_rows} source rows · ${payload.coverage.invalid_rows} invalid · ${payload.coverage.excluded_cycles} excluded cycles · generated ${formatTimestamp(payload.generated_at)}.`}</li>
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

// The Now strip: current used / remaining and completed used / unused at
// reset, each with a one-phrase coverage note, and the link to the panel.
// Not an alarm — no state colour, no threshold.
export function WeeklyUtilizationSummary({ weekly }: { weekly: WeeklyPanelState }) {
  const compact = useMemo(() => (weekly.data ? compactWeeklySummary(weekly.data) : null), [weekly.data]);
  return (
    <section className="card weekly-summary-card" aria-labelledby="nowWeeklyTitle">
      <div className="card-header">
        <div className="card-title">
          <h2 id="nowWeeklyTitle">Weekly allowance use</h2>
          <p className="caption">
            {"Average weekly allowance used, equal weight per account. "}
            <a href="#weeklyUtilization">Per-provider detail and completed-window evidence</a>
          </p>
        </div>
      </div>
      <div id="nowWeeklyState" className="empty-state" hidden={!weekly.message}>{weekly.message || ""}</div>
      {compact && (
        <dl className="weekly-strip" hidden={!weekly.contentVisible}>
          <div>
            <dt>Current windows</dt>
            <dd><strong>{compact.current.usedText}</strong>{` used · ${compact.current.complementText} ${compact.current.complementLabel}`}</dd>
            <dd className="caption">{compact.current.note}</dd>
          </div>
          <div>
            <dt>Completed windows</dt>
            <dd><strong>{compact.completed.usedText}</strong>{` used · ${compact.completed.complementText} ${compact.completed.complementLabel}`}</dd>
            <dd className="caption">{`${compact.completed.note} · last sample before reset`}</dd>
          </div>
        </dl>
      )}
    </section>
  );
}
