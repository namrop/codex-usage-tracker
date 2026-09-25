// Web searches per backend (keeper, 2026-09-25, Discord unified usage ledger
// thread msg 1553067845766742228: "brave needs usage tracking too"). Brave is
// billed per request, monthly in arrears, so this month's requests times its
// receipt rate is the running bill. The search ledger holds Hermes calls from
// 2026-09-11 and OpenClaw calls from its whole transcript store; a month that
// began before the ledger's first call is a floor, not the bill.

export interface SearchBackendRow {
  backend: string;
  label: string;
  monthCalls: number;
  billableCalls: number;
  byHarness: Array<[string, number]>;
  weekCalls: number;
  weekRateLimited: number;
  weekErrors: number;
  rateUsd: number | null;
  estimatedUsd: number | null;
}

const BACKEND_LABELS: Record<string, string> = {
  brave: "Brave Search",
  zai: "Z.AI web search",
  keyless: "Keyless (no key)",
  firecrawl: "Firecrawl",
};

function num(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function deriveSearchRows(payload: any): SearchBackendRow[] {
  if (!payload) return [];
  const week = new Map<string, any>();
  for (const row of Array.isArray(payload.backends) ? payload.backends : []) week.set(String(row.backend), row);
  const month = new Map<string, any>();
  const monthRows: any[] = payload.month_to_date && Array.isArray(payload.month_to_date.backends) ? payload.month_to_date.backends : [];
  for (const row of monthRows) month.set(String(row.backend), row);
  const names = Array.from(new Set([...week.keys(), ...month.keys()]));
  const rows = names.map((backend) => {
    const w = week.get(backend) || {};
    const m = month.get(backend) || {};
    const byHarness = Object.entries(m.by_harness || {})
      .map(([name, count]) => [name, Number(count) || 0] as [string, number])
      .sort((a, b) => b[1] - a[1]);
    return {
      backend,
      label: BACKEND_LABELS[backend] || backend,
      monthCalls: Number(m.calls) || 0,
      billableCalls: Number(m.billable_calls) || 0,
      byHarness,
      weekCalls: Number(w.calls) || 0,
      weekRateLimited: Number(w.rate_limited) || 0,
      weekErrors: Number(w.error) || 0,
      rateUsd: num(m.rate_usd),
      estimatedUsd: num(m.estimated_usd),
    };
  });
  return rows.sort((a, b) => b.monthCalls - a.monthCalls || a.label.localeCompare(b.label));
}

// "counting since Sep 11" when the ledger starts after the month does.
export function coverageNote(payload: any): string | null {
  const mtd = payload && payload.month_to_date;
  if (!mtd || !mtd.ledger_since || !mtd.month_start) return null;
  const since = String(mtd.ledger_since).slice(0, 10);
  if (since <= String(mtd.month_start)) return null;
  const date = new Date(`${since}T00:00:00Z`);
  const text = date.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
  return `The ledger starts ${text}, so this month's counts are a floor.`;
}
