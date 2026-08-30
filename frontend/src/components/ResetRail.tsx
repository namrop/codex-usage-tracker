import { formatCountdown } from "../lib/format";
import { providerCardDomId, providerKey } from "../lib/providers";
import { QUOTA_WEEKLY_NAMES, type QuotaRow } from "../lib/quota";
import { useNow } from "../hooks/useNow";

// Reset countdown chips (keeper-ratified flow rework, akasan 2026-08-30).
// The rail chips and the hero digest's soonest-reset chip render through the
// same component, so role class, ticking countdown, and the card anchor stay
// in one place. Same provider+window twice = a second account (account
// identifiers never cross the private API by design) — numbered by order.

export interface ResetChipModel {
  provider: string;
  cardKey: string;
  windowName: string;
  usedPct: number;
  resetAt: number;
  weekly: boolean;
  label: string;
}

export function deriveResetChips(rows: QuotaRow[]): ResetChipModel[] {
  const chips = rows
    .filter((row) => row.resets_at && String(row.unit || "") === "percent"
      && row.used_value != null && row.limit_value != null)
    .map((row) => ({
      provider: String(row.provider || "unknown"),
      cardKey: providerKey(String(row.provider || "unknown")),
      windowName: String(row.quota_name || ""),
      usedPct: (Number(row.used_value) / Number(row.limit_value)) * 100,
      resetAt: Date.parse(String(row.resets_at)),
      weekly: QUOTA_WEEKLY_NAMES.has(String(row.quota_name || "")),
      label: "",
    }))
    .filter((chip) => Number.isFinite(chip.resetAt) && Number.isFinite(chip.usedPct))
    .sort((a, b) => a.resetAt - b.resetAt);
  const seen = new Map<string, number>();
  chips.forEach((chip) => {
    const dupKey = `${chip.provider}:${chip.windowName}`;
    const ordinal = (seen.get(dupKey) || 0) + 1;
    seen.set(dupKey, ordinal);
    chip.label = ordinal > 1 ? `${chip.provider} (${ordinal})` : chip.provider;
  });
  return chips;
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

export function ResetRail({ chips, loading }: { chips: ResetChipModel[]; loading: boolean }) {
  const summary = loading
    ? "Loading subscription windows…"
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
