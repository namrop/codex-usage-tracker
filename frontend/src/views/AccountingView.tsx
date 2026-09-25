import type { DashboardState } from "../hooks/useDashboardData";
import { BillingCard } from "../components/BillingCard";
import { ListPriceHeadline } from "../components/ListPriceHeadline";
import { RulingsPerDollarCard } from "../components/RulingsPerDollarCard";
import { SearchesCard } from "../components/SearchesCard";
import { SubscriptionValueCard } from "../components/SubscriptionValueCard";

// Accounting: the API list-price equivalent headline (a comparison figure,
// never a bill), the posted billing facts (the only actual charges), and
// rulings per dollar of list-price equivalent. Three record classes, each
// labelled as what it is.

export function AccountingView({ state }: { state: DashboardState }) {
  return (
    <>
      <ListPriceHeadline
        unifiedData={state.unified.data}
        unifiedMessage={state.unified.message}
        listPriceDay={state.listPriceDay}
        donutFive={state.donutFive}
      />
      <SubscriptionValueCard fees={state.subscriptionFees} unified={state.unified} />
      <SearchesCard searches={state.searches} />
      <BillingCard billing={state.billing} />
      <RulingsPerDollarCard rulings={state.rulings} />
    </>
  );
}
