import type { DashboardState } from "../hooks/useDashboardData";
import { BillingCard } from "../components/BillingCard";
import { ListPriceHeadline } from "../components/ListPriceHeadline";
import { RulingsPerDollarCard } from "../components/RulingsPerDollarCard";

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
      <BillingCard billing={state.billing} />
      <RulingsPerDollarCard rulings={state.rulings} />
    </>
  );
}
