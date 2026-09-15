import { useMemo } from "react";
import { intFormat } from "../lib/format";

// Rulings per dollar (keeper ruling 2026-09-10: "we need to make ratification
// a first class thing"). Tokens proxy cost, cost proxies value used, value
// used proxies value produced — and the keeper's unit of PRODUCED value is a
// ratification. This panel is the join: ratifications recorded in the Atrium
// ruling ledger per ISO week, set against the same week's list-price
// inference estimate.
//
// Two things the panel must never imply: that the dollars are billed (they
// are a list-price ESTIMATE at the standard service tier, over traffic that
// mostly bills $0 on subscriptions), and that the count is keeper activity
// (it counts rulings that were RECORDED). Both live in the footnote, and the
// "no ledger yet" state says so outright rather than rendering a confident
// row of zeros.
export interface RulingsPanelState {
  data: any | null;
  message: string | null;
  contentVisible: boolean;
}

interface RulingWeekModel {
  key: string;
  week: string;
  partial: boolean;
  rulings: number;
  classes: string;
  listPrice: string | null;
  perThousand: number | null;
  outputTokens: number | null;
}

function classSummary(byClass: unknown): string {
  if (!byClass || typeof byClass !== "object") return "—";
  const entries = Object.entries(byClass as Record<string, number>)
    .filter(([, count]) => Number(count) > 0)
    .sort((a, b) => b[1] - a[1]);
  if (!entries.length) return "—";
  return entries.map(([name, count]) => `${name} ${count}`).join(" · ");
}

function deriveWeeks(series: any[]): RulingWeekModel[] {
  return series.map((row, index) => ({
    key: `${row.week || index}`,
    week: String(row.week || "—"),
    partial: Boolean(row.partial),
    rulings: Number(row.rulings_total || 0),
    classes: classSummary(row.rulings_by_class),
    listPrice: row.list_price_usd == null ? null : String(row.list_price_usd),
    perThousand: row.rulings_per_1k_usd == null ? null : Number(row.rulings_per_1k_usd),
    outputTokens: row.output_tokens == null ? null : Number(row.output_tokens),
  }));
}

export function RulingsPerDollarCard({ rulings }: { rulings: RulingsPanelState }) {
  const weeks = useMemo(
    () => (Array.isArray(rulings.data?.series) ? deriveWeeks(rulings.data.series) : []),
    [rulings.data],
  );
  const coverage = rulings.data?.coverage || {};
  const ledgersScanned = Number(coverage.ledgers_scanned || 0);
  const parseErrors = Number(coverage.parse_errors || 0);
  const totalRulings = weeks.reduce((sum, week) => sum + week.rulings, 0);

  return (
    <section className="card" aria-labelledby="rulingsTitle">
      <div className="card-header">
        <div className="card-title">
          <h2 id="rulingsTitle">Rulings per dollar</h2>
          <p className="caption">
            Keeper ratifications recorded in the Atrium ruling ledger, by ISO week (America/New_York), against the same week's API list-price equivalent — what that week's inference would have cost at public list prices, not billed spend. The ratio is recorded rulings per $1,000 of list-price equivalent — judgment per dollar, computed rather than mined.
          </p>
        </div>
      </div>
      <div id="rulingsState" className="empty-state" hidden={!rulings.message}>{rulings.message || ""}</div>
      <div id="rulingsContent" hidden={!rulings.contentVisible}>
        {ledgersScanned === 0 ? (
          <p className="caption">
            No ruling ledger in canon yet — the counts below are honest zeros, not a quiet week. Backfill of prior adjudications is scheduled for ~2026-09-18.
          </p>
        ) : null}
        <div className="table-wrap">
          <table className="rulings-table" aria-describedby="rulingsTitle">
            <thead>
              <tr>
                <th scope="col">Week</th>
                <th scope="col">Rulings</th>
                <th scope="col">List-price equivalent</th>
                <th scope="col">Rulings per $1k list</th>
                <th scope="col">Output tokens</th>
                <th scope="col">Classes</th>
              </tr>
            </thead>
            <tbody id="rulingsBody">
              {weeks.map((week) => (
                <tr key={week.key}>
                  <th scope="row">{week.week}{week.partial ? " ·  in progress" : ""}</th>
                  <td>{intFormat(week.rulings)}</td>
                  <td>{week.listPrice == null ? "—" : `$${week.listPrice}`}</td>
                  {/* A dash here means the week has no denominator (no priced
                      traffic) — not a ratio of zero. */}
                  <td>{week.perThousand == null ? "—" : week.perThousand.toFixed(2)}</td>
                  <td>{week.outputTokens == null ? "—" : intFormat(week.outputTokens)}</td>
                  <td>{week.classes}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="caption" id="rulingsFootnote">
          {totalRulings} recorded {totalRulings === 1 ? "ruling" : "rulings"} across {weeks.length} weeks from {ledgersScanned} {ledgersScanned === 1 ? "ledger" : "ledgers"}
          {parseErrors > 0 ? ` · ${parseErrors} ${parseErrors === 1 ? "entry" : "entries"} unparseable (counted, not dropped)` : ""}.
          The denominator is the API list-price equivalent at the standard service tier, not billed cost — subscription traffic bills $0 actual. The count measures rulings that were <em>recorded</em>, not keeper adjudication traffic.
        </p>
      </div>
    </section>
  );
}
