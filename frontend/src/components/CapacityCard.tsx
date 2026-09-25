import { useMemo } from "react";
import type { BurnPanelState, SubsPanelState } from "../hooks/useDashboardData";
import { useNow } from "../hooks/useNow";
import { useViewportWidth } from "../hooks/useViewportWidth";
import { projectionText } from "../lib/burn";
import {
  ageText,
  capacityRoom,
  capacityStateLabel,
  confidenceText,
  deriveCapacityAccounts,
  type CapacityAccount,
  type CapacityWindow,
} from "../lib/capacity";
import { formatTimestamp } from "../lib/format";
import { CountdownText } from "./ResetRail";

// Capacity by account (keeper authorization 2026-09-14): the first card on
// the Now view. One row per provider account, the binding window first,
// remaining amounts in each window's own units, reset countdowns, and the
// exact/estimated/partial confidence plus freshness of every reading. The
// derivation is lib/capacity.ts (Node-tested); this component only renders.

const KIND_LABEL: Record<CapacityWindow["kind"], string> = {
  model: "model window",
  model_specific: "model-specific cap",
  tool: "tool quota",
  allowance: "key allowance",
  balance: "balance",
};

// The phone breakpoint the style layer uses (app.css, max-width: 640px).
// Below it the explanatory caption collapses behind a native <details> so
// the first account row lands on the first screen (browser QA 2026-09-14:
// it started a full screen down); the text itself is unchanged.
export const PHONE_MAX_WIDTH = 640;

function amountInUnit(value: number, unit: string): string {
  const lowered = unit.trim().toLowerCase();
  if (lowered === "usd") return `$${value.toFixed(2)}`;
  const shown = Number.isInteger(value) ? String(value) : value.toFixed(2);
  return unit ? `${shown} ${unit}` : shown;
}

function WindowChip({ window, now }: { window: CapacityWindow; now: number }) {
  const classes = ["capacity-window", `kind-${window.kind}`];
  if (window.atCap) classes.push("is-at-cap");
  if (window.stale) classes.push("is-stale");
  const resetFuture = window.resetAt !== null && window.resetAt > now;
  const projection = window.projection;
  const projectionLine = projection && !window.stale && projection.status !== "not_applicable"
    ? projectionText(
      projection.status,
      projection.reason,
      projection.exhaustionAt ? formatTimestamp(projection.exhaustionAt) : null,
    )
    : null;
  return (
    <li className={classes.join(" ")}>
      <div className="capacity-window-head">
        <span className="capacity-window-name">{window.displayName}</span>
        <span className="capacity-window-kind">{KIND_LABEL[window.kind]}</span>
      </div>
      <strong className="capacity-window-remaining">{window.remainingText}</strong>
      {window.usedPct !== null && (
        <div
          className="capacity-meter"
          role="progressbar"
          aria-label={`${window.displayName} used`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Number(window.usedPct.toFixed(1))}
        >
          <div className="capacity-meter-fill" style={{ width: `${window.usedPct}%` }} />
          {window.elapsedPct !== null && !window.stale && (
            // Pace mark: how much of the week has gone. Used share past the
            // mark means ahead of an even pace.
            <div className="capacity-meter-pace" style={{ left: `${window.elapsedPct}%` }} aria-hidden="true" />
          )}
        </div>
      )}
      {window.usedPct !== null && window.elapsedPct !== null && !window.stale && (
        <span className="capacity-pace">
          {`${window.usedPct.toFixed(0)}% used · ${window.elapsedPct.toFixed(0)}% of the week gone`}
        </span>
      )}
      <div className="capacity-window-meta">
        <span className={`subscription-badge confidence-${window.confidence}`}>{confidenceText(window)}</span>
        {window.resolutionLabel && (
          // A native whole-point reading (OpenCode Go usage API): the integer
          // the provider reported, said as such — never "100.0%".
          <span className="capacity-resolution">
            {window.usedText ? `${window.usedText} used · ${window.resolutionLabel}` : window.resolutionLabel}
          </span>
        )}
        {window.resolutionLabel && window.providerState && (
          <span>{`provider state ${window.providerState.replace(/[-_]+/g, " ")}`}</span>
        )}
        <span className={window.stale ? "capacity-stale-text" : undefined}>
          {window.stale
            ? window.staleReason === "reset_passed_since_observation"
              ? `stale · reset passed since sample (${ageText(window.ageSeconds).replace(/^observed /, "")})`
              : `stale · ${ageText(window.ageSeconds)}`
            : ageText(window.ageSeconds)}
        </span>
        {resetFuture ? (
          <span>
            {"resets in "}
            <span className="mono"><CountdownText resetAt={window.resetAt as number} weekly={window.weekly} /></span>
          </span>
        ) : window.kind === "balance" ? (
          <span>no reset (balance)</span>
        ) : window.resetAt !== null ? (
          <span>{`reset ${formatTimestamp(new Date(window.resetAt).toISOString())}`}</span>
        ) : (
          <span>no reset reported</span>
        )}
        {projectionLine && <span className="capacity-projection">{projectionLine}</span>}
        {window.kind === "allowance" && window.usedValue !== null && (
          // The raw source field, named for what it is: the key's lifetime
          // usage is not this allowance's consumption.
          <span>{`key lifetime usage ${amountInUnit(window.usedValue, window.unit)}`}</span>
        )}
      </div>
    </li>
  );
}

function AccountRow({ account, now }: { account: CapacityAccount; now: number }) {
  return (
    <li className={`capacity-account state-${account.state}`}>
      <div className="capacity-account-head">
        <strong className="capacity-account-label">{account.label}</strong>
        <span className={`capacity-state state-${account.state}`}>{capacityStateLabel(account.state)}</span>
        <span className="capacity-headline">{account.headline}</span>
      </div>
      {account.note && <p className="capacity-note">{account.note}</p>}
      <ul className="capacity-windows" aria-label={`${account.label} windows`}>
        {account.windows.map((window) => (
          <WindowChip key={`${window.quotaName}-${window.harness}`} window={window} now={now} />
        ))}
      </ul>
    </li>
  );
}

export function CapacityCard({ subs, burn, thresholdSeconds }: {
  subs: SubsPanelState;
  burn: BurnPanelState;
  thresholdSeconds: number;
}) {
  // A one-second tick would re-derive every row; the state is re-derived
  // at hydrate cadence (the rows change) and the countdowns tick on their
  // own inside CountdownText.
  const now = useNow(60000);
  const phone = useViewportWidth() <= PHONE_MAX_WIDTH;
  const rows = subs.data && Array.isArray(subs.data.latest) ? subs.data.latest : null;
  const burnSeries = burn.data && Array.isArray(burn.data.series) ? burn.data.series : undefined;
  const accounts = useMemo(
    () => (rows ? deriveCapacityAccounts(rows, { now, maxAgeSeconds: thresholdSeconds, burnSeries }) : []),
    [rows, now, thresholdSeconds, burnSeries],
  );

  let message: string | null = null;
  if (subs.loading) {
    message = "Loading capacity…";
  } else if (subs.placeholder || !rows) {
    message = "Capacity unavailable: subscription observations could not be loaded.";
  } else if (!accounts.length) {
    message = "No quota observations are recorded, so no account capacity can be shown.";
  }
  // The room line (keeper-approved UI review item 4, 2026-09-25) replaces
  // the old count line: who has room and how much, who is low, who is out
  // and until when.
  const room = useMemo(() => capacityRoom(accounts, now), [accounts, now]);
  const roomParts: { key: string; label: string; items: string[] }[] = accounts.length
    ? [
      { key: "room", label: "Room", items: room.room },
      { key: "low", label: "Low", items: room.low },
      { key: "out", label: "Out", items: room.out },
      { key: "stale", label: "No fresh reading", items: room.stale },
    ].filter((part) => part.items.length)
    : [];

  const caption = (
    <p className="caption">
      One row per provider account: the window that binds, what remains in that window’s own units, when it resets, and how sure the reading is. The line above the rows sums it up: room left in each account’s tightest limit, accounts running low, and accounts that are out until a reset. On a weekly bar, the thin mark shows how much of the week has gone. Percentages are each account’s own cap and are never combined across providers; balances are amounts, not percentages; a model-specific cap, a tool quota or a key allowance at its limit constrains that model, tool or key without exhausting the account. While an account’s weekly or monthly cap is used up, its five-hour window is not shown.
    </p>
  );

  return (
    <section className="card capacity-card" aria-labelledby="capacityTitle">
      <div className="card-header">
        <div className="card-title">
          <h2 id="capacityTitle">Capacity by account</h2>
          {phone ? (
            <details className="card-brief">
              <summary>How to read this</summary>
              {caption}
            </details>
          ) : caption}
        </div>
      </div>
      <div id="capacityState" className="empty-state" hidden={!message}>{message || ""}</div>
      {subs.notice && !subs.loading && rows ? (
        <p className="lane-notice" id="capacityNotice">{subs.notice}</p>
      ) : null}
      <p id="capacitySummary" className="chart-summary capacity-room" aria-live="polite" hidden={!roomParts.length}>
        {roomParts.map((part) => (
          <span key={part.key} className={`capacity-room-part room-${part.key}`}>
            <strong>{`${part.label}:`}</strong>{` ${part.items.join(" · ")}`}
          </span>
        ))}
      </p>
      <ul id="capacityAccounts" className="capacity-list" hidden={Boolean(message)}>
        {accounts.map((account) => (
          <AccountRow key={account.key} account={account} now={now} />
        ))}
      </ul>
    </section>
  );
}
