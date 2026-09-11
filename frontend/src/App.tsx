import { useEffect, useMemo } from "react";
import { useDashboardData } from "./hooks/useDashboardData";
import { useGlass } from "./hooks/useGlass";
import { usePageBackground } from "./hooks/usePageBackground";
import { usePalette } from "./hooks/usePalette";
import { BillingCard } from "./components/BillingCard";
import { BurnRateCard } from "./components/BurnRateCard";
import { RulingsPerDollarCard } from "./components/RulingsPerDollarCard";
import { GlassPanels } from "./components/GlassPanels";
import { OpsHero } from "./components/OpsHero";
import { deriveResetChips } from "./components/ResetRail";
import { SubscriptionsCard, subscriptionThresholdSeconds } from "./components/SubscriptionsCard";
import { UnifiedUsageCard } from "./components/UnifiedUsageCard";

// The shared estate picker: a gear that expands. mountSwitcher fetches it
// (css + js) through the same origin -> snapshot hops, so this page always
// gets whatever GC ships rather than a copy frozen at vendor time.
// Optional chrome — theming works if it never arrives.
let switcherMounted = false;
function mountSwitcher(): void {
  if (switcherMounted) return;
  const gc = globalThis.GovernedCognition;
  if (!gc) return;
  switcherMounted = true;
  gc.mountSwitcher("#gcSwitcher").catch(() => {});
}

export default function App() {
  const palette = usePalette();
  // GC owns the glass preference; <GlassPanels> draws it on the shell's
  // main-panel class (keeper-ruled: a class, not one showpiece surface).
  // Nothing here reads .glass() — glassEffective() is the mount gate, so a
  // reader asking for reduced transparency outranks the toggle.
  const glass = useGlass();
  // Republishes the theme's background asset as an absolute url — see
  // lib/background.ts for why the CSS token cannot be consumed directly.
  usePageBackground();
  const { state, refresh } = useDashboardData();

  useEffect(() => {
    mountSwitcher();
  }, []);

  // Cross-loader joins: whichever loader finishes last re-renders with
  // complete data (donut legends want quota rows from the subscriptions
  // loader; the rail is quota-only) — in React that join is just shared
  // state.
  const quotaLatest = state.subs.data && Array.isArray(state.subs.data.latest)
    ? state.subs.data.latest
    : undefined;
  const subsFreshness = state.subs.data ? state.subs.data.freshness : null;
  const resetChips = useMemo(
    () => deriveResetChips(quotaLatest || [], {
      now: Date.now(),
      maxAgeSeconds: subscriptionThresholdSeconds(subsFreshness || {}),
    }),
    [quotaLatest, subsFreshness],
  );

  return (
    <main className="shell">
      <OpsHero
        freshness={state.freshness}
        soonestChip={resetChips[0]}
        subsLoaded={!state.subs.loading && !state.subs.placeholder}
        subsUnavailable={!state.subs.loading && state.subs.placeholder}
        unifiedData={state.unified.data}
      />
      <UnifiedUsageCard
        palette={palette}
        unified={state.unified}
        freshness={state.freshness}
        donutFive={state.donutFive}
        listPriceDay={state.listPriceDay}
        quotaLatest={quotaLatest}
        resetChips={resetChips}
        subsLoading={state.subs.loading}
        subsUnavailable={!state.subs.loading && state.subs.placeholder}
        providerStatusFeeds={state.providerStatusFeeds}
        refreshing={state.refreshing}
        onRefresh={refresh}
      />
      <SubscriptionsCard palette={palette} subs={state.subs} />
      <BurnRateCard burn={state.burn} />
      <BillingCard billing={state.billing} />
      <RulingsPerDollarCard rulings={state.rulings} />
      <GlassPanels glass={glass} />
    </main>
  );
}
