import { useMemo, useState } from "react";
import type { Palette } from "../lib/palette";
import { subscriptionQuotaChartOptions, type ChartConfig } from "../lib/chart";
import { formatTimestamp } from "../lib/format";
import {
  PROVIDER_DISPLAY_NAMES,
  PROVIDER_ORDER,
  providerCardDomId,
  providerKey,
  subscriptionSeriesDisplayLabel,
  subscriptionUnifiedProviderColor,
} from "../lib/providers";
import {
  missingQuotaMessage,
  normalizedQuotaSeries,
  quotaConfidenceLabel,
  subscriptionUnifiedQuotaDash,
  type NormalizedQuotaSeries,
} from "../lib/quota";
import { formatUtcHour, percent } from "../lib/format";
import type { SubsPanelState } from "../hooks/useDashboardData";
import { ChartCanvas } from "./ChartCanvas";
import { DirectLabels } from "./DirectLabels";
import { Fold, tableFoldSummary } from "./Fold";
import { SeriesTable } from "./SeriesTable";
import { TextRow } from "./UnifiedUsageCard";

function subscriptionQuotaDatasets(palette: Palette, series: NormalizedQuotaSeries[]): Record<string, unknown>[] {
  return series.map((row, index) => {
    const color = subscriptionUnifiedProviderColor(palette, row, index);
    const status = row.confidence_status;
    return {
      label: `${subscriptionSeriesDisplayLabel(row)} — ${quotaConfidenceLabel(status)} confidence`,
      data: row.values,
      borderColor: color,
      backgroundColor: color,
      borderDash: subscriptionUnifiedQuotaDash(row),
      borderWidth: 2.4,
      pointRadius: 0,
      pointHitRadius: 10,
      pointHoverRadius: 3,
      fill: false,
      spanGaps: false,
      tension: 0,
    };
  });
}

function quotaTableCell(value: unknown): string {
  return value === null ? "—" : percent(value);
}

interface HistoryCardProps {
  palette: Palette;
  prefix: string;
  period: "weekly" | "five-hour";
  seriesDescription: string;
  chartBucketLimit?: number;
  projection: any;
  bucketStarts: string[];
  labels: string[];
  // Non-null = charts are ruled unavailable (stale/empty/malformed history);
  // the message replaces the summary and the canvas hides.
  unavailableMessage: string | null;
  title: string;
  caption: string;
  loadingText: string;
  loaded: boolean;
  foldKey: string;
  foldLabel: string;
  tableCaption: string;
  canvasAriaLabel: string;
  labelsAriaLabel: string;
}

function SubscriptionHistoryCard({
  palette, prefix, period, seriesDescription, chartBucketLimit, projection, bucketStarts, labels,
  unavailableMessage, title, caption, loadingText, loaded, foldKey, foldLabel, tableCaption,
  canvasAriaLabel, labelsAriaLabel,
}: HistoryCardProps) {
  const [chartOk, setChartOk] = useState(true);

  const rows = useMemo(() => (
    unavailableMessage
      ? []
      : ((Array.isArray(projection.series) ? projection.series : []) as any[])
        .map((series) => normalizedQuotaSeries(series, bucketStarts.length))
        .filter((series): series is NormalizedQuotaSeries => series !== null)
  ), [projection, bucketStarts, unavailableMessage]);

  const missingMessage = unavailableMessage ? "" : missingQuotaMessage(projection.no_data_providers, period);

  // Chart-window cap (polish round item 11): the five-hour sawtooth is
  // unreadable across 168 buckets, so its CANVAS plots only the trailing
  // slice; the fold table renders from the unsliced rows and keeps the full
  // window.
  const limit = Number(chartBucketLimit);
  const bounded = Number.isFinite(limit) && limit > 0 && limit < labels.length;
  const config: ChartConfig | null = useMemo(() => {
    if (unavailableMessage || !rows.length) return null;
    const chartLabels = bounded ? labels.slice(-limit) : labels;
    const chartRows = bounded
      ? rows.map((row) => ({ ...row, values: row.values.slice(-limit) }))
      : rows;
    return {
      type: "line",
      data: { labels: chartLabels, datasets: subscriptionQuotaDatasets(palette, chartRows) },
      options: subscriptionQuotaChartOptions(palette),
    };
  }, [unavailableMessage, rows, bounded, labels, limit, palette]);

  let summary: string;
  if (!loaded) {
    summary = loadingText;
  } else if (unavailableMessage) {
    summary = unavailableMessage;
  } else if (!rows.length) {
    summary = missingMessage
      || `No comparable ${seriesDescription}: no mapped provider series was returned (status unavailable).`;
  } else {
    const windowNote = bounded ? ` Chart shows the last ${limit} hours; the table keeps the full window.` : "";
    summary = `${rows.length} comparable ${seriesDescription}.${windowNote} ${missingMessage}`.trim();
    if (!chartOk) {
      summary = `${summary} Chart drawing is unavailable; the accessible data table remains current.`;
    }
  }

  const labelRows = rows.map((row, index) => {
    const statusText = `${quotaConfidenceLabel(row.confidence_status)} confidence`;
    const dashPattern = subscriptionUnifiedQuotaDash(row);
    return {
      label: subscriptionSeriesDisplayLabel(row),
      detail: statusText,
      color: subscriptionUnifiedProviderColor(palette, row, index),
      dashed: Array.isArray(dashPattern) && dashPattern.length > 0,
      // The aria surface keeps the FULL series label — the short form is a
      // sighted-scan affordance, not an identity change.
      ariaLabel: `${row.label}: ${statusText}`,
    };
  });

  return (
    <article className="subscription-unified-history-card" aria-labelledby={`${prefix}Title`}>
      <h3 id={`${prefix}Title`}>{title}</h3>
      <p id={`${prefix}Description`} className="caption">{caption}</p>
      <p id={`${prefix}Summary`} className="chart-summary" aria-live="polite">{summary}</p>
      <div className="subscription-quota-chart-layout">
        <div className="subscription-quota-chart-wrap">
          <ChartCanvas
            id={`${prefix}Chart`}
            ariaLabel={canvasAriaLabel}
            role="img"
            ariaDescribedBy={`${prefix}Description ${prefix}Summary`}
            config={config}
            hidden={!config}
            onRendered={setChartOk}
          />
        </div>
        <DirectLabels
          id={`${prefix}Labels`}
          className="direct-labels subscription-chart-labels"
          ariaLabel={labelsAriaLabel}
          rows={labelRows}
        />
      </div>
      <Fold
        foldKey={foldKey}
        label={foldLabel}
        headingLevel={4}
        summary={tableFoldSummary({
          rows: rows.length ? bucketStarts.length : 0,
          singular: "hour",
          columns: rows.length,
          colNoun: "series",
        })}
      >
        <SeriesTable
          headId={`${prefix}TableHead`}
          bodyId={`${prefix}TableBody`}
          caption={tableCaption}
          bucketStarts={rows.length ? bucketStarts : []}
          rows={rows}
          formatCell={quotaTableCell}
          headLabel={(row) => `${row.label} (${quotaConfidenceLabel((row as NormalizedQuotaSeries).confidence_status)} confidence)`}
        />
      </Fold>
    </article>
  );
}

// ── Provider cards ──────────────────────────────────────────────────────

interface ProviderGroup {
  key: string;
  providerIds: Set<string>;
  rows: any[];
}

function hasQuotaValue(value: unknown): boolean {
  return value !== null && value !== undefined && value !== "";
}

function quotaNumberText(value: unknown): string {
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  return Number.isInteger(num) ? String(num) : num.toFixed(2);
}

function quotaAmount(value: unknown, unit: unknown): string {
  if (!hasQuotaValue(value)) return "Unavailable";
  if (!hasQuotaValue(unit)) return quotaNumberText(value);
  const unitText = String(unit);
  // "percent" is the ledger's spelling of "%": never print the word.
  if (unitText === "%" || unitText.trim().toLowerCase() === "percent") return `${quotaNumberText(value)}%`;
  if (unitText.toLowerCase() === "usd") {
    const num = Number(value);
    if (Number.isFinite(num)) return `${num < 0 ? "−" : ""}$${Math.abs(num).toFixed(2)}`;
  }
  return `${quotaNumberText(value)} ${unitText}`;
}

function confidencePresentation(row: any): { key: string; label: string } {
  const confidence = String(row.measurement_confidence || "unknown").toLowerCase();
  if (confidence === "exact") return { key: "exact", label: "Exact" };
  if (confidence === "estimated") return { key: "estimated", label: "Estimated / partial" };
  return { key: "unknown", label: "Unknown" };
}

function presentationExplanation(row: any, confidence: { key: string }): string {
  const parts: string[] = [];
  if (confidence.key === "estimated") {
    parts.push("Partial estimate; not an exact provider observation.");
  }
  if (hasQuotaValue(row.coverage_status)) {
    parts.push(`Coverage: ${String(row.coverage_status).replace(/[-_]+/g, " ")}.`);
  }
  if (hasQuotaValue(row.provider_state)) {
    parts.push(`Provider state: ${String(row.provider_state).replace(/[-_]+/g, " ")}.`);
  }
  return parts.join(" ");
}

function Badge({ text, className }: { text: string; className: string }) {
  return <span className={`subscription-badge ${className}`}>{text}</span>;
}

function QuotaStat({ label, value, roleClass, title }: {
  label: string;
  value: string;
  roleClass?: string;
  title?: string;
}) {
  return (
    <div className="subscription-quota-stat">
      <div className="label">{label}</div>
      <div className={`subscription-quota-value${roleClass ? ` ${roleClass}` : ""}`} title={title}>{value}</div>
    </div>
  );
}

function SubscriptionWindow({ row }: { row: any }) {
  const confidence = confidencePresentation(row);
  const usedNumber = hasQuotaValue(row.used_value) ? Number(row.used_value) : NaN;
  const limitNumber = hasQuotaValue(row.limit_value) ? Number(row.limit_value) : NaN;
  const remainingNumber = hasQuotaValue(row.remaining_value) ? Number(row.remaining_value) : NaN;
  const rawUnit = hasQuotaValue(row.unit) ? String(row.unit) : "";
  // `provider_unit` with a limit of 100 is a normalized percentage (kimi),
  // so it displays as one; the provider's own unit name stays in the
  // tooltip (item 10 — display only, canonical data untouched).
  const normalizedPercent = rawUnit === "provider_unit" && limitNumber === 100;
  const displayUnit = normalizedPercent ? "%" : rawUnit;
  const unitProvenance = normalizedPercent
    ? "provider_unit — normalized 0–100 scale reported by the provider"
    : "";
  const windowKindText = String(row.window_kind || "").toLowerCase();
  const balanceUnit = rawUnit.toLowerCase() === "usd" || rawUnit.toLowerCase() === "credits";
  // Lifetime/unknown windows have no cap to fill toward, and used > limit
  // means the two figures are different meters (item 8 — openrouter's
  // lifetime key spend vs credit limit), so neither gets a used/limit bar.
  const meterless = windowKindText === "lifetime"
    || windowKindText === "unknown"
    || (Number.isFinite(usedNumber) && Number.isFinite(limitNumber) && usedNumber > limitNumber);
  // A balance is an amount held, not a fraction of a cap (item 9 —
  // deepseek account_balance).
  const balanceOnly = balanceUnit
    && (!hasQuotaValue(row.used_value) || !hasQuotaValue(row.limit_value));
  const balanceRole = Number.isFinite(remainingNumber)
    ? (remainingNumber > 0 ? "stability" : "violation")
    : "";

  const metadataParts: string[] = [];
  if (hasQuotaValue(row.harness)) metadataParts.push(`Harness: ${String(row.harness)}`);
  // Two accounts on one provider share harness and quota name; the account
  // index is the only thing that tells their windows apart.
  const accountCount = Number(row.provider_account_count);
  const accountIndex = Number(row.provider_account_index);
  if (Number.isSafeInteger(accountCount) && accountCount > 1) {
    metadataParts.push(`Account ${Number.isSafeInteger(accountIndex) ? accountIndex : "?"} of ${accountCount}`);
  }
  if (hasQuotaValue(row.quota_scope)) metadataParts.push(`Scope: ${String(row.quota_scope)}`);
  metadataParts.push(`Unit: ${displayUnit || "unavailable"}`);

  const ratioIsAuthoritative = !balanceOnly
    && !meterless
    && confidence.key === "exact"
    && Number.isFinite(usedNumber)
    && Number.isFinite(limitNumber)
    && limitNumber > 0;
  const ratio = ratioIsAuthoritative
    ? Math.max(0, Math.min(100, (usedNumber / limitNumber) * 100))
    : 0;

  const usedText = quotaAmount(row.used_value, displayUnit);
  const limitText = quotaAmount(row.limit_value, displayUnit);
  const usedAndLimit = !hasQuotaValue(row.used_value) && !hasQuotaValue(row.limit_value)
    ? "Unavailable"
    : `${usedText} / ${limitText}`;

  const explanationText = presentationExplanation(row, confidence);

  return (
    <section className={confidence.key === "estimated" ? "subscription-window is-estimated" : "subscription-window"}>
      <div className="subscription-window-header">
        <h4 className="subscription-window-title">
          {hasQuotaValue(row.quota_name) ? String(row.quota_name) : "Unnamed quota"}
        </h4>
        <div className="subscription-window-badges">
          <Badge text={confidence.label} className={`confidence-${confidence.key}`} />
          <Badge text={hasQuotaValue(row.window_kind) ? String(row.window_kind) : "Unknown window"} className="unknown" />
        </div>
      </div>
      <p className="subscription-window-meta" title={unitProvenance || undefined}>{metadataParts.join(" · ")}</p>
      <div className="subscription-quota-grid">
        {balanceOnly ? (
          <>
            <QuotaStat
              label="Balance"
              value={quotaAmount(row.remaining_value, displayUnit)}
              roleClass={balanceRole ? `role-${balanceRole}` : undefined}
            />
            {Number.isFinite(usedNumber) && (
              <QuotaStat label="Spent" value={quotaAmount(row.used_value, displayUnit)} />
            )}
          </>
        ) : meterless ? (
          <>
            <QuotaStat label="Spent" value={quotaAmount(row.used_value, displayUnit)} />
            <QuotaStat
              label="Remaining"
              value={quotaAmount(row.remaining_value, displayUnit)}
              roleClass={balanceUnit && balanceRole ? `role-${balanceRole}` : undefined}
              title={hasQuotaValue(row.limit_value)
                ? `Provider-reported limit: ${quotaAmount(row.limit_value, displayUnit)}`
                : undefined}
            />
          </>
        ) : (
          <>
            <QuotaStat label="Used / limit" value={usedAndLimit} title={unitProvenance || undefined} />
            <QuotaStat
              label="Remaining"
              value={quotaAmount(row.remaining_value, displayUnit)}
              title={unitProvenance || undefined}
            />
          </>
        )}
      </div>
      {ratioIsAuthoritative && (
        <div
          className="subscription-progress"
          role="progressbar"
          aria-label="Authoritative quota used"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Number(ratio.toFixed(1))}
        >
          <div className="subscription-progress-fill" style={{ width: `${ratio}%` }} />
        </div>
      )}
      <p className="subscription-window-time">
        {`Window: ${formatTimestamp(row.window_started_at)} → ${formatTimestamp(row.window_ends_at)} · Reset: ${formatTimestamp(row.resets_at)}`}
      </p>
      {explanationText && <p className="subscription-explanation">{explanationText}</p>}
    </section>
  );
}

function readableProviderName(group: ProviderGroup): string {
  if (PROVIDER_DISPLAY_NAMES[group.key]) return PROVIDER_DISPLAY_NAMES[group.key];
  return group.key;
}

function ProviderCard({ group, thresholdSeconds }: { group: ProviderGroup; thresholdSeconds: number }) {
  const observedTimes = group.rows
    .map((row) => Date.parse(row.observed_at))
    .filter((value) => Number.isFinite(value));
  const newestObservedMs = observedTimes.length ? Math.max(...observedTimes) : null;
  let providerFreshness = "unknown";
  if (newestObservedMs !== null) {
    const ageSeconds = (Date.now() - newestObservedMs) / 1000;
    providerFreshness = ageSeconds <= thresholdSeconds ? "fresh" : "stale";
  }
  const freshnessLabel = providerFreshness === "fresh"
    ? "Fresh"
    : providerFreshness === "stale" ? "Stale" : "Unknown freshness";
  // Account index first so a second account's windows sit together, then
  // quota name within the account.
  const sortedRows = group.rows.slice().sort((left, right) => {
    const leftAccount = Number(left.provider_account_index) || 1;
    const rightAccount = Number(right.provider_account_index) || 1;
    if (leftAccount !== rightAccount) return leftAccount - rightAccount;
    return String(left.quota_name || "").localeCompare(String(right.quota_name || ""));
  });

  return (
    <article className="subscription-provider-card" id={providerCardDomId(group.key)}>
      <div className="subscription-provider-header">
        <div className="subscription-provider-title">
          <h3>{readableProviderName(group)}</h3>
          <div className="subscription-provider-id mono">{Array.from(group.providerIds).join(" · ")}</div>
        </div>
        <Badge text={freshnessLabel} className={providerFreshness} />
      </div>
      <p className="subscription-provider-observed">
        {`Last observation: ${newestObservedMs === null ? "Unavailable" : formatTimestamp(new Date(newestObservedMs).toISOString())} · freshness threshold ${Math.round(thresholdSeconds / 60)} min`}
      </p>
      <div className="subscription-window-list">
        {!sortedRows.length && (
          <p className="empty-state">No quota windows available for this provider.</p>
        )}
        {sortedRows.map((row, index) => (
          <SubscriptionWindow
            key={`${row.quota_name}-${row.harness}-${Number(row.provider_account_index) || 1}-${index}`}
            row={row}
          />
        ))}
      </div>
    </article>
  );
}

function PlaceholderProviderCard({ providerId, displayName }: { providerId: string; displayName: string }) {
  return (
    <article className="subscription-provider-card" id={providerCardDomId(providerId)}>
      <div className="subscription-provider-header">
        <div className="subscription-provider-title">
          <h3>{displayName}</h3>
          <div className="subscription-provider-id mono">{providerId}</div>
        </div>
        <Badge text="Unknown freshness" className="unknown" />
      </div>
      <p className="empty-state">No quota windows available for this provider.</p>
    </article>
  );
}

// ── The card ────────────────────────────────────────────────────────────

export function SubscriptionsCard({ palette, subs }: { palette: Palette; subs: SubsPanelState }) {
  const data = subs.data;
  const rows: any[] = data && Array.isArray(data.latest) ? data.latest : [];
  const freshness = (data && data.freshness) || {};

  const statusMessage = subs.loading
    ? "Loading subscription observations…"
    : subs.notice;
  const contentVisible = !subs.loading && (Boolean(data) || subs.placeholder);

  // Charts hidden while observations are stale/empty (honest quota meters,
  // polish round) — the ruled messages, verbatim.
  const timeSeries = data ? data.time_series : null;
  let unavailableMessage: string | null = null;
  if (subs.placeholder || !data) {
    unavailableMessage = "Subscription quota history is unavailable.";
  } else if (freshness.status === "stale") {
    unavailableMessage = "Subscription quota history is stale; charts are hidden until current observations arrive.";
  } else if (freshness.status === "empty") {
    unavailableMessage = "Subscription quota history is unavailable because the quota ledger is empty.";
  } else if (!timeSeries || typeof timeSeries !== "object" || !Array.isArray(timeSeries.bucket_starts) || !timeSeries.bucket_starts.length) {
    unavailableMessage = "No bounded subscription quota history is available for this 168-hour window.";
  }

  const bucketStarts: string[] = unavailableMessage
    ? []
    : (timeSeries.bucket_starts as unknown[]).map((value) => String(value));
  const labels = bucketStarts.map((value) => formatUtcHour(value, true));
  const weekly = !unavailableMessage && timeSeries.unified_weekly && typeof timeSeries.unified_weekly === "object"
    ? timeSeries.unified_weekly
    : {};
  const fiveHour = !unavailableMessage && timeSeries.unified_five_hour && typeof timeSeries.unified_five_hour === "object"
    ? timeSeries.unified_five_hour
    : {};

  // Provider grouping: the rail chips anchor to the provider cards, so both
  // resolve the same canonical group key (item 13).
  const groups = useMemo(() => {
    if (subs.placeholder || !data) return null;
    const providerGroups = new Map<string, ProviderGroup>();
    const ensureProviderGroup = (provider: unknown): ProviderGroup => {
      const rawProvider = String(provider || "unknown").trim() || "unknown";
      const key = providerKey(rawProvider);
      if (!providerGroups.has(key)) {
        providerGroups.set(key, { key, providerIds: new Set(), rows: [] });
      }
      const group = providerGroups.get(key)!;
      group.providerIds.add(rawProvider);
      return group;
    };
    PROVIDER_ORDER.forEach((provider) => ensureProviderGroup(provider));
    rows.forEach((row) => {
      ensureProviderGroup(row.provider).rows.push(row);
    });
    return Array.from(providerGroups.values()).sort((left, right) => {
      const leftIndex = PROVIDER_ORDER.indexOf(left.key);
      const rightIndex = PROVIDER_ORDER.indexOf(right.key);
      const leftRank = leftIndex === -1 ? PROVIDER_ORDER.length : leftIndex;
      const rightRank = rightIndex === -1 ? PROVIDER_ORDER.length : rightIndex;
      if (leftRank !== rightRank) return leftRank - rightRank;
      const displayOrder = readableProviderName(left).localeCompare(
        readableProviderName(right), undefined, { sensitivity: "variant" },
      );
      if (displayOrder !== 0) return displayOrder;
      return left.key < right.key ? -1 : left.key > right.key ? 1 : 0;
    });
  }, [subs.placeholder, data, rows]);

  const thresholdCandidate = Number(freshness.threshold_seconds);
  const maxAgeCandidate = Number(freshness.max_age_seconds);
  const thresholdSeconds = Number.isFinite(thresholdCandidate) && thresholdCandidate > 0
    ? thresholdCandidate
    : Number.isFinite(maxAgeCandidate) && maxAgeCandidate > 0
      ? maxAgeCandidate
      : 21600;

  const rawSummary = tableFoldSummary({ rows: rows.length, singular: "observation" });

  return (
    <section className="card" aria-labelledby="subscriptionsTitle">
      <div className="card-header">
        <div className="card-title">
          <h2 id="subscriptionsTitle">Subscriptions</h2>
          <p className="caption">Provider quota windows, authority, reset times, and observation freshness; normalized utilization charts compare percentages only — never tokens or dollars.</p>
          <p className="caption">Usage events above, subscription quota observations here, and billing facts below are separate record classes. No quota estimate is a billing fact.</p>
        </div>
      </div>
      <div id="subscriptionsState" className="empty-state" hidden={!statusMessage}>{statusMessage || ""}</div>
      <div id="subscriptionsContent" hidden={!contentVisible}>
        <section className="subscription-quota-history" aria-label="Unified subscription quota history">
          <div className="subscription-unified-history-grid">
            <SubscriptionHistoryCard
              palette={palette}
              prefix="subscriptionUnifiedWeekly"
              period="weekly"
              seriesDescription="primary weekly-equivalent provider series"
              projection={weekly}
              bucketStarts={bucketStarts}
              labels={labels}
              unavailableMessage={unavailableMessage}
              title="Unified weekly subscription utilization"
              caption="Each provider’s explicitly mapped primary weekly-equivalent quota on a normalized 0–100% scale."
              loadingText="Loading weekly subscription quota history…"
              loaded={!subs.loading}
              foldKey="subscriptionWeeklySeries"
              foldLabel="Hourly unified weekly quota data"
              tableCaption="Primary weekly-equivalent provider quota utilization per complete UTC hour"
              canvasAriaLabel="Unified weekly subscription quota utilization from 0 to 100 percent"
              labelsAriaLabel="Unified weekly subscription series and confidence statuses"
            />
            <SubscriptionHistoryCard
              palette={palette}
              prefix="subscriptionUnifiedFiveHour"
              period="five-hour"
              seriesDescription="five-hour provider series"
              chartBucketLimit={48}
              projection={fiveHour}
              bucketStarts={bucketStarts}
              labels={labels}
              unavailableMessage={unavailableMessage}
              title="Unified five-hour subscription utilization"
              caption="Each provider’s explicitly mapped comparable five-hour quota over the last 48 hours on a normalized 0–100% scale (the folded hourly table keeps the full week)."
              loadingText="Loading five-hour subscription quota history…"
              loaded={!subs.loading}
              foldKey="subscriptionFiveHourSeries"
              foldLabel="Hourly unified five-hour quota data"
              tableCaption="Comparable five-hour provider quota utilization per complete UTC hour"
              canvasAriaLabel="Unified five-hour subscription quota utilization from 0 to 100 percent"
              labelsAriaLabel="Unified five-hour subscription series and confidence statuses"
            />
          </div>
        </section>
        <div id="subscriptionProviderGrid" className="subscription-provider-grid">
          {subs.placeholder || !groups
            ? Object.entries(PROVIDER_DISPLAY_NAMES).map(([providerId, displayName]) => (
              <PlaceholderProviderCard key={providerId} providerId={providerId} displayName={displayName} />
            ))
            : groups.map((group) => (
              <ProviderCard key={group.key} group={group} thresholdSeconds={thresholdSeconds} />
            ))}
        </div>
        <Fold foldKey="subscriptionsRaw" label="Raw quota observation ledger" summary={rawSummary} colsToggle>
          <div className="table-wrap">
            <table className="tier-quota">
              <caption>Latest quota observations; these are not usage events or billing facts.</caption>
              <thead>
                <tr><th>Provider</th><th>Observed</th><th>Confidence</th><th>Harness</th><th>Quota</th><th>Window</th><th>Used</th><th>Remaining</th><th>Limit</th><th>Unit</th><th>Starts</th><th>Ends</th><th>Resets</th></tr>
              </thead>
              <tbody id="subscriptionsBody">
                {rows.map((row, index) => (
                  <TextRow
                    key={`${row.provider}-${row.quota_name}-${index}`}
                    values={[
                      row.provider, formatTimestamp(row.observed_at), row.measurement_confidence,
                      row.harness, row.quota_name, row.window_kind, row.used_value,
                      row.remaining_value, row.limit_value, row.unit, formatTimestamp(row.window_started_at),
                      formatTimestamp(row.window_ends_at), formatTimestamp(row.resets_at),
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
