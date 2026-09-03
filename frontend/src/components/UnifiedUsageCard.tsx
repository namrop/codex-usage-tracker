import { useMemo, useState } from "react";
import type { Palette } from "../lib/palette";
import {
  barDataset,
  cacheHitRateChartOptions,
  lineDataset,
  unifiedTokenChartOptions,
  type ChartConfig,
} from "../lib/chart";
import {
  bucketMinutesOf,
  bucketWording,
  compactIntFormat,
  formatApiModes,
  formatBucketLabel,
  formatTimestamp,
  intFormat,
  percent,
  type BucketWording,
} from "../lib/format";
import { useViewportWidth } from "../hooks/useViewportWidth";
import { cededLanes, type Freshness } from "../lib/freshness";
import { subscriptionUnifiedProviderColor } from "../lib/providers";
import { QUOTA_FIVE_HOUR_NAMES, QUOTA_WEEKLY_NAMES, type QuotaRow } from "../lib/quota";
import { ChartCanvas } from "./ChartCanvas";
import { DirectLabels, type DirectLabelRow } from "./DirectLabels";
import { Donut } from "./Donut";
import { Fold, tableFoldSummary } from "./Fold";
import { ProviderStatus } from "./ProviderStatus";
import { CountdownText, ResetRail, type ResetChipModel } from "./ResetRail";
import { SeriesTable, type SeriesTableRow } from "./SeriesTable";

// Estate-wide token composition (keeper-ruled 2026-08-30, replacing the
// codex-only Token bucket mix): the five canonical token buckets summed
// across every provider, stacked per complete UTC bucket (15-minute,
// keeper-ruled 2026-09-02). Bucket colors are the established SERIES
// entries; reasoning stays its own series because this chart shows
// composition, not the accounted total.
const TOKEN_COMPOSITION_BUCKETS: readonly [string, string, keyof Palette["series"]][] = Object.freeze([
  ["input_tokens", "Input", "input"],
  ["cache_read_tokens", "Cache read", "cacheRead"],
  ["cache_write_tokens", "Cache write", "cacheWrite"],
  ["output_tokens", "Output", "output"],
  ["reasoning_tokens", "Reasoning", "reasoning"],
]) as never;

interface SeriesChartModel {
  config: ChartConfig;
  labelRows: DirectLabelRow[];
  tableRows: SeriesTableRow[];
  bucketStarts: unknown[];
  summary: string;
}

// The per-chart axis inputs every model shares: compact tick labels on the
// axis, full labels as tooltip titles, wording from the bucket width, and
// the live viewport width for the tick limit.
interface ChartFrame {
  bucketMinutes: number;
  wording: BucketWording;
  labels: string[];
  tooltipTitles: string[];
  viewportWidth: number;
}

function chartFrame(timeSeries: any, viewportWidth: number): ChartFrame {
  const bucketMinutes = bucketMinutesOf(timeSeries);
  const starts: unknown[] = Array.isArray(timeSeries.bucket_starts) ? timeSeries.bucket_starts : [];
  return {
    bucketMinutes,
    wording: bucketWording(bucketMinutes),
    labels: starts.map((value) => formatBucketLabel(value, bucketMinutes, true)),
    tooltipTitles: starts.map((value) => formatBucketLabel(value, bucketMinutes, false)),
    viewportWidth,
  };
}

function modelTokenModel(timeSeries: any, palette: Palette, frame: ChartFrame): SeriesChartModel | null {
  if (!timeSeries || !Array.isArray(timeSeries.bucket_starts)) return null;
  const { labels, tooltipTitles, viewportWidth, wording } = frame;
  const models: any[] = Array.isArray(timeSeries.model_series) ? [...timeSeries.model_series] : [];
  if (timeSeries.other_model_series) models.push(timeSeries.other_model_series);
  // modelColors is already the hue-spread walk of the rank set (palette),
  // so the two largest series never share a near-identical neighbour hue.
  const modelColors = models.map((_row, index) => palette.modelColors[index % palette.modelColors.length]);
  const datasets = models.map((row, index) => barDataset(
    row.label,
    row.values,
    modelColors[index],
    { stack: "models", borderRadius: 1 },
  ));
  const modelTotal = models.reduce((sum, row) => sum + Number(row.total_tokens || 0), 0);
  return {
    config: {
      type: "bar",
      data: { labels, datasets },
      options: unifiedTokenChartOptions(palette, { stacked: true, viewportWidth, tooltipTitles }),
    },
    labelRows: models.map((row, index) => ({
      label: String(row.label),
      detail: compactIntFormat(row.total_tokens),
      color: modelColors[index],
    })),
    tableRows: models.map((row) => ({ label: String(row.label), values: row.values })),
    bucketStarts: timeSeries.bucket_starts,
    summary: `${compactIntFormat(modelTotal)} accounted tokens across ${models.length} displayed series in ${wording.completePlural}.`,
  };
}

function tokenCompositionModel(timeSeries: any, palette: Palette, frame: ChartFrame): SeriesChartModel | null {
  if (!timeSeries || !Array.isArray(timeSeries.bucket_starts)) return null;
  const bucketSeries = timeSeries.bucket_series;
  if (!bucketSeries || typeof bucketSeries !== "object") return null;
  const { labels, tooltipTitles, viewportWidth, wording } = frame;
  const rows = TOKEN_COMPOSITION_BUCKETS.map(([field, label]) => {
    const values: unknown[] = Array.isArray(bucketSeries[field])
      ? bucketSeries[field]
      : timeSeries.bucket_starts.map(() => 0);
    return {
      label,
      values,
      total_tokens: values.reduce((sum: number, value) => sum + Number(value || 0), 0),
    };
  });
  const colors = TOKEN_COMPOSITION_BUCKETS.map(([, , seriesKey]) => palette.series[seriesKey]);
  const datasets = rows.map((row, index) => barDataset(
    row.label,
    row.values,
    colors[index],
    { stack: "composition", borderRadius: 1 },
  ));
  const compositionTotal = rows.reduce((sum, row) => sum + row.total_tokens, 0);
  return {
    config: {
      type: "bar",
      data: { labels, datasets },
      options: unifiedTokenChartOptions(palette, { stacked: true, viewportWidth, tooltipTitles }),
    },
    labelRows: rows.map((row, index) => ({
      label: row.label,
      detail: compactIntFormat(row.total_tokens),
      color: colors[index],
    })),
    tableRows: rows.map((row) => ({ label: row.label, values: row.values })),
    bucketStarts: timeSeries.bucket_starts,
    summary: `${compactIntFormat(compositionTotal)} tokens across five buckets and every provider in ${wording.completePlural}.`,
  };
}

// Estate-wide cache hit rate (keeper-ruled 2026-08-30, replacing the
// codex-only chart): per provider per bucket, cache reads over prompt
// tokens (input + cache read). A bucket with no prompt tokens is null — the
// line gaps instead of lying at zero.
function cacheHitRateModel(timeSeries: any, palette: Palette, frame: ChartFrame): SeriesChartModel | null {
  if (!timeSeries || !Array.isArray(timeSeries.bucket_starts) || !Array.isArray(timeSeries.provider_status_series)) {
    return null;
  }
  const { labels, tooltipTitles, viewportWidth, wording } = frame;
  const all = (timeSeries.provider_status_series as any[]).filter((row) => (
    Array.isArray(row.input_tokens) && Array.isArray(row.cache_read_tokens)
  ));
  // The same low-traffic folding rule the status strip applies: "unknown"
  // and providers under 10 events in the window fold out of the chart and
  // into the summary line.
  const shown = all.filter((row) => row.provider !== "unknown" && Number(row.total_events) >= 10);
  const folded = all.filter((row) => row.provider === "unknown" || Number(row.total_events) < 10);
  const rows = shown.map((row, index) => {
    let promptTotal = 0;
    let cacheTotal = 0;
    const values = timeSeries.bucket_starts.map((_unused: unknown, bucket: number) => {
      const cache = Number(row.cache_read_tokens[bucket] || 0);
      const prompt = Number(row.input_tokens[bucket] || 0) + cache;
      promptTotal += prompt;
      cacheTotal += cache;
      return prompt > 0 ? (cache / prompt) * 100 : null;
    });
    return {
      provider: String(row.provider),
      label: String(row.provider),
      color: subscriptionUnifiedProviderColor(palette, { provider: row.provider_canonical }, index),
      windowAverage: promptTotal > 0 ? (cacheTotal / promptTotal) * 100 : null,
      values,
    };
  });
  const datasets = rows.map((row) => lineDataset(row.label, row.values, row.color, { spanGaps: false }));
  const summaryParts = [
    rows.length
      ? `${rows.length} provider${rows.length === 1 ? "" : "s"} charted over ${wording.completePlural}; gaps mean no prompt tokens ${wording.that}.`
      : "No provider met the traffic bar for the cache chart in this window.",
  ];
  if (folded.length) {
    const foldedNames = folded
      .map((row) => `${row.provider} (${intFormat(row.total_events)} events)`)
      .join(", ");
    summaryParts.push(`Low-traffic fold: ${foldedNames}.`);
  }
  return {
    config: {
      type: "line",
      data: { labels, datasets },
      options: cacheHitRateChartOptions(palette, { viewportWidth, tooltipTitles }),
    },
    labelRows: rows.map((row) => ({
      label: row.provider,
      detail: row.windowAverage === null ? "no prompt tokens" : `${percent(row.windowAverage)} avg`,
      color: row.color,
    })),
    tableRows: rows.map((row) => ({ label: row.label, values: row.values })),
    bucketStarts: timeSeries.bucket_starts,
    summary: summaryParts.join(" "),
  };
}

// Spend sparkline (polish round item 12): the server's per-UTC-calendar-day
// list-price estimates (oldest first; the first and last day may be partial
// and say so) as a bare structure-role polyline — trend only; the exact
// values ride the title.
function spendDayLabel(day: any): string {
  const date = new Date(day.day_start).toLocaleString([], { timeZone: "UTC", month: "short", day: "numeric" });
  return `${date} $${day.estimate_usd}${day && day.partial === true ? " (partial)" : ""}`;
}

function SpendSparkline({ days }: { days: any[] }) {
  const values = days.map((day) => Number(day && day.estimate_usd));
  if (days.length < 2 || !values.every((value) => Number.isFinite(value))) {
    return (
      <svg
        id="unifiedSpendSpark"
        className="spend-spark"
        viewBox="0 0 100 24"
        preserveAspectRatio="none"
        role="img"
        aria-label="Daily list-price estimates over the window"
        // SVGElement has no .hidden property — the attribute itself is what
        // the style layer keys on in the SVG namespace.
        {...({ hidden: "hidden" } as Record<string, string>)}
      />
    );
  }
  const width = 100;
  const height = 24;
  const pad = 2;
  const max = Math.max(...values);
  const points = values
    .map((value, index) => {
      const x = (index / (values.length - 1)) * width;
      const y = max > 0 ? height - pad - (value / max) * (height - pad * 2) : height - pad;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const described = days.map(spendDayLabel).join(" · ");
  return (
    <svg
      id="unifiedSpendSpark"
      className="spend-spark"
      viewBox="0 0 100 24"
      preserveAspectRatio="none"
      role="img"
      aria-label={`Daily list-price estimates (UTC day buckets): ${described}`}
    >
      <polyline points={points} />
      <title>{described}</title>
    </svg>
  );
}

function ChartSection({ sectionId, titleId, title, caption, canvasId, canvasAriaLabel, labelsId, labelsAriaLabel, summaryId, model, fallbackSummary, loadingSummary, foldKey, foldLabel, tableCaption, tableHeadId, tableBodyId, formatCell, loaded, wording }: {
  sectionId?: string;
  titleId: string;
  title: string;
  caption: string;
  canvasId: string;
  canvasAriaLabel: string;
  labelsId: string;
  labelsAriaLabel: string;
  summaryId: string;
  model: SeriesChartModel | null;
  fallbackSummary: string;
  loadingSummary: string;
  foldKey: string;
  foldLabel: string;
  tableCaption: string;
  tableHeadId: string;
  tableBodyId: string;
  formatCell?: (value: unknown) => string;
  loaded: boolean;
  wording: BucketWording;
}) {
  const [chartOk, setChartOk] = useState(true);
  const summary = !loaded
    ? loadingSummary
    : model && chartOk
      ? model.summary
      : fallbackSummary;
  const foldSummary = tableFoldSummary({
    rows: model ? model.bucketStarts.length : 0,
    singular: wording.singular,
    columns: model ? model.tableRows.length : 0,
    colNoun: "series",
  });
  return (
    <section className="usage-chart" aria-labelledby={titleId} id={sectionId}>
      <h3 id={titleId}>{title}</h3>
      <p className="caption">{caption}</p>
      <p id={summaryId} className="chart-summary" aria-live="polite">{summary}</p>
      <div className="usage-chart-layout">
        <div className="chart-wrap unified">
          <ChartCanvas
            id={canvasId}
            ariaLabel={canvasAriaLabel}
            config={model ? model.config : null}
            onRendered={setChartOk}
          />
        </div>
        <DirectLabels id={labelsId} ariaLabel={labelsAriaLabel} rows={model ? model.labelRows : []} />
      </div>
      <Fold foldKey={foldKey} label={foldLabel} summary={foldSummary} headingLevel={4}>
        <SeriesTable
          headId={tableHeadId}
          bodyId={tableBodyId}
          caption={tableCaption}
          bucketStarts={model ? model.bucketStarts : []}
          bucketMinutes={wording.minutes}
          rows={model ? model.tableRows : []}
          formatCell={formatCell}
        />
      </Fold>
    </section>
  );
}

export function UnifiedUsageCard({ palette, unified, freshness, donutFive, listPriceDay, quotaLatest, resetChips, subsLoading, subsUnavailable = false, providerStatusFeeds, refreshing, onRefresh }: {
  palette: Palette;
  unified: { data: any | null; message: string | null; contentVisible: boolean };
  freshness: Freshness | null;
  donutFive: any | null;
  listPriceDay: any | null;
  quotaLatest: QuotaRow[] | undefined;
  resetChips: ResetChipModel[];
  subsLoading: boolean;
  subsUnavailable?: boolean;
  providerStatusFeeds: Record<string, any> | null;
  refreshing: boolean;
  onRefresh: () => void;
}) {
  const data = unified.data;
  const timeSeries = data ? data.time_series : null;
  const viewportWidth = useViewportWidth();
  const frame = useMemo(() => (timeSeries ? chartFrame(timeSeries, viewportWidth) : null), [timeSeries, viewportWidth]);
  const modelToken = useMemo(() => (timeSeries && frame ? modelTokenModel(timeSeries, palette, frame) : null), [timeSeries, palette, frame]);
  const composition = useMemo(() => (timeSeries && frame ? tokenCompositionModel(timeSeries, palette, frame) : null), [timeSeries, palette, frame]);
  const cacheHitRate = useMemo(() => (timeSeries && frame ? cacheHitRateModel(timeSeries, palette, frame) : null), [timeSeries, palette, frame]);
  // Wording follows the loaded series' bucket width; before data lands the
  // captions describe the 15-minute buckets the backend cuts.
  const wording = frame ? frame.wording : bucketWording(15);

  // Ceded-lane rendering (the one behavior addition of the kintsugi render,
  // keeper-ratified): a lane the collector gave up on this run is a
  // violation-role state — one honest line naming the lane and its error
  // class, not a dashboard section.
  const ceded = freshness ? cededLanes(freshness) : [];
  const cededText = ceded.length
    ? `Lane${ceded.length > 1 ? "s" : ""} ceded this run: ${ceded.map((lane) => (
      lane.last_error_class ? `${lane.lane} (${lane.last_error_class})` : String(lane.lane)
    )).join(", ")} — collector retried, then skipped so the rest of the run could land.`
    : "";

  const totals = (data && data.totals) || {};
  const coverage = (data && data.coverage) || {};
  const events = data ? Number((data.window && data.window.event_count) || 0) : 0;
  const substituted = data ? Number(totals.substituted_events || 0) : 0;
  const listWeek = totals.list_price || {};
  const listPriceDaily: any[] = data && Array.isArray(data.list_price_daily) ? data.list_price_daily : [];

  // Est. spend at LIST prices (keeper-ruled 2026-08-30): the subscription
  // lanes report $0 actual, so the tile answers "what would this traffic
  // cost at list." Week is the value; 24h and 5h fill in as their windows
  // arrive; coverage keeps the gaps honest.
  const spendHintParts: string[] = [];
  if (listPriceDay) spendHintParts.push(`24h $${listPriceDay.estimate_usd}`);
  const five = donutFive && donutFive.totals ? donutFive.totals.list_price : null;
  if (five) spendHintParts.push(`5h $${five.estimate_usd}`);
  const share = listWeek.priced_token_share;
  if (share !== undefined && share < 0.999) {
    spendHintParts.push(`${Math.round(share * 100)}% of tokens priced`);
  }
  spendHintParts.push("list-price estimate");

  const soonest = resetChips[0];

  const harnessRows: any[] = (data && data.by_harness) || [];
  const providerModelRows: any[] = (data && data.by_provider_model) || [];
  const harnessSummary = tableFoldSummary({
    rows: harnessRows.length,
    singular: "harness",
    plural: "harnesses",
    metrics: [
      { total: harnessRows.length ? harnessRows.reduce((sum, row) => sum + Number(row.events || 0), 0) : null, label: "events" },
      { total: harnessRows.length ? harnessRows.reduce((sum, row) => sum + Number(row.total_tokens || 0), 0) : null, label: "tokens" },
    ],
  });
  const providerModelSummary = tableFoldSummary({
    rows: providerModelRows.length,
    singular: "provider/model row",
    plural: "provider/model rows",
    metrics: [
      { total: providerModelRows.length ? providerModelRows.reduce((sum, row) => sum + Number(row.events || 0), 0) : null, label: "events" },
      { total: providerModelRows.length ? providerModelRows.reduce((sum, row) => sum + Number(row.total_tokens || 0), 0) : null, label: "tokens" },
    ],
  });

  return (
    <section className="card" aria-labelledby="unifiedUsageTitle">
      <div className="card-header">
        <div className="card-title">
          <h2 id="unifiedUsageTitle">Unified usage · 7 days</h2>
          <p className="caption">Canonical token totals and request estimates; reasoning is diagnostic and never counted twice.</p>
          <p className="caption">{`Bucketed surfaces use ${wording.completePlural} (countdowns run in wall-clock time), and legend quota percentages are each provider’s own cap — never shares of one pool, never drawn as slices.`}</p>
        </div>
        <div className="toolbar" aria-label="Refresh controls">
          <button className="refresh-btn" id="refreshUsage" disabled={refreshing} onClick={onRefresh}>
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>
      <div id="unifiedUsageState" className="empty-state" hidden={!unified.message}>{unified.message || ""}</div>
      <p id="unifiedCededNotice" className="lane-notice" hidden={!cededText} aria-live="polite">{cededText}</p>
      <div id="unifiedUsageContent" hidden={!unified.contentVisible}>
        <div className="panel-grid">
          <div className="metric">
            <div className="label">Events</div>
            <div className="value" id="unifiedEvents">{data ? intFormat(events) : "—"}</div>
            <div className="hint" id="unifiedEventsHint">
              {data
                // Coverage demoted to caption text (keeper-ratified retirement #4).
                ? `${intFormat(coverage.exact_events)} exact · ${intFormat(coverage.reconstructed_events)} reconstructed`
                : "—"}
            </div>
          </div>
          <div className="metric">
            <div className="label">Total tokens</div>
            <div className="value color-token" id="unifiedTotalTokens">{data ? intFormat(totals.total_tokens) : "—"}</div>
            <div className="hint" id="unifiedWindow">
              {data ? `${formatTimestamp(data.window.first_occurred_at)} → ${formatTimestamp(data.window.last_occurred_at)}` : "—"}
            </div>
          </div>
          <div className="metric">
            <div className="label">Est. spend (list)</div>
            <div className="value" id="unifiedPricedSpend">{data ? `$${listWeek.estimate_usd || "0"} wk` : "—"}</div>
            <SpendSparkline days={listPriceDaily} />
            <div className="hint" id="unifiedPricedSpendHint">{data ? spendHintParts.join(" · ") : "—"}</div>
          </div>
          <div className="metric">
            <div className="label">Soonest reset</div>
            <div className="value mono" id="unifiedSoonestReset" data-reset-at={soonest ? String(soonest.resetAt) : undefined} data-weekly={soonest ? (soonest.weekly ? "1" : "") : undefined}>
              {soonest ? <CountdownText resetAt={soonest.resetAt} weekly={soonest.weekly} /> : "—"}
            </div>
            <div className="hint" id="unifiedSoonestResetHint">
              {soonest ? `${soonest.label} ${soonest.windowName} · ${soonest.usedPct.toFixed(0)}% used` : "no exact resets reported"}
            </div>
          </div>
          <div className={`metric${substituted > 0 ? " role-violation" : ""}`} id="unifiedSubstitutionTile">
            <div className="label">Substitution watch</div>
            {/* Substitution watch: NULL model_reported never counts; zero is
                the healthy reading and any nonzero wears the violation role. */}
            <div className="value" id="unifiedSubstitutions">{data ? intFormat(substituted) : "—"}</div>
            <div className="hint" id="unifiedSubstitutionsHint">
              {data
                ? (substituted ? "served model differed from requested" : "no substitutions observed · 7 days")
                : "—"}
            </div>
          </div>
        </div>

        <div className="window-pair">
          <section className="window-card" aria-labelledby="fiveHourDonutTitle">
            <h3 id="fiveHourDonutTitle">Five-hour throughput by provider</h3>
            <p className="caption">Accounted tokens over the last five hours to the most recent complete quarter hour, split by provider.</p>
            <Donut
              svgId="fiveHourDonut"
              legendId="fiveHourDonutLegend"
              summaryId="fiveHourDonutSummary"
              legendAriaLabel="Five-hour provider share and quota state"
              payload={donutFive}
              quotaLatest={quotaLatest}
              windowNames={QUOTA_FIVE_HOUR_NAMES}
              windowNoun="five-hour"
              palette={palette}
              loadingText="Loading five-hour throughput…"
            />
          </section>
          <section className="window-card" aria-labelledby="resetRailTitle">
            <h3 id="resetRailTitle">Reset countdowns</h3>
            <p className="caption">Every provider quota window that reports an exact reset time, soonest first.</p>
            <ResetRail chips={resetChips} loading={subsLoading} unavailable={subsUnavailable} />
          </section>
        </div>

        <div className="window-pair">
          <section className="window-card" aria-labelledby="weeklyDonutTitle">
            <h3 id="weeklyDonutTitle">Seven-day throughput by provider</h3>
            <p className="caption">The same provider split across the seven-day window.</p>
            <Donut
              svgId="weeklyDonut"
              legendId="weeklyDonutLegend"
              summaryId="weeklyDonutSummary"
              legendAriaLabel="Seven-day provider share and quota state"
              payload={data}
              quotaLatest={quotaLatest}
              windowNames={QUOTA_WEEKLY_NAMES}
              windowNoun="week"
              palette={palette}
              loadingText="Loading seven-day throughput…"
            />
          </section>
          <section className="window-card" aria-labelledby="providerStatusTitle">
            <h3 id="providerStatusTitle">Provider API status · 48 hours</h3>
            <p className="caption">Provider status-feed badges over observed per-UTC-hour call health, where a dim cell means no traffic rather than a clean hour.</p>
            <ProviderStatus
              timeSeries={timeSeries}
              feeds={providerStatusFeeds}
              palette={palette}
              loading={!data}
            />
            <ul className="status-key" aria-hidden="true">
              <li><span className="status-cell state-ok" />all ok</li>
              <li><span className="status-cell state-degraded" />some errors</li>
              <li><span className="status-cell state-failing" />mostly errors</li>
              <li><span className="status-cell state-idle" />no traffic</li>
            </ul>
          </section>
        </div>

        <ChartSection
          titleId="modelTokenTitle"
          title="Tokens across provider models"
          caption={`${wording.adjective} accounted tokens by model, with the five largest series visible and the rest rolled into Other.`}
          canvasId="modelTokenChart"
          canvasAriaLabel={`Stacked ${wording.adjective.toLowerCase()} token usage across provider and model pairs`}
          labelsId="modelTokenLabels"
          labelsAriaLabel="Model series totals"
          summaryId="modelTokenSummary"
          model={modelToken}
          fallbackSummary="Model chart unavailable; aggregate tables remain current."
          loadingSummary="Loading model token series…"
          foldKey="modelTokenSeries"
          foldLabel={`${wording.adjective} model data`}
          tableCaption={`Accounted tokens ${wording.per} by provider and model`}
          tableHeadId="modelTokenTableHead"
          tableBodyId="modelTokenTableBody"
          loaded={Boolean(data)}
          wording={wording}
        />
        <ChartSection
          titleId="tokenCompositionTitle"
          title="Token composition"
          caption={`${wording.adjective} input, cache read, cache write, output, and reasoning tokens summed across every provider.`}
          canvasId="tokenCompositionChart"
          canvasAriaLabel={`Stacked ${wording.adjective.toLowerCase()} token composition across all providers`}
          labelsId="tokenCompositionLabels"
          labelsAriaLabel="Token bucket totals"
          summaryId="tokenCompositionSummary"
          model={composition}
          fallbackSummary="Token composition chart unavailable; aggregate tables remain current."
          loadingSummary="Loading token composition…"
          foldKey="tokenCompositionSeries"
          foldLabel={`${wording.adjective} composition data`}
          tableCaption={`Token buckets ${wording.per} across all providers`}
          tableHeadId="tokenCompositionTableHead"
          tableBodyId="tokenCompositionTableBody"
          loaded={Boolean(data)}
          wording={wording}
        />
        <ChartSection
          titleId="cacheHitRateTitle"
          title="Cache hit rate"
          caption={`Cache-read share of each provider’s prompt tokens ${wording.per.replace("per complete ", "per ")}, with gaps where a provider sent no prompt tokens.`}
          canvasId="cacheHitRateChart"
          canvasAriaLabel="Cache hit rate per provider from 0 to 100 percent"
          labelsId="cacheHitRateLabels"
          labelsAriaLabel="Provider window-average cache hit rates"
          summaryId="cacheHitRateSummary"
          model={cacheHitRate}
          fallbackSummary="Cache hit rate chart unavailable; the provider status strip remains current."
          loadingSummary="Loading cache hit rate…"
          foldKey="cacheHitRateSeries"
          foldLabel={`${wording.adjective} cache data`}
          tableCaption={`Cache hit rate ${wording.per} by provider`}
          tableHeadId="cacheHitRateTableHead"
          tableBodyId="cacheHitRateTableBody"
          formatCell={(value) => (value === null || value === undefined ? "—" : percent(value))}
          loaded={Boolean(data)}
          wording={wording}
        />

        <Fold foldKey="harnessUsage" label="By harness" summary={harnessSummary} colsToggle>
          <div className="table-wrap">
            <table className="tier-harness">
              <thead>
                <tr><th>Harness</th><th>Events</th><th>Input</th><th>Cache read</th><th>Cache write</th><th>Output</th><th>Reasoning</th><th>Total</th><th>Estimated USD</th><th>Actual USD</th></tr>
              </thead>
              <tbody id="harnessUsageBody">
                {harnessRows.map((row, index) => (
                  <TextRow
                    key={`${row.harness}-${index}`}
                    values={[
                      row.harness, row.events, row.input_tokens, row.cache_read_tokens, row.cache_write_tokens,
                      row.output_tokens, row.reasoning_tokens, row.total_tokens, row.estimated_cost_usd, row.actual_cost_usd,
                    ]}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </Fold>
        <Fold foldKey="providerModelUsage" label="By provider and model" summary={providerModelSummary} colsToggle>
          <div className="table-wrap">
            <table className="tier-provider-model">
              <thead>
                <tr><th>Provider</th><th>Model</th><th>API</th><th>Events</th><th>Input</th><th>Cache read</th><th>Cache write</th><th>Output</th><th>Reasoning</th><th>Total</th><th>Estimated USD</th><th>Actual USD</th></tr>
              </thead>
              <tbody id="providerModelUsageBody">
                {providerModelRows.map((row, index) => (
                  <TextRow
                    key={`${row.provider}-${row.model}-${index}`}
                    values={[
                      row.provider, row.model, formatApiModes(row.api_modes), row.events, row.input_tokens,
                      row.cache_read_tokens, row.cache_write_tokens, row.output_tokens, row.reasoning_tokens,
                      row.total_tokens, row.estimated_cost_usd, row.actual_cost_usd,
                    ]}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </Fold>
      </div>
    </section>
  );
}

export function TextRow({ values }: { values: unknown[] }) {
  return (
    <tr>
      {values.map((value, index) => (
        <td key={index}>
          {value === null || value === undefined || value === "" ? "—" : String(value)}
        </td>
      ))}
    </tr>
  );
}
