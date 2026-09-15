import { formatCountdown } from "../lib/format";
import { accountDisplayLabel, providerCardDomId, providerKey } from "../lib/providers";
import { QUOTA_WEEKLY_NAMES, isPercentQuotaRow, quotaRowProviderKey, type QuotaRow } from "../lib/quota";
import { useNow } from "../hooks/useNow";

// Reset countdown chips (keeper-ratified flow rework, akasan 2026-08-30).
// The rail chips and the hero digest's soonest-reset chip render through the
// same component, so role class, ticking countdown, and the card anchor stay
// in one place. Only rows the provider measured EXACTLY qualify — the rail
// claims "exact resets" and an estimated window has no honest countdown.
// Same provider+window twice = a second account. When the API carries a
// provider_account_count above one every chip of that provider is numbered
// through accountDisplayLabel ("Claude 1", "Claude 2" — the one label the
// capacity summary, the burn table and the provider cards also use);
// without it, a duplicate window is numbered by order as "(2)".

export interface ResetChipModel {
  // Canonical provider id (identity); `label` is the display form.
  provider: string;
  cardKey: string;
  windowName: string;
  usedPct: number;
  resetAt: number;
  weekly: boolean;
  label: string;
}

export interface ResetChipOptions {
  now: number;
  // Rows observed longer ago than this are not countdowns: a chip claims an
  // exact reset, and a stale lane's last sample (2026-09-10: Z.AI, twelve
  // hours old, its five-hour window long past) read "resetting…" at the top
  // of the rail and in the hero's soonest-reset slot. Same chain the
  // subscription cards use for their freshness badge.
  maxAgeSeconds: number;
}

export function deriveResetChips(rows: QuotaRow[], options?: ResetChipOptions): ResetChipModel[] {
  const chips = rows
    .filter((row) => row.resets_at
      && String(row.measurement_confidence || "").trim().toLowerCase() === "exact"
      && isPercentQuotaRow(row)
      && row.used_value != null && row.limit_value != null)
    .filter((row) => {
      if (!options) return true;
      const observed = Date.parse(String(row.observed_at || ""));
      return Number.isFinite(observed) && (options.now - observed) / 1000 <= options.maxAgeSeconds;
    })
    .map((row) => {
      const raw = String(row.provider || "unknown");
      const canonical = quotaRowProviderKey(row);
      // provider_canonical when the API carries it; otherwise the alias map
      // resolves the raw ledger value to the same card-group key.
      const cardKey = row.provider_canonical ? String(row.provider_canonical) : providerKey(canonical || raw);
      const accountCount = Number(row.provider_account_count);
      const accountIndex = Number(row.provider_account_index);
      return {
        provider: cardKey,
        cardKey,
        windowName: String(row.quota_name || ""),
        usedPct: (Number(row.used_value) / Number(row.limit_value)) * 100,
        resetAt: Date.parse(String(row.resets_at)),
        weekly: QUOTA_WEEKLY_NAMES.has(String(row.quota_name || "")),
        label: "",
        accountLabel: accountDisplayLabel({ ...row, provider_canonical: cardKey }),
        numbered: Number.isSafeInteger(accountCount) && accountCount > 1
          && Number.isSafeInteger(accountIndex) && accountIndex >= 1,
      };
    })
    .filter((chip) => Number.isFinite(chip.resetAt) && Number.isFinite(chip.usedPct))
    .sort((a, b) => a.resetAt - b.resetAt);
  const seen = new Map<string, number>();
  return chips.map(({ accountLabel, numbered, ...chip }) => {
    const dupKey = `${chip.provider}:${chip.windowName}`;
    const byOrder = (seen.get(dupKey) || 0) + 1;
    seen.set(dupKey, byOrder);
    const label = numbered
      ? accountLabel
      : byOrder > 1 ? `${accountLabel} (${byOrder})` : accountLabel;
    return { ...chip, label };
  });
}

export function chipRole(usedPct: number, msLeft: number): string {
  if (usedPct >= 90) return "role-violation";
  if (usedPct >= 70 && msLeft > 3600000) return "role-threshold";
  return "role-stability";
}

// Wall-clock countdown text; quiet when the tab is hidden (useNow). Reduced
// motion still gets text updates — a countdown IS the content.
export function CountdownText({ resetAt, weekly }: { resetAt: number; weekly: boolean }) {
  const now = useNow();
  return <>{formatCountdown(resetAt - now, weekly)}</>;
}

export function ResetChip({ chip, id, hidden }: { chip: ResetChipModel; id?: string; hidden?: boolean }) {
  // Role class is computed at hydrate-render time (like the pre-React
  // fillResetChip); only the countdown text ticks between hydrates.
  const role = chipRole(chip.usedPct, chip.resetAt - Date.now());
  return (
    <a
      id={id}
      className={`reset-chip ${role}`}
      href={`#${providerCardDomId(chip.cardKey)}`}
      hidden={hidden}
      data-reset-at={String(chip.resetAt)}
      data-weekly={chip.weekly ? "1" : ""}
    >
      <strong>{chip.label}</strong>
      <span className="chip-window">{` ${chip.windowName} · ${chip.usedPct.toFixed(0)}% used · `}</span>
      <span className="chip-countdown"><CountdownText resetAt={chip.resetAt} weekly={chip.weekly} /></span>
    </a>
  );
}

export function ResetRail({ chips, loading, unavailable = false }: {
  chips: ResetChipModel[];
  loading: boolean;
  // A failed subscriptions fetch with no last-good payload: the rail must
  // not read "Loading…" forever.
  unavailable?: boolean;
}) {
  const summary = loading
    ? "Loading subscription windows…"
    : unavailable
      ? "Subscription windows unavailable."
      : chips.length
        ? `${chips.length} windows reporting exact resets; soonest first.`
        : "No quota windows report an exact reset time.";
  return (
    <>
      <p id="resetRailSummary" className="chart-summary" aria-live="polite">{summary}</p>
      <ul id="resetRailChips" className="reset-chips" aria-label="Quota reset countdowns">
        {chips.map((chip, index) => (
          <li key={`${chip.provider}-${chip.windowName}-${index}`}>
            {/* A real anchor (item 13): keyboard reachable, targets the
                provider's subscription card; scroll behavior is the CSS
                layer's reduced-motion-aware call. */}
            <ResetChip chip={chip} />
          </li>
        ))}
      </ul>
    </>
  );
}
