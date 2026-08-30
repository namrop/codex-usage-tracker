import type { Palette } from "../lib/palette";
import { compactIntFormat } from "../lib/format";
import { subscriptionUnifiedProviderColor } from "../lib/providers";
import { quotaPercentFor, type QuotaRow } from "../lib/quota";

// Window throughput donut (keeper-ratified flow rework, akasan 2026-08-30).
// One SOLID arc per provider (keeper-ruled 2026-08-30: the same-hue
// fresh/cached pair read as a confusing two-tone). Cache share lives in the
// legend text; the ring answers only "who ate the window." Quota lives in
// the LEGEND, never as a slice: per-provider caps are not shares of one
// pool (ruled interpretation, akasan 2026-08-30).

function donutArcPath(cx: number, cy: number, r: number, startFrac: number, endFrac: number): string {
  const tau = Math.PI * 2;
  const a0 = startFrac * tau - Math.PI / 2;
  const a1 = endFrac * tau - Math.PI / 2;
  const large = (endFrac - startFrac) > 0.5 ? 1 : 0;
  return `M ${cx + r * Math.cos(a0)} ${cy + r * Math.sin(a0)} A ${r} ${r} 0 ${large} 1 ${cx + r * Math.cos(a1)} ${cy + r * Math.sin(a1)}`;
}

export function Donut({ svgId, legendId, summaryId, legendAriaLabel, payload, quotaLatest, windowNames, windowNoun, palette, loadingText }: {
  svgId: string;
  legendId: string;
  summaryId: string;
  legendAriaLabel: string;
  payload: any | null;
  quotaLatest: QuotaRow[] | undefined;
  windowNames: ReadonlySet<string>;
  windowNoun: string;
  palette: Palette;
  loadingText: string;
}) {
  if (!payload) {
    return (
      <>
        <p id={summaryId} className="chart-summary" aria-live="polite">{loadingText}</p>
        <div className="donut-layout">
          <svg id={svgId} className="donut" viewBox="0 0 200 200" role="img" aria-describedby={summaryId} />
          <ol id={legendId} className="donut-legend" aria-label={legendAriaLabel} />
        </div>
      </>
    );
  }

  const providers = ((payload.by_provider || []) as any[]).filter((row) => Number(row.total_tokens) > 0);
  providers.sort((a, b) => Number(b.total_tokens) - Number(a.total_tokens));
  const windowTotal = providers.reduce((sum, row) => sum + Number(row.total_tokens), 0);

  if (!windowTotal) {
    return (
      <>
        <p id={summaryId} className="chart-summary" aria-live="polite">
          {`No accounted tokens in the ${windowNoun} window.`}
        </p>
        <div className="donut-layout">
          <svg id={svgId} className="donut" viewBox="0 0 200 200" role="img" aria-describedby={summaryId} />
          <ol id={legendId} className="donut-legend" aria-label={legendAriaLabel} />
        </div>
      </>
    );
  }

  let cursor = 0;
  const arcs: { d: string; color: string; minor: boolean; key: string }[] = [];
  const legendRows: { key: string; color: string; provider: string; detail: string }[] = [];
  const minorRows: any[] = [];
  providers.forEach((row, index) => {
    const color = subscriptionUnifiedProviderColor(palette, { provider: row.provider_canonical }, index);
    const span = Number(row.total_tokens) / windowTotal;
    // Noise fold (polish round item 4a): under 0.1% of window tokens a
    // provider keeps its arc as a hairline but folds out of the legend into
    // one combined line below.
    const minor = span < 0.001;
    if (span > 0) {
      const gap = providers.length > 1 ? Math.min(0.004, span / 4) : 0;
      arcs.push({
        d: donutArcPath(100, 100, 74, cursor + gap, Math.min(cursor + span - gap, 0.99999)),
        color,
        minor,
        key: `${row.provider}-${index}`,
      });
      cursor += span;
    }
    if (minor) {
      minorRows.push(row);
      return;
    }
    const share = ((Number(row.total_tokens) / windowTotal) * 100).toFixed(1);
    const cachedShare = row.total_tokens
      ? ((Number(row.cached_tokens || 0) / Number(row.total_tokens)) * 100).toFixed(0)
      : "0";
    const quota = quotaPercentFor(quotaLatest, row.provider_canonical, windowNames);
    const detail = quota
      ? ` ${share}% of window tokens · ${cachedShare}% cached · ${quota.pct.toFixed(0)}% of ${windowNoun} used${quota.accounts > 1 ? " (worst account)" : ""}`
      : ` ${share}% of window tokens · ${cachedShare}% cached`;
    legendRows.push({ key: `${row.provider}-${index}`, color, provider: String(row.provider), detail });
  });

  const combined = minorRows.reduce((sum, row) => sum + Number(row.total_tokens), 0);
  const combinedPct = ((combined / windowTotal) * 100).toFixed(2);
  const minorNames = minorRows
    .map((row) => `${row.provider} ${((Number(row.total_tokens) / windowTotal) * 100).toFixed(2)}%`)
    .join(", ");

  return (
    <>
      <p id={summaryId} className="chart-summary" aria-live="polite">
        {`${compactIntFormat(windowTotal)} accounted tokens across ${providers.length} provider${providers.length === 1 ? "" : "s"}.`}
      </p>
      <div className="donut-layout">
        <svg id={svgId} className="donut" viewBox="0 0 200 200" role="img" aria-describedby={summaryId}>
          {arcs.map((arc) => (
            <path key={arc.key} d={arc.d} fill="none" stroke={arc.color} strokeWidth={arc.minor ? "10" : "30"} />
          ))}
          <text x="100" y="96" textAnchor="middle" className="donut-centre">{compactIntFormat(windowTotal)}</text>
          <text x="100" y="114" textAnchor="middle" className="donut-centre-caption">tokens</text>
        </svg>
        <ol id={legendId} className="donut-legend" aria-label={legendAriaLabel}>
          {legendRows.map((row) => (
            <li key={row.key}>
              <span className="swatch-dot" style={{ background: row.color }} />
              <div>
                <strong>{row.provider}</strong>
                <span>{row.detail}</span>
              </div>
            </li>
          ))}
          {minorRows.length > 0 && (
            <li
              className="legend-fold"
              title={minorNames}
              // The fold dims visually; aria still names every folded provider.
              aria-label={`${minorRows.length} provider${minorRows.length === 1 ? "" : "s"} under 0.1% of window tokens: ${minorNames}`}
            >
              {`+${minorRows.length} more · ${combinedPct}% together`}
            </li>
          )}
        </ol>
      </div>
    </>
  );
}
