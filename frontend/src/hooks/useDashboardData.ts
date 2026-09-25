import { useCallback, useEffect, useRef, useState } from "react";
import { formatTimestamp } from "../lib/format";
import { staleLaneSuffix, type Freshness } from "../lib/freshness";
import { whenThemeReady } from "../lib/palette";
import { isGoUsagePayload, type GoUsagePayload } from "../lib/goUsage";
import { isWeeklyUtilizationPayload, type WeeklyUtilizationPayload } from "../lib/weeklyUtilization";

// One hydrate cycle = the private-panel loaders (unified usage,
// subscriptions, billing, provider status feeds, weekly allowance use),
// exactly as the pre-React inline script ran them. Each loader owns its
// panel's failure text; the refresh button state only tracks the cycle.
// Refresh runs every 60 seconds while the tab is visible, and again
// whenever the tab becomes visible.

export interface UnifiedPanelState {
  data: any | null;
  message: string | null;
  contentVisible: boolean;
}

export interface SubsPanelState {
  data: any | null;
  notice: string | null;
  placeholder: boolean;
  loading: boolean;
}

export interface BurnPanelState {
  data: any | null;
  message: string | null;
  contentVisible: boolean;
}

export interface RulingsPanelState {
  data: any | null;
  message: string | null;
  contentVisible: boolean;
}

export interface BillingPanelState {
  data: any | null;
  message: string | null;
  contentVisible: boolean;
}

export interface WeeklyPanelState {
  data: WeeklyUtilizationPayload | null;
  message: string | null;
  contentVisible: boolean;
}

export interface SubscriptionFeesPanelState {
  data: any | null;
  message: string | null;
  contentVisible: boolean;
}

export interface GoUsagePanelState {
  data: GoUsagePayload | null;
  message: string | null;
  contentVisible: boolean;
}

export interface DashboardState {
  unified: UnifiedPanelState;
  // The digest mirrors freshness even when the panel falls back to a
  // stale/empty notice — "does anything need me?" must not blank.
  freshness: Freshness | null;
  donutFive: any | null;
  listPriceDay: any | null;
  subs: SubsPanelState;
  burn: BurnPanelState;
  billing: BillingPanelState;
  rulings: RulingsPanelState;
  weekly: WeeklyPanelState;
  goUsage: GoUsagePanelState;
  // What each subscription costs (keeper msg 1553037883747598450, 2026-09-25).
  subscriptionFees: SubscriptionFeesPanelState;
  providerStatusFeeds: Record<string, any> | null;
  refreshing: boolean;
}

const INITIAL_STATE: DashboardState = {
  unified: { data: null, message: "Loading unified usage…", contentVisible: false },
  freshness: null,
  donutFive: null,
  listPriceDay: null,
  subs: { data: null, notice: null, placeholder: false, loading: true },
  burn: { data: null, message: "Loading window burn rate…", contentVisible: false },
  billing: { data: null, message: "Loading billing facts…", contentVisible: false },
  rulings: { data: null, message: "Loading rulings per dollar…", contentVisible: false },
  weekly: { data: null, message: "Loading weekly allowance use…", contentVisible: false },
  goUsage: { data: null, message: "Loading OpenCode Go usage…", contentVisible: false },
  subscriptionFees: { data: null, message: "Loading subscription fees…", contentVisible: false },
  providerStatusFeeds: null,
  refreshing: false,
};

type Set = React.Dispatch<React.SetStateAction<DashboardState>>;

async function loadUnifiedUsage(set: Set): Promise<void> {
  try {
    const response = await fetch("/api/unified-usage?hours=168", { signal: AbortSignal.timeout(30000) });
    if (!response.ok) throw new Error("unavailable");
    const data = await response.json();
    const freshness: Freshness = data.freshness || {};
    if (freshness.status === "stale") {
      set((prev) => ({
        ...prev,
        freshness,
        unified: {
          ...prev.unified,
          message: `Canonical usage is stale · last ledger write ${formatTimestamp(freshness.latest_at)}.${staleLaneSuffix(freshness)}`,
          contentVisible: false,
        },
      }));
      return;
    }
    if (freshness.status === "empty") {
      set((prev) => ({
        ...prev,
        freshness,
        unified: { ...prev.unified, message: "Canonical usage is unavailable.", contentVisible: false },
      }));
      return;
    }
    const events = Number((data.window && data.window.event_count) || 0);
    if (!events) {
      set((prev) => ({
        ...prev,
        freshness,
        unified: {
          ...prev.unified,
          message: "No unified usage events in the last 168 hours.",
          contentVisible: false,
        },
      }));
      return;
    }
    set((prev) => ({
      ...prev,
      freshness,
      unified: { data, message: null, contentVisible: true },
    }));
    try {
      const fiveResponse = await fetch("/api/unified-usage?hours=5", { signal: AbortSignal.timeout(30000) });
      if (fiveResponse.ok) {
        const donutFive = await fiveResponse.json();
        set((prev) => ({ ...prev, donutFive }));
      }
    } catch (fiveError) {
      // last-good law: an earlier five-hour donut stays on a failed refresh.
    }
    try {
      const dayResponse = await fetch("/api/unified-usage?hours=24", { signal: AbortSignal.timeout(30000) });
      if (dayResponse.ok) {
        const listPriceDay = (await dayResponse.json()).totals.list_price;
        set((prev) => ({ ...prev, listPriceDay }));
      }
    } catch (dayError) {
      // ditto.
    }
  } catch (error) {
    set((prev) => {
      if (prev.unified.contentVisible) {
        return {
          ...prev,
          unified: { ...prev.unified, message: "Refresh failed — showing last good data." },
        };
      }
      return {
        ...prev,
        unified: { ...prev.unified, message: "Unified usage unavailable.", contentVisible: false },
      };
    });
  }
}

async function loadSubscriptions(set: Set): Promise<void> {
  try {
    const response = await fetch("/api/subscriptions?hours=168&history=0", { signal: AbortSignal.timeout(30000) });
    if (!response.ok) throw new Error("unavailable");
    const data = await response.json();
    const freshness: Freshness = data.freshness || {};
    let ledgerNotice = "";
    if (freshness.status === "stale") {
      ledgerNotice = `Subscription observations are stale · last ledger write ${formatTimestamp(freshness.latest_at)}.${staleLaneSuffix(freshness)} Provider observations remain visible below.`;
    }
    if (freshness.status === "empty") {
      ledgerNotice = "Subscription ledger freshness is unavailable. Provider observations remain visible when present.";
    }
    const rows = Array.isArray(data.latest) ? data.latest : [];
    if (!rows.length && !ledgerNotice) {
      ledgerNotice = "No subscription observations are currently recorded. Expected providers remain visible below.";
    }
    set((prev) => ({
      ...prev,
      subs: { data, notice: ledgerNotice || null, placeholder: false, loading: false },
    }));
  } catch (error) {
    set((prev) => {
      if (prev.subs.data) {
        return {
          ...prev,
          subs: { ...prev.subs, notice: "Refresh failed — showing last good data.", loading: false },
        };
      }
      return {
        ...prev,
        subs: { data: null, notice: "Subscription data unavailable.", placeholder: true, loading: false },
      };
    });
  }
}

async function loadBurn(set: Set): Promise<void> {
  try {
    const response = await fetch("/api/quota-burn?hours=6", { signal: AbortSignal.timeout(30000) });
    if (!response.ok) throw new Error("unavailable");
    const data = await response.json();
    const series = Array.isArray(data.series) ? data.series : [];
    if (!series.length) {
      set((prev) => ({
        ...prev,
        burn: { data: null, message: "No actively-sampled windows in the last 6 hours.", contentVisible: false },
      }));
      return;
    }
    set((prev) => ({
      ...prev,
      burn: { data, message: null, contentVisible: true },
    }));
  } catch (error) {
    set((prev) => {
      if (prev.burn.contentVisible) {
        return { ...prev, burn: { ...prev.burn, message: "Refresh failed — showing last good data." } };
      }
      return { ...prev, burn: { data: null, message: "Window burn rate unavailable.", contentVisible: false } };
    });
  }
}

async function loadSubscriptionFees(set: Set): Promise<void> {
  try {
    const response = await fetch("/api/subscription-fees", { signal: AbortSignal.timeout(30000) });
    if (!response.ok) throw new Error("unavailable");
    const data = await response.json();
    const subscriptions = Array.isArray(data.subscriptions) ? data.subscriptions : [];
    if (!subscriptions.length) {
      set((prev) => ({
        ...prev,
        subscriptionFees: { data, message: "No subscription charges recorded yet.", contentVisible: false },
      }));
      return;
    }
    set((prev) => ({ ...prev, subscriptionFees: { data, message: null, contentVisible: true } }));
  } catch (error) {
    set((prev) => (prev.subscriptionFees.contentVisible
      ? { ...prev, subscriptionFees: { ...prev.subscriptionFees, message: "Refresh failed — showing last good data." } }
      : { ...prev, subscriptionFees: { data: null, message: "Subscription fees unavailable.", contentVisible: false } }));
  }
}

async function loadBilling(set: Set): Promise<void> {
  try {
    const response = await fetch("/api/billing?days=30", { signal: AbortSignal.timeout(30000) });
    if (!response.ok) throw new Error("unavailable");
    const data = await response.json();
    const rows = Array.isArray(data.transactions) ? data.transactions : [];
    if (!rows.length) {
      set((prev) => ({
        ...prev,
        billing: { ...prev.billing, message: "No billing transactions in the last 30 days.", contentVisible: false },
      }));
      return;
    }
    set((prev) => ({ ...prev, billing: { data, message: null, contentVisible: true } }));
  } catch (error) {
    set((prev) => {
      if (prev.billing.contentVisible) {
        return {
          ...prev,
          billing: { ...prev.billing, message: "Refresh failed — showing last good data." },
        };
      }
      return {
        ...prev,
        billing: { ...prev.billing, message: "Billing data unavailable.", contentVisible: false },
      };
    });
  }
}

async function loadRulings(set: Set): Promise<void> {
  // Rulings per dollar. The priced join needs a canonical usage ledger; a
  // dashboard configured without one still gets the counts from /api/rulings
  // rather than an empty panel, because the numerator is a fact on its own.
  try {
    // The priced join loads every canonical usage row in the window, and the
    // private API refuses more than 100k rows with a 413 (seen live
    // 2026-09-11: eight weeks was past the limit the day the endpoint went
    // into service, and the panel read "unavailable" over a working ledger).
    // Step down to the widest window that fits before giving up the dollars.
    let response: Response | null = null;
    for (const weeks of [8, 4, 2]) {
      response = await fetch(`/api/rulings-per-dollar?weeks=${weeks}`, { signal: AbortSignal.timeout(30000) });
      if (response.status !== 413) break;
    }
    if (!response || response.status === 404 || response.status === 413) {
      response = await fetch("/api/rulings?weeks=8", { signal: AbortSignal.timeout(30000) });
    }
    if (!response.ok) throw new Error("unavailable");
    const data = await response.json();
    set((prev) => ({ ...prev, rulings: { data, message: null, contentVisible: true } }));
  } catch (error) {
    set((prev) => {
      if (prev.rulings.contentVisible) {
        return { ...prev, rulings: { ...prev.rulings, message: "Refresh failed — showing last good data." } };
      }
      return {
        ...prev,
        rulings: { ...prev.rulings, message: "Ruling ledger unavailable.", contentVisible: false },
      };
    });
  }
}

async function loadWeeklyUtilization(set: Set): Promise<void> {
  // Weekly allowance use (shared contract 2026-09-14). One bounded read at
  // the default four completed windows; the panel and the Now strip share
  // this slot so switching views never refetches. A 404 is the backend's
  // "safe missing/unconfigured source" answer and reads as such — never as
  // a zero; a malformed body fails the same way a bad status does.
  try {
    const response = await fetch("/api/weekly-utilization?weeks=4", { signal: AbortSignal.timeout(30000) });
    if (response.status === 404) {
      set((prev) => ({
        ...prev,
        weekly: { data: null, message: "Weekly allowance use is not configured on this dashboard.", contentVisible: false },
      }));
      return;
    }
    if (!response.ok) throw new Error("unavailable");
    const data = await response.json();
    if (!isWeeklyUtilizationPayload(data)) throw new Error("malformed");
    set((prev) => ({ ...prev, weekly: { data, message: null, contentVisible: true } }));
  } catch (error) {
    set((prev) => {
      if (prev.weekly.contentVisible) {
        return { ...prev, weekly: { ...prev.weekly, message: "Refresh failed — showing last good data." } };
      }
      return { ...prev, weekly: { data: null, message: "Weekly allowance use unavailable.", contentVisible: false } };
    });
  }
}

async function loadGoUsage(set: Set): Promise<void> {
  // OpenCode Go usage explanation (contract 2026-09-14, docs/go-native-
  // usage-explanation.md). One bounded read of the current UTC week; the
  // History card owns this slot. A 404 is the backend's "route not wired /
  // source not configured" answer and reads as such — never as a zero
  // native percent; a malformed body fails the same way a bad status does.
  try {
    const response = await fetch("/api/opencode-go-usage", { signal: AbortSignal.timeout(30000) });
    if (response.status === 404) {
      set((prev) => ({
        ...prev,
        goUsage: { data: null, message: "OpenCode Go usage explanation is not configured on this dashboard.", contentVisible: false },
      }));
      return;
    }
    if (!response.ok) throw new Error("unavailable");
    const data = await response.json();
    if (!isGoUsagePayload(data)) throw new Error("malformed");
    set((prev) => ({ ...prev, goUsage: { data, message: null, contentVisible: true } }));
  } catch (error) {
    set((prev) => {
      if (prev.goUsage.contentVisible) {
        return { ...prev, goUsage: { ...prev.goUsage, message: "Refresh failed — showing last good data." } };
      }
      return { ...prev, goUsage: { data: null, message: "OpenCode Go usage explanation unavailable.", contentVisible: false } };
    });
  }
}

async function loadProviderStatusFeeds(set: Set): Promise<void> {
  try {
    const response = await fetch("/api/provider-status", { signal: AbortSignal.timeout(15000) });
    if (!response.ok) return;
    const data = await response.json();
    set((prev) => ({ ...prev, providerStatusFeeds: data.providers || {} }));
  } catch (feedError) {
    // last-good law: a stale badge beats a vanished one.
  }
}

export function useDashboardData(): { state: DashboardState; refresh: () => void } {
  const [state, setState] = useState<DashboardState>(INITIAL_STATE);
  const inFlight = useRef(false);
  const booted = useRef(false);

  const hydrate = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setState((prev) => ({ ...prev, refreshing: true }));
    try {
      await Promise.allSettled([
        loadUnifiedUsage(setState),
        loadSubscriptions(setState),
        loadBurn(setState),
        loadBilling(setState),
        loadSubscriptionFees(setState),
        loadRulings(setState),
        loadWeeklyUtilization(setState),
        loadGoUsage(setState),
        loadProviderStatusFeeds(setState),
      ]);
    } finally {
      inFlight.current = false;
      setState((prev) => ({ ...prev, refreshing: false }));
    }
  }, []);

  useEffect(() => {
    let disposed = false;
    // The GC token layer must be resolved before the first data render;
    // whenThemeReady has a deadline so a pathological outage still renders.
    whenThemeReady().then(() => {
      if (disposed) return;
      booted.current = true;
      hydrate();
    });
    const interval = setInterval(() => {
      if (!document.hidden && booted.current) hydrate();
    }, 60000);
    const onVisibility = () => {
      if (!document.hidden && booted.current) hydrate();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      disposed = true;
      clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [hydrate]);

  return { state, refresh: hydrate };
}
