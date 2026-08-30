import type { BillingPanelState } from "../hooks/useDashboardData";
import { formatTimestamp } from "../lib/format";
import { Fold, tableFoldSummary } from "./Fold";
import { TextRow } from "./UnifiedUsageCard";

// Billing ledger · 30 days: posted and pending billing facts — a record
// class explicitly separate from the request estimates above.
export function BillingCard({ billing }: { billing: BillingPanelState }) {
  const data = billing.data;
  const rows: any[] = data && Array.isArray(data.transactions) ? data.transactions : [];
  const totals: any[] = (data && data.totals_by_currency) || [];

  return (
    <section className="card" aria-labelledby="billingTitle">
      <div className="card-header">
        <div className="card-title">
          <h2 id="billingTitle">Billing ledger · 30 days</h2>
          <p className="caption">Posted and pending billing facts — a record class explicitly separate from the request estimates above.</p>
        </div>
      </div>
      <div id="billingState" className="empty-state" hidden={!billing.message}>{billing.message || ""}</div>
      <div id="billingContent" hidden={!billing.contentVisible}>
        <div id="billingTotals" className="series-key">
          {totals.map((total, index) => (
            <span className="key-pill" key={`${total.currency}-${index}`}>{`${total.currency}: ${total.amount}`}</span>
          ))}
        </div>
        <Fold
          foldKey="billingLedger"
          label="Billing facts"
          summary={tableFoldSummary({ rows: rows.length, singular: "transaction" })}
          colsToggle
        >
          <div className="table-wrap">
            <table className="tier-billing">
              <thead>
                <tr><th>Provider</th><th>Occurred</th><th>Kind</th><th>Status</th><th>Amount</th><th>Currency</th><th>Period start</th><th>Period end</th><th>Description</th></tr>
              </thead>
              <tbody id="billingBody">
                {rows.map((row, index) => (
                  <TextRow
                    key={`${row.provider}-${row.occurred_at}-${index}`}
                    values={[
                      row.provider, formatTimestamp(row.occurred_at), row.transaction_kind, row.status,
                      row.amount, row.currency, formatTimestamp(row.billing_period_start),
                      formatTimestamp(row.billing_period_end), row.description_code,
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
