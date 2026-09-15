import type { DashboardState } from "../hooks/useDashboardData";
import type { Palette } from "../lib/palette";
import type { QuotaRow } from "../lib/quota";
import type { ResetChipModel } from "../components/ResetRail";
import { SubscriptionHistoryCharts } from "../components/SubscriptionsCard";
import { UnifiedUsageCard } from "../components/UnifiedUsageCard";

// History: every chart and table the long page carried — seven-day
// throughput donuts, tokens by model, token composition, cache hit rate,
// the harness and provider/model folds, and the two unified quota
// utilization charts with their fold tables. Nothing was dropped; it just
// no longer sits ahead of the capacity summary.

export function HistoryView({ state, palette, quotaLatest, resetChips }: {
  state: DashboardState;
  palette: Palette;
  quotaLatest: QuotaRow[] | undefined;
  resetChips: ResetChipModel[];
}) {
  return (
    <>
      <UnifiedUsageCard
        palette={palette}
        unified={state.unified}
        freshness={state.freshness}
        donutFive={state.donutFive}
        quotaLatest={quotaLatest}
        resetChips={resetChips}
      />
      <SubscriptionHistoryCharts palette={palette} subs={state.subs} />
    </>
  );
}
