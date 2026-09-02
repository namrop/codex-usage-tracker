import { useCallback, useEffect, useRef, useState } from "react";
import { formatTimestamp } from "../lib/format";
import { staleLaneSuffix, type Freshness } from "../lib/freshness";
import { whenThemeReady } from "../lib/palette";

// One hydrate cycle = the four private-panel loaders (unified usage,
// subscriptions, billing, provider status feeds), exactly as the pre-React
// inline script ran them. Each loader owns its panel's failure text; the
// refresh button state only tracks the cycle. Refresh runs every 60 seconds
// while the tab is visible, and again whenever the tab becomes visible.

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

export interface BillingPanelState {
  data: any | null;
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
          message: "No unified usage events in the last 168 complete hours.",
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
