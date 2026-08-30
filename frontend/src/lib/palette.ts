// Governed Cognition palette bridge (kintsugi render): the JS palette
// arrays died here — every color the app hands to Chart.js or canvas
// resolves through the GC token layer at render time, so a theme swap
// reaches the charts. getColor() normalizes any CSS color value (var
// chains, color-mix) to a plain rgb()/rgba() string via a probe element so
// canvas and Chart.js parse it under every theme.
//
// Conformance note (the documented withAlpha exception): this module may
// CONSTRUCT rgba() strings from RESOLVED token values — it may not carry
// color literals, and neither may any other frontend source.

export function getCss(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

let colorProbe: HTMLSpanElement | null = null;

export function getColor(name: string): string {
  if (!colorProbe) {
    colorProbe = document.createElement("span");
    colorProbe.style.display = "none";
    document.documentElement.appendChild(colorProbe);
  }
  colorProbe.style.color = "";
  colorProbe.style.color = getCss(name);
  const resolved = getComputedStyle(colorProbe).color;
  return resolved || getCss(name);
}

export function withAlpha(color: string, alpha: number): string {
  const match = String(color).match(/^rgba?\(([^)]+)\)$/);
  if (!match) return color;
  const [r, g, b] = match[1].split(",").map((part) => part.trim());
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

export interface SeriesPalette {
  muted: string;
  line: string;
  input: string;
  cacheRead: string;
  cacheWrite: string;
  output: string;
  reasoning: string;
  grid: string;
  gridSoft: string;
  tooltipBg: string;
}

export interface Palette {
  series: SeriesPalette;
  // Ranked/unbounded series wear the declared ai-usage.series-rank set
  // (keeper amendment, verbatim in the GC schema: surplus graph-line colors
  // are acceptable but defined in governed cognition). Harness identity
  // wears the declared ai-usage.harness set.
  rankColors: string[];
  modelColors: string[];
  harnessColors: Record<string, string>;
}

export function resolvePalette(): Palette {
  const rankColors = Array.from({ length: 10 }, (_unused, index) => (
    getColor(`--gc-cat-ai-usage-series-rank-rank-${index + 1}`)
  ));
  return {
    series: {
      muted: getColor("--muted"),
      line: getColor("--line-soft"),
      input: getColor("--sky"),
      cacheRead: getColor("--spark-session"),
      cacheWrite: getColor("--copper"),
      output: getColor("--weekly"),
      reasoning: getColor("--spark-weekly"),
      grid: withAlpha(getColor("--line"), 0.10),
      gridSoft: withAlpha(getColor("--line"), 0.08),
      tooltipBg: withAlpha(getColor("--bg"), 0.96),
    },
    rankColors,
    modelColors: rankColors.slice(),
    harnessColors: {
      claude_code: getColor("--gc-cat-ai-usage-harness-claude_code"),
      claude_code_secondary: getColor("--gc-cat-ai-usage-harness-claude_code_secondary"),
      hermes: getColor("--gc-cat-ai-usage-harness-hermes"),
      opencode: getColor("--gc-cat-ai-usage-harness-opencode"),
    },
  };
}

// Charts read colors at render time, so the GC token layer must be resolved
// BEFORE the first render: poll for the theme stylesheet (origin or vendored
// fallback — the loader guarantees one lands), with a deadline so a
// pathological outage still renders data.
export function whenThemeReady(deadlineMs = 4000): Promise<void> {
  const started = Date.now();
  return new Promise((resolve) => {
    (function poll() {
      if (getCss("--gc-bg") || Date.now() - started > deadlineMs) {
        resolve();
        return;
      }
      setTimeout(poll, 50);
    })();
  });
}

export const REDUCED_MOTION = typeof window !== "undefined"
  && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
