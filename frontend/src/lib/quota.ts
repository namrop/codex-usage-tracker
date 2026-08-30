// Subscription quota helpers — ported verbatim from the pre-React inline
// dashboard script.

export const QUOTA_WEEKLY_NAMES: ReadonlySet<string> = new Set(["week", "seven_day", "spark_week"]);
export const QUOTA_FIVE_HOUR_NAMES: ReadonlySet<string> = new Set(["five_hour", "spark_five_hour"]);

export interface QuotaRow {
  provider?: unknown;
  quota_name?: unknown;
  unit?: unknown;
  used_value?: unknown;
  limit_value?: unknown;
  remaining_value?: unknown;
  resets_at?: unknown;
  [key: string]: unknown;
}

export function quotaPercentFor(
  quotaLatest: QuotaRow[] | undefined,
  canonicalProvider: string,
  windowNames: ReadonlySet<string>,
): { pct: number; accounts: number } | null {
  const rows = (quotaLatest || []).filter((row) =>
    String(row.provider || "") === canonicalProvider
    && windowNames.has(String(row.quota_name || ""))
    && String(row.unit || "") === "percent"
    && row.used_value != null && row.limit_value != null);
  if (!rows.length) return null;
  let worst: number | null = null;
  rows.forEach((row) => {
    const pct = (Number(row.used_value) / Number(row.limit_value)) * 100;
    if (Number.isFinite(pct) && (worst === null || pct > worst)) worst = pct;
  });
  return worst === null ? null : { pct: worst, accounts: rows.length };
}

export function quotaConfidenceLabel(status: unknown): string {
  const normalized = String(status || "unknown").trim().toLowerCase();
  const labels: Record<string, string> = {
    exact: "Exact",
    estimated: "Estimated",
    reconstructed: "Reconstructed",
    partial: "Partial",
    mixed: "Mixed",
    unknown: "Unknown",
  };
  return labels[normalized] || "Unknown";
}

export function quotaConfidenceDash(confidenceStatus: unknown): number[] {
  const status = String(confidenceStatus || "unknown").trim().toLowerCase();
  return status === "exact" ? [] : [7, 5];
}

export interface NormalizedQuotaSeries {
  provider: string;
  label: string;
  quota_name: string;
  harness: string;
  confidence_status: string;
  provider_account_count: number;
  provider_account_index: number;
  values: (number | null)[];
}

export function normalizedQuotaSeries(series: any, bucketCount: number): NormalizedQuotaSeries | null {
  if (!series || typeof series !== "object" || !Array.isArray(series.values)) return null;
  const values = Array.from({ length: bucketCount }, (_unused, index) => {
    const raw = series.values[index];
    if (raw === null || raw === undefined || raw === "") return null;
    const value = Number(raw);
    return Number.isFinite(value) && value >= 0 && value <= 100 ? value : null;
  });
  if (!values.some((value) => value !== null)) return null;
  const providerAccountCount = Number(series.provider_account_count);
  const providerAccountIndex = Number(series.provider_account_index);
  return {
    provider: String(series.provider || "unknown"),
    label: String(series.label || series.quota_name || "Quota"),
    quota_name: String(series.quota_name || "unknown"),
    harness: series.harness ? String(series.harness) : "",
    confidence_status: String(series.confidence_status || "unknown").toLowerCase(),
    provider_account_count: Number.isSafeInteger(providerAccountCount) && providerAccountCount > 0
      ? providerAccountCount
      : 1,
    provider_account_index: Number.isSafeInteger(providerAccountIndex)
      && providerAccountIndex >= 1
      && providerAccountIndex <= providerAccountCount
      ? providerAccountIndex
      : 1,
    values,
  };
}

export function subscriptionUnifiedQuotaDash(row: {
  confidence_status?: unknown;
  provider_account_count?: unknown;
  provider_account_index?: unknown;
} | null | undefined): number[] {
  const status = String((row && row.confidence_status) || "unknown").trim().toLowerCase();
  const count = Number(row && row.provider_account_count);
  const index = Number(row && row.provider_account_index);
  if (!Number.isSafeInteger(count) || count <= 1 || !Number.isSafeInteger(index)) {
    return quotaConfidenceDash(status);
  }
  const exactPatterns = [[], [2, 4], [10, 4], [10, 3, 2, 3]];
  const uncertainPatterns = [[7, 5], [10, 3, 2, 3], [2, 3], [12, 3, 2, 3]];
  const patterns = status === "exact" ? exactPatterns : uncertainPatterns;
  return patterns[(index - 1) % patterns.length];
}

export function missingQuotaMessage(rows: any, period: string): string {
  if (!Array.isArray(rows) || !rows.length) return "";
  const missing = rows.map((row) => {
    const label = String((row && (row.label || row.provider)) || "Unknown provider");
    if (period === "five-hour" && row && row.provider === "openai-codex") {
      return "OpenAI / Codex reports no comparable five-hour quota; weekly data is not substituted";
    }
    const status = String((row && row.status) || "status unavailable").replace(/[-_]+/g, " ");
    return `${label} (${status})`;
  });
  if (period === "weekly") {
    return `No comparable primary weekly-equivalent data: ${missing.join("; ")}.`;
  }
  return `No comparable five-hour data: ${missing.join("; ")}.`;
}
