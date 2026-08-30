// Chart.js rides the vendored UMD build (/static/vendor/chart.umd.min.js),
// loaded as a classic script from the entry HTML exactly as before the React
// conversion — never bundled, so the app chunk stays free of third-party
// color literals and the vendored asset keeps its own cache policy.
import { REDUCED_MOTION, type Palette } from "./palette";
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
    tension: 0.24,
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
    // `${color}A8` appends an alpha byte to the resolved token value — the
    // documented withAlpha-family exception; no literal color lives here.
    backgroundColor: `${color}A8`,
    borderColor: color,
    borderWidth: 1,
    borderRadius: 4,
    ...extra,
  };
}

export function unifiedTokenChartOptions(
  palette: Palette,
  { stacked = false }: { stacked?: boolean } = {},
): Record<string, unknown> {
  const SERIES = palette.series;
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
        ticks: {
          color: SERIES.muted,
          autoSkip: true,
          maxRotation: 0,
          minRotation: 0,
          align: "inner",
          maxTicksLimit: window.innerWidth <= 640 ? 4 : 7,
        },
        grid: { display: false },
      },
    },
    plugins: {
      legend: { display: false },
      tooltip: {
        displayColors: false,
        backgroundColor: SERIES.tooltipBg,
        borderColor: SERIES.line,
        borderWidth: 1,
        padding: 10,
        callbacks: {
          label: (context: { dataset: { label: string }; parsed: { y: unknown } }) =>
            `${context.dataset.label}: ${intFormat(context.parsed.y)} tokens`,
        },
      },
    },
  };
}

export function cacheHitRateChartOptions(palette: Palette): Record<string, unknown> {
  const SERIES = palette.series;
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
        ticks: {
          color: SERIES.muted,
          autoSkip: true,
          maxRotation: 0,
          minRotation: 0,
          align: "inner",
          maxTicksLimit: window.innerWidth <= 640 ? 4 : 7,
        },
        grid: { display: false },
      },
    },
    plugins: {
      legend: { display: false },
      tooltip: {
        displayColors: false,
        backgroundColor: SERIES.tooltipBg,
        borderColor: SERIES.line,
        borderWidth: 1,
        padding: 10,
        callbacks: {
          label: (context: { dataset: { label: string }; parsed: { y: unknown } }) =>
            `${context.dataset.label}: ${percent(context.parsed.y)}`,
        },
      },
    },
  };
}

export function subscriptionQuotaChartOptions(palette: Palette): Record<string, unknown> {
  const SERIES = palette.series;
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
        ticks: {
          color: SERIES.muted,
          autoSkip: true,
          maxRotation: 0,
          minRotation: 0,
          maxTicksLimit: window.matchMedia("(max-width: 520px)").matches ? 3 : 9,
        },
        grid: { display: false },
      },
    },
    plugins: {
      legend: { display: false },
      tooltip: {
        displayColors: true,
        backgroundColor: SERIES.tooltipBg,
        borderColor: SERIES.line,
        borderWidth: 1,
        padding: 10,
        callbacks: {
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
