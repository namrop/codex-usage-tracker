import { useMemo } from "react";
import type { SubscriptionFeesPanelState, UnifiedPanelState } from "../hooks/useDashboardData";
import { formatTimestamp } from "../lib/format";
import { deriveSubscriptionValue, money } from "../lib/subscriptionValue";
import { TextRow } from "./UnifiedUsageCard";

// Subscriptions: what you pay vs list price (keeper msg 1553037883747598450,
// 2026-09-25). The fee is the latest posted plan charge from the receipts
// manifest; the list-price side is this week's traffic at API list rates.
export function SubscriptionValueCard({ fees, unified }: { fees: SubscriptionFeesPanelState; unified: UnifiedPanelState }) {
  const rows = useMemo(() => deriveSubscriptionValue(fees.data, unified.data), [fees.data, unified.data]);
  const other: any[] = fees.data && Array.isArray(fees.data.other_charges) ? fees.data.other_charges : [];
  const totalFeeWeek = rows.reduce((sum, row) => sum + row.feePerWeek, 0);
  // The multiple compares like with like: fees of subscriptions with a
  // list-price figure, against that traffic at list.
  const trackedFeeWeek = rows.reduce((sum, row) => sum + (row.listPriceNote === null ? row.feePerWeek : 0), 0);
  const totalListWeek = rows.reduce((sum, row) => sum + (row.listPriceWeek || 0), 0);
  return (
    <section className="card" aria-labelledby="subscriptionValueTitle">
      <div className="card-header">
        <div className="card-title">
          <h2 id="subscriptionValueTitle">Subscriptions · what you pay vs list price</h2>
          <p className="caption">
            Each subscription’s fee from its latest receipt, next to what the last seven days of its traffic would have cost at API list prices. Where no charge is in your mail, the fee is the rate you gave. List price is a comparison figure, not a bill.
          </p>
        </div>
      </div>
      <div id="subscriptionValueState" className="empty-state" hidden={!fees.message}>{fees.message || ""}</div>
      <div id="subscriptionValueContent" hidden={!fees.contentVisible}>
        <p className="chart-summary">
          {`${money(totalFeeWeek)} a week in fees · ${money(totalListWeek)} of traffic at list this week`}
          {trackedFeeWeek > 0 ? ` · ${(totalListWeek / trackedFeeWeek).toFixed(1)}×` : ""}
          {trackedFeeWeek < totalFeeWeek ? ` on the subscriptions with a list price` : ""}
        </p>
        <div className="table-wrap">
          <table className="tier-subscription-value">
            <thead>
              <tr><th>Subscription</th><th>Plan</th><th>Fee / month</th><th>Fee / week</th><th>List price · 7 days</th><th>Value</th><th>Last charge</th><th>Paid through</th></tr>
            </thead>
            <tbody id="subscriptionValueBody">
              {rows.map((row) => (
                <TextRow
                  key={row.key}
                  values={[
                    row.label,
                    row.plan || "—",
                    money(row.feePerMonth, row.currency),
                    money(row.feePerWeek, row.currency),
                    row.listPriceNote === null ? money(row.listPriceWeek) : row.listPriceNote,
                    row.multiple === null ? "—" : `${row.multiple.toFixed(1)}×`,
                    row.feeSource === "stated" ? "none in mail · rate you gave" : formatTimestamp(row.chargedAt),
                    row.periodEnd
                      ? `${formatTimestamp(row.periodEnd)}${row.periodCurrent === false ? " · no later charge recorded" : ""}`
                      : "—",
                  ]}
                />
              ))}
            </tbody>
          </table>
        </div>
        {other.length > 0 && (
          <>
            <h3 className="subsection-title">Other charges · 31 days</h3>
            <p className="caption">Extra usage, prepaid credit, prorations and refunds. Not part of the monthly fee.</p>
            <div className="table-wrap">
              <table className="tier-subscription-other">
                <thead>
                  <tr><th>Subscription</th><th>Kind</th><th>What</th><th>Charged</th><th>Amount</th></tr>
                </thead>
                <tbody>
                  {other.map((row, index) => (
                    <TextRow
                      key={`${row.subscription}-${row.occurred_at}-${index}`}
                      values={[row.label, row.kind, row.plan || "—", formatTimestamp(row.occurred_at), money(Number(row.amount), row.currency)]}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </section>
  );
}
