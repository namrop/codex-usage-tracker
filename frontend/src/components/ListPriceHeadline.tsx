// API list-price equivalent (the former estimated-spend tile, renamed under
// the keeper authorization of 2026-09-14): what the accounted traffic would have cost at
// each provider's public API list price at the standard service tier. The
// Accounting view leads with it because every dollar figure on that view
// is either this comparison figure or a posted billing fact, and the two
// must never be confused: subscription lanes bill $0 actual.

// Spend sparkline (polish round item 12): the server's per-UTC-calendar-day
// list-price estimates (oldest first; the first and last day may be partial
// and say so) as a bare structure-role polyline — trend only; the exact
// values ride the title.
function spendDayLabel(day: any): string {
  const date = new Date(day.day_start).toLocaleString([], { timeZone: "UTC", month: "short", day: "numeric" });
  return `${date} $${day.estimate_usd}${day && day.partial === true ? " (partial)" : ""}`;
}

export function SpendSparkline({ days }: { days: any[] }) {
  const values = days.map((day) => Number(day && day.estimate_usd));
  if (days.length < 2 || !values.every((value) => Number.isFinite(value))) {
    return (
      <svg
        id="unifiedSpendSpark"
        className="spend-spark"
        viewBox="0 0 100 24"
        preserveAspectRatio="none"
        role="img"
        aria-label="Daily list-price equivalents over the window"
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
      aria-label={`Daily list-price equivalents (UTC day buckets): ${described}`}
    >
      <polyline points={points} />
      <title>{described}</title>
    </svg>
  );
}

export function ListPriceHeadline({ unifiedData, unifiedMessage, listPriceDay, donutFive }: {
  unifiedData: any | null;
  unifiedMessage: string | null;
  listPriceDay: any | null;
  donutFive: any | null;
}) {
  const data = unifiedData;
  const totals = (data && data.totals) || {};
  const listWeek = totals.list_price || {};
  const listPriceDaily: any[] = data && Array.isArray(data.list_price_daily) ? data.list_price_daily : [];
  const hintParts: string[] = [];
  if (listPriceDay) hintParts.push(`24h $${listPriceDay.estimate_usd}`);
  const five = donutFive && donutFive.totals ? donutFive.totals.list_price : null;
  if (five) hintParts.push(`5h $${five.estimate_usd}`);
  const share = listWeek.priced_token_share;
  if (share !== undefined && share < 0.999) {
    hintParts.push(`${Math.round(share * 100)}% of tokens priced`);
  }
  const unpriced: string[] = Array.isArray(listWeek.unpriced_providers) ? listWeek.unpriced_providers : [];
  if (unpriced.length) hintParts.push(`no list price for ${unpriced.join(", ")}`);

  return (
    <section className="card" aria-labelledby="listPriceTitle">
      <div className="card-header">
        <div className="card-title">
          <h2 id="listPriceTitle">API list-price equivalent · 7 days</h2>
          <p className="caption">
            What the accounted traffic would have cost at each provider’s public API list price at the standard service tier — a comparison figure, not a bill: subscription lanes bill $0 actual, and posted charges are the billing ledger below.
          </p>
        </div>
      </div>
      <div id="listPriceState" className="empty-state" hidden={Boolean(data) || !unifiedMessage}>{unifiedMessage || ""}</div>
      <div className="panel-grid" hidden={!data}>
        <div className="metric">
          <div className="label">API list-price equivalent · week</div>
          <div className="value" id="unifiedPricedSpend">{data ? `$${listWeek.estimate_usd || "0"}` : "—"}</div>
          <SpendSparkline days={listPriceDaily} />
          <div className="hint" id="unifiedPricedSpendHint">
            {data ? [...hintParts, "list-price equivalent, not billed cost"].join(" · ") : "—"}
          </div>
        </div>
        <div className="metric">
          <div className="label">Priced tokens</div>
          <div className="value" id="listPricePricedShare">
            {data && share !== undefined ? `${Math.round(Number(share) * 100)}%` : "—"}
          </div>
          <div className="hint">share of accounted tokens with a known list price</div>
        </div>
      </div>
    </section>
  );
}
