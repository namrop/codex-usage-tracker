// Chart.js rides the vendored UMD build (/static/vendor/chart.umd.min.js),
// loaded as a classic script from the entry HTML exactly as before the React
// conversion — never bundled, so the app chunk stays free of third-party
// color literals and the vendored asset keeps its own cache policy.
import { REDUCED_MOTION, withAlpha, type Palette } from "./palette";
import { compactIntFormat, intFormat, percent } from "./format";

export interface ChartInstance {
  data: { labels: unknown[]; datasets: unknown[] };
  options: Record<string, unknown>;
  update(): void;
  resize(): void;
  destroy(): void;
}

interface ChartConstructor {
  new (canvas: HTMLCanvasElement, config: Record<string, unknown>): ChartInstance;
  getChart(canvas: HTMLCanvasElement): ChartInstance | undefined;
}

declare global {
  // eslint-disable-next-line no-var
  var Chart: ChartConstructor | undefined;
}

export function chartConstructor(): ChartConstructor | undefined {
  return typeof Chart === "undefined" ? undefined : Chart;
}

export interface ChartConfig {
  type: string;
  data: { labels: unknown[]; datasets: Record<string, unknown>[] };
  options: Record<string, unknown>;
}

// Keeper-ruled 2026-09-02: "I want the charts to be very slightly
// smoothed." Monotone interpolation keeps a smoothed 0–100% quota line
// from overshooting its own samples. Bars stay as they are.
export const LINE_SMOOTHING: Readonly<Record<string, unknown>> = Object.freeze({
  tension: 0.2,
  cubicInterpolationMode: "monotone",
});

export function lineDataset(
  label: string,
  data: unknown[],
  color: string,
  extra: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    label,
    data,
    borderColor: color,
    backgroundColor: color,
    ...LINE_SMOOTHING,
    fill: false,
    spanGaps: true,
    ...extra,
  };
}

export function barDataset(
  label: string,
  data: unknown[],
  color: string,
  extra: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    label,
    data,
    type: "bar",
    // getColor() normalises the token to rgb(); the alpha rides withAlpha
    // (the documented exception) — a hex alpha byte appended to rgb() was
    // an unparseable colour and the fill silently fell back.
    backgroundColor: withAlpha(color, 0.66),
    borderColor: color,
    borderWidth: 1,
    borderRadius: 4,
    ...extra,
  };
}

// Per-chart inputs that are not palette: the viewport width (from the
// useViewportWidth hook — sampling window.innerWidth inside a memo froze
// the breakpoint at first render) and the full-form bucket labels the
// tooltip title shows while the axis ticks wear the compact form.
export interface ChartAxisContext {
  viewportWidth: number;
  tooltipTitles?: readonly string[];
}

// Six ticks under 1100px, eight above: the compact "Aug 27 · 14:15" label
// runs into its neighbour past that on the 1036px live page.
export function xTickLimit(viewportWidth: number): number {
  return viewportWidth < 1100 ? 6 : 8;
}

function xScaleTicks(SERIES: Palette["series"], viewportWidth: number, extra: Record<string, unknown> = {}) {
  return {
    color: SERIES.muted,
    autoSkip: true,
    // Edge labels are inner-aligned, which autoSkip does not measure; the
    // padding keeps the first two ticks from touching at narrow widths.
    autoSkipPadding: 28,
    maxRotation: 0,
    minRotation: 0,
    maxTicksLimit: xTickLimit(viewportWidth),
    ...extra,
  };
}

function tooltipBase(SERIES: Palette["series"], tooltipTitles: readonly string[] | undefined) {
  const callbacks: Record<string, unknown> = {};
  if (tooltipTitles) {
    callbacks.title = (items: { dataIndex: number }[]) => (
      items.length ? tooltipTitles[items[0].dataIndex] ?? "" : ""
    );
  }
  return {
    backgroundColor: SERIES.tooltipBg,
    // Resolved text tokens: the johannah light theme was white-on-cream
    // under Chart.js's default tooltip text colours.
    titleColor: SERIES.text,
    bodyColor: SERIES.muted,
    borderColor: SERIES.line,
    borderWidth: 1,
    padding: 10,
    callbacks,
  };
}

export function unifiedTokenChartOptions(
  palette: Palette,
  { stacked = false, viewportWidth, tooltipTitles }: { stacked?: boolean } & ChartAxisContext,
): Record<string, unknown> {
  const SERIES = palette.series;
  const tooltip = tooltipBase(SERIES, tooltipTitles);
  return {
    maintainAspectRatio: false,
    responsive: true,
    interaction: { mode: "index", intersect: false },
    animation: REDUCED_MOTION ? false : { duration: 260 },
    elements: { line: { borderWidth: 2.4 }, point: { radius: 0, hitRadius: 10, hoverRadius: 3 } },
    scales: {
      y: {
        beginAtZero: true,
        stacked,
        border: { color: SERIES.line },
        ticks: { color: SERIES.muted, maxTicksLimit: 5, callback: (value: unknown) => compactIntFormat(value) },
        grid: { color: SERIES.gridSoft },
      },
      x: {
        stacked,
        border: { color: SERIES.line },
        ticks: xScaleTicks(SERIES, viewportWidth, { align: "inner" }),
        grid: { display: false },
      },
    },
    plugins: {
      legend: { display: false },
      tooltip: {
        displayColors: false,
        ...tooltip,
        callbacks: {
          ...tooltip.callbacks,
          label: (context: { dataset: { label: string }; parsed: { y: unknown } }) =>
            `${context.dataset.label}: ${intFormat(context.parsed.y)} tokens`,
        },
      },
    },
  };
}

export function cacheHitRateChartOptions(
  palette: Palette,
  { viewportWidth, tooltipTitles }: ChartAxisContext,
): Record<string, unknown> {
  const SERIES = palette.series;
  const tooltip = tooltipBase(SERIES, tooltipTitles);
  return {
    maintainAspectRatio: false,
    responsive: true,
    interaction: { mode: "index", intersect: false },
    animation: REDUCED_MOTION ? false : { duration: 260 },
    elements: { line: { borderWidth: 2.4 }, point: { radius: 0, hitRadius: 10, hoverRadius: 3 } },
    scales: {
      y: {
        min: 0,
        max: 100,
        border: { color: SERIES.line },
        ticks: { color: SERIES.muted, stepSize: 25, callback: (value: unknown) => `${value}%` },
        grid: { color: SERIES.gridSoft },
      },
      x: {
        border: { color: SERIES.line },
        ticks: xScaleTicks(SERIES, viewportWidth, { align: "inner" }),
        grid: { display: false },
      },
    },
    plugins: {
      legend: { display: false },
      tooltip: {
        displayColors: false,
        ...tooltip,
        callbacks: {
          ...tooltip.callbacks,
          label: (context: { dataset: { label: string }; parsed: { y: unknown } }) =>
            `${context.dataset.label}: ${percent(context.parsed.y)}`,
        },
      },
    },
  };
}

export function subscriptionQuotaChartOptions(
  palette: Palette,
  { viewportWidth, tooltipTitles }: ChartAxisContext,
): Record<string, unknown> {
  const SERIES = palette.series;
  const tooltip = tooltipBase(SERIES, tooltipTitles);
  return {
    maintainAspectRatio: false,
    responsive: true,
    interaction: { mode: "index", intersect: false },
    animation: REDUCED_MOTION ? false : { duration: 260 },
    scales: {
      y: {
        min: 0,
        max: 100,
        border: { color: SERIES.line },
        ticks: { color: SERIES.muted, stepSize: 25, callback: (value: unknown) => `${value}%` },
        grid: { color: SERIES.grid },
        title: { display: true, text: "quota used", color: SERIES.muted },
      },
      x: {
        border: { color: SERIES.line },
        ticks: xScaleTicks(SERIES, viewportWidth),
        grid: { display: false },
      },
    },
    plugins: {
      legend: { display: false },
      tooltip: {
        displayColors: true,
        ...tooltip,
        callbacks: {
          ...tooltip.callbacks,
          label: (context: { dataset: { label: string }; parsed: { y: unknown } }) =>
            `${context.dataset.label}: ${percent(context.parsed.y)}`,
        },
      },
    },
  };
}

export function replaceChartData(
  chart: ChartInstance,
  labels: unknown[],
  datasets: Record<string, unknown>[],
  options: Record<string, unknown>,
): void {
  chart.data.labels = labels;
  chart.data.datasets = datasets;
  chart.options = options;
  chart.update();
}
