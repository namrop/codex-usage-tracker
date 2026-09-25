import type { DashboardState } from "../hooks/useDashboardData";
import type { Palette } from "../lib/palette";
import { BurnRateCard } from "../components/BurnRateCard";
import { CapacityCard } from "../components/CapacityCard";
import { ProviderStatus } from "../components/ProviderStatus";
import { ResetRail, type ResetChipModel } from "../components/ResetRail";
import { SubscriptionProviderDetail } from "../components/SubscriptionsCard";
import { WeeklyUtilizationSummary } from "../components/WeeklyUtilizationCard";

// Now (the default view): what can I use right now, how fast is it going,
// when does it come back, and is the provider up — in that order, with the
// per-provider subscription detail collapsed beneath. The long quota history
// charts live on History. Views render as fragments so every card stays a
// direct child of the shell (the glass class and its observer key on that).

export function NowView({ state, palette, resetChips, thresholdSeconds, revealToken, hiddenFiveHour }: {
  state: DashboardState;
  palette: Palette;
  resetChips: ResetChipModel[];
  thresholdSeconds: number;
  revealToken: number;
  hiddenFiveHour?: ReadonlyMap<string, string>;
}) {
  const unifiedData = state.unified.data;
  const timeSeries = unifiedData ? unifiedData.time_series : null;
  return (
    <>
      <CapacityCard subs={state.subs} burn={state.burn} thresholdSeconds={thresholdSeconds} />
      <BurnRateCard burn={state.burn} hiddenFiveHour={hiddenFiveHour} />
      <WeeklyUtilizationSummary weekly={state.weekly} />
      <section className="card" aria-labelledby="nowHealthTitle">
        <div className="card-header">
          <div className="card-title">
            <h2 id="nowHealthTitle">Resets and provider health</h2>
            <p className="caption">Exact reset countdowns soonest first, and each provider’s public status feed over observed per-UTC-hour call health.</p>
          </div>
        </div>
        <div className="window-pair">
          <section className="window-card" aria-labelledby="resetRailTitle">
            <h3 id="resetRailTitle">Reset countdowns</h3>
            <p className="caption">Every provider quota window that reports an exact reset time, soonest first; each chip links to its provider card below.</p>
            <ResetRail
              chips={resetChips}
              loading={state.subs.loading}
              unavailable={!state.subs.loading && state.subs.placeholder}
            />
          </section>
          <section className="window-card" aria-labelledby="providerStatusTitle">
            <h3 id="providerStatusTitle">Provider API status · 48 hours</h3>
            <p className="caption">Provider status-feed badges over observed per-UTC-hour call health, where a dim cell means no traffic rather than a clean hour.</p>
            <ProviderStatus
              timeSeries={timeSeries}
              feeds={state.providerStatusFeeds}
              palette={palette}
              loading={!unifiedData}
            />
            <ul className="status-key" aria-hidden="true">
              <li><span className="status-cell state-ok" />all ok</li>
              <li><span className="status-cell state-degraded" />some errors</li>
              <li><span className="status-cell state-failing" />mostly errors</li>
              <li><span className="status-cell state-idle" />no traffic</li>
            </ul>
          </section>
        </div>
      </section>
      <SubscriptionProviderDetail subs={state.subs} revealToken={revealToken} hiddenFiveHour={hiddenFiveHour} />
    </>
  );
}
