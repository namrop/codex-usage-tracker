// What each subscription costs next to what its traffic would cost at API list
// prices (keeper, 2026-09-25, Discord unified usage ledger thread msg
// 1553037883747598450: "we should record what I pay each month per
// subscription"). The fee is the latest posted plan charge (from the receipts
// manifest via /api/subscription-fees); the list-price side is the unified
// usage window's own per-row estimate. Both are over the same seven days, so
// nothing is extrapolated. List price is a comparison figure, never a bill.
// A "stated" fee is the rate Luis gave for a subscription whose charges are not
// in his mail (msg 1553054862856556656, ChatGPT Pro); it has no charge date.

export interface SubscriptionValueRow {
  key: string;
  label: string;
  plan: string | null;
  feePerMonth: number;
  feePerWeek: number;
  currency: string;
  feeSource: "receipt" | "stated";
  statedAt: string | null;
  chargedAt: string | null;
  periodEnd: string | null;
  periodCurrent: boolean | null;
  // False for subscriptions whose traffic the usage ledger does not record
  // (Pocket, ElevenLabs; keeper msg 1553058626350874726): fee only.
  usageTracked: boolean;
  listPriceWeek: number | null;
  // listPriceWeek / feePerWeek; null without a list-price figure.
  multiple: number | null;
}

function num(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

// List-price equivalent per subscription over the unified window: Claude by
// account label ("Claude 1"), everything else by canonical provider.
export function listPriceBySubscription(unified: any): Map<string, number> {
  const totals = new Map<string, number>();
  if (!unified) return totals;
  const rows: any[] = Array.isArray(unified.by_provider_model) ? unified.by_provider_model : [];
  for (const row of rows) {
    const index = Number(row.provider_account_index);
    if (row.provider_canonical === "anthropic" && Number.isSafeInteger(index) && index >= 1) {
      const key = `anthropic:${index}`;
      totals.set(key, (totals.get(key) || 0) + (num(row.list_price_usd) || 0));
    }
  }
  const providers: any[] = Array.isArray(unified.by_provider) ? unified.by_provider : [];
  for (const row of providers) {
    if (row.provider === "anthropic") continue;
    const value = num(row.list_price_usd);
    if (value !== null) totals.set(String(row.provider), value);
  }
  return totals;
}

export function deriveSubscriptionValue(fees: any, unified: any): SubscriptionValueRow[] {
  const list = listPriceBySubscription(unified);
  const hasList = Boolean(unified && Array.isArray(unified.by_provider));
  const subscriptions: any[] = fees && Array.isArray(fees.subscriptions) ? fees.subscriptions : [];
  return subscriptions.map((row) => {
    const index = Number(row.provider_account_index);
    const listKey = row.provider === "anthropic" && Number.isSafeInteger(index) ? `anthropic:${index}` : String(row.provider);
    const feePerWeek = num(row.fee_per_week) || 0;
    const usageTracked = row.usage_tracked !== false;
    const listPriceWeek = hasList && usageTracked ? (list.get(listKey) ?? 0) : null;
    return {
      key: String(row.subscription),
      label: String(row.label || row.subscription),
      plan: row.plan ? String(row.plan) : null,
      feePerMonth: num(row.fee_per_month) || 0,
      feePerWeek,
      currency: String(row.currency || "USD"),
      feeSource: row.fee_source === "stated" ? "stated" : "receipt",
      statedAt: row.stated_at ? String(row.stated_at) : null,
      chargedAt: row.charged_at ? String(row.charged_at) : null,
      periodEnd: row.billing_period_end ? String(row.billing_period_end) : null,
      periodCurrent: row.period_current === true ? true : row.period_current === false ? false : null,
      usageTracked,
      listPriceWeek,
      multiple: listPriceWeek !== null && feePerWeek > 0 ? listPriceWeek / feePerWeek : null,
    };
  });
}

export function money(value: number | null, currency = "USD"): string {
  if (value === null || !Number.isFinite(value)) return "—";
  const text = value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return currency === "USD" ? `$${text}` : `${text} ${currency}`;
}
