import { formatTimestamp, intFormat } from "../lib/format";
import { cededLanes, staleLaneSuffix, type Freshness } from "../lib/freshness";
import { ResetChip, type ResetChipModel } from "./ResetRail";

// Ops digest: mirrors of the unified tile row's state (freshness, soonest
// reset, week list-price, substitution watch) — the digest derives from the
// same state the panels render from; it never fetches.

export function OpsHero({ freshness, soonestChip, subsLoaded, subsUnavailable = false, unifiedData }: {
  freshness: Freshness | null;
  soonestChip: ResetChipModel | undefined;
  subsLoaded: boolean;
  // The subscriptions fetch failed with nothing to fall back on: say so
  // rather than "waiting" forever.
  subsUnavailable?: boolean;
  unifiedData: any | null;
}) {
  const status = freshness ? String(freshness.status || "unknown") : null;
  const ceded = freshness ? cededLanes(freshness).map((lane) => String(lane.lane)) : [];
  const freshnessRole = freshness
    ? {
      // Stale-beyond-grace and ceded lanes wear the violation role (ruled
      // mapping in the style adapter header).
      "role-stability": status === "fresh" && !ceded.length,
      "role-violation": status === "stale" || status === "empty" || ceded.length > 0,
    }
    : {};
  const freshnessClasses = ["digest-item"];
  if (freshnessRole["role-stability"]) freshnessClasses.push("role-stability");
  if (freshnessRole["role-violation"]) freshnessClasses.push("role-violation");
  let freshnessNote = "waiting for the first canonical read";
  if (freshness) {
    const parts: string[] = [];
    if (freshness.latest_at) parts.push(`last write ${formatTimestamp(freshness.latest_at)}`);
    const staleSuffix = staleLaneSuffix(freshness).trim();
    if (staleSuffix) parts.push(staleSuffix);
    if (ceded.length) parts.push(`Ceded lane${ceded.length > 1 ? "s" : ""}: ${ceded.join(", ")}.`);
    freshnessNote = parts.join(" ") || "usage ledger heartbeat";
  }

  const totals = (unifiedData && unifiedData.totals) || null;
  const week = (totals && totals.list_price) || null;
  const spendText = week ? `$${week.estimate_usd || "0"} wk` : "—";
  const substituted = totals ? Number(totals.substituted_events || 0) : null;
  const substitutionNote = substituted === null
    ? "—"
    : substituted
      ? "served model differed from requested"
      : "no substitutions observed · 7 days";
  const substitutionClasses = ["digest-item"];
  if (substituted !== null && substituted > 0) substitutionClasses.push("role-violation");
  if (substituted !== null && !substituted) substitutionClasses.push("role-stability");

  return (
    <section className="title-panel ops-hero" aria-labelledby="aiUsageTitle">
      <div className="ops-hero-intro">
        <p className="eyebrow">Private provider-neutral accounting</p>
        <h1 id="aiUsageTitle">AI Usage</h1>
        <p className="subtitle">Seven-day canonical usage, subscription windows, and posted billing facts across configured AI harnesses and providers.</p>
        <div id="gcSwitcher" className="gc-switcher-mount" aria-label="Governed Cognition theme" />
      </div>
      <aside className="ops-digest" aria-label="Operational digest">
        <div className={freshnessClasses.join(" ")} id="digestFreshnessItem">
          <div className="micro-label">Ledger freshness</div>
          <strong className="digest-value" id="digestFreshness">{status ?? "—"}</strong>
          <span className="digest-note" id="digestFreshnessNote">{freshnessNote}</span>
        </div>
        <div className="digest-item">
          <div className="micro-label">Soonest reset</div>
          {/* No placeholder anchor: `.reset-chip { display: flex }` beat the
              UA's [hidden] rule and the empty chip drew as a blank pill. */}
          {soonestChip ? <ResetChip chip={soonestChip} id="digestReset" /> : null}
          <span className="digest-note" id="digestResetNote">
            {soonestChip
              ? "soonest quota window · links to its card"
              : subsUnavailable
                ? "subscription windows unavailable"
                : subsLoaded
                  ? "no exact resets reported"
                  : "waiting for subscription windows"}
          </span>
        </div>
        <div className="digest-item">
          <div className="micro-label">Est. spend · week</div>
          <strong className="digest-value" id="digestSpend">{spendText}</strong>
          <span className="digest-note">list-price estimate</span>
        </div>
        <div className={substitutionClasses.join(" ")} id="digestSubstitutionItem">
          <div className="micro-label">Substitution watch</div>
          <strong className="digest-value" id="digestSubstitutions">{substituted === null ? "—" : intFormat(substituted)}</strong>
          <span className="digest-note" id="digestSubstitutionsNote">{substitutionNote}</span>
        </div>
      </aside>
    </section>
  );
}
