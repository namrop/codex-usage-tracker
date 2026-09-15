import { useEffect, useMemo, useState } from "react";
import { useDashboardData } from "./hooks/useDashboardData";
import { useGlass } from "./hooks/useGlass";
import { usePageBackground } from "./hooks/usePageBackground";
import { usePalette } from "./hooks/usePalette";
import { GlassPanels } from "./components/GlassPanels";
import { OpsHero } from "./components/OpsHero";
import { deriveResetChips } from "./components/ResetRail";
import { subscriptionThresholdSeconds } from "./components/SubscriptionsCard";
import { ViewNav } from "./components/ViewNav";
import { domRevealCandidate, scheduleReveal } from "./lib/reveal";
import { parseViewHash, sameDocumentAnchorNavigation, type ViewLocation } from "./lib/views";
import { AccountingView } from "./views/AccountingView";
import { DiagnosticsView } from "./views/DiagnosticsView";
import { HistoryView } from "./views/HistoryView";
import { NowView } from "./views/NowView";

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

// Hash-routed views (2026-09-14). The hash is the whole view state, so
// reload, history and copied links land where the reader was; the data
// hooks live here above the views, so switching a view never refetches and
// last-good data, refresh cadence and per-panel failure notices are shared
// by all four. Inactive views UNMOUNT rather than hide: a Chart.js canvas
// built inside display:none is 0×0 and stays blank, so a view's charts are
// built fresh, at their real size, when the view becomes active.
function useViewLocation(): { location: ViewLocation; revealToken: number } {
  const [location, setLocation] = useState<ViewLocation>(() => parseViewHash(window.location.hash));
  // Bumped on every anchor navigation (a reset chip's #subscription-card-*
  // link), so the fold that hides the card opens and the card scrolls into
  // view even when the hash did not change. hashchange covers a changed
  // hash; a repeated click on the SAME link navigates without any event
  // (browser QA 2026-09-14: the fold stayed shut), so plain primary clicks
  // on same-document fragment links are routed here too — the browser's
  // default (history, modifiers, other targets, cross-view links) is never
  // prevented, and no fake hash or query is written to force a change.
  const [revealToken, setRevealToken] = useState(() => (parseViewHash(window.location.hash).anchor ? 1 : 0));
  useEffect(() => {
    const onHashChange = () => {
      const next = parseViewHash(window.location.hash);
      setLocation(next);
      if (next.anchor) setRevealToken((token) => token + 1);
    };
    const onClick = (event: MouseEvent) => {
      const origin = event.target instanceof Element ? event.target.closest("a[href]") : null;
      if (!(origin instanceof HTMLAnchorElement)) return;
      const navigation = sameDocumentAnchorNavigation({
        href: origin.getAttribute("href"),
        currentHref: window.location.href,
        button: event.button,
        metaKey: event.metaKey,
        ctrlKey: event.ctrlKey,
        shiftKey: event.shiftKey,
        altKey: event.altKey,
        target: origin.getAttribute("target"),
        download: origin.getAttribute("download"),
        defaultPrevented: event.defaultPrevented,
      });
      // A changed hash reaches onHashChange; only the silent repeat of an
      // anchor needs help.
      if (!navigation || !navigation.sameHash || !navigation.location.anchor) return;
      setLocation(navigation.location);
      setRevealToken((token) => token + 1);
    };
    window.addEventListener("hashchange", onHashChange);
    document.addEventListener("click", onClick);
    return () => {
      window.removeEventListener("hashchange", onHashChange);
      document.removeEventListener("click", onClick);
    };
  }, []);
  return { location, revealToken };
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
  const { location, revealToken } = useViewLocation();
  const view = location.view;

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
  const thresholdSeconds = subscriptionThresholdSeconds(subsFreshness || {});
  const resetChips = useMemo(
    () => deriveResetChips(quotaLatest || [], {
      now: Date.now(),
      maxAgeSeconds: thresholdSeconds,
    }),
    [quotaLatest, thresholdSeconds],
  );

  // Reveal a deep-linked card once its view and fold have rendered. The
  // subscriptions loader may still be in flight on a cold deep link, so
  // this re-runs when it settles. scheduleReveal (lib/reveal.ts, Node-
  // tested) waits frame by frame for the card to exist and have a box —
  // the fold's reveal effect opens it on the commit after the token bump —
  // then scrolls to it and focuses it (the cards carry tabIndex=-1),
  // retrying until focus has actually landed or a bounded budget runs out.
  const subsLoaded = !state.subs.loading;
  useEffect(() => {
    const anchor = location.anchor;
    if (!anchor || !revealToken) return;
    return scheduleReveal(
      () => domRevealCandidate(document, anchor),
      {
        frame: (callback) => window.requestAnimationFrame(callback),
        cancel: (id) => window.cancelAnimationFrame(id),
      },
    );
  }, [location.anchor, revealToken, subsLoaded]);

  return (
    <main className="shell" data-view={view}>
      <OpsHero
        freshness={state.freshness}
        soonestChip={resetChips[0]}
        subsLoaded={!state.subs.loading && !state.subs.placeholder}
        subsUnavailable={!state.subs.loading && state.subs.placeholder}
        unifiedData={state.unified.data}
      />
      <ViewNav current={view} refreshing={state.refreshing} onRefresh={refresh} />
      {view === "now" && (
        <NowView
          state={state}
          palette={palette}
          resetChips={resetChips}
          thresholdSeconds={thresholdSeconds}
          revealToken={revealToken}
        />
      )}
      {view === "history" && (
        <HistoryView state={state} palette={palette} quotaLatest={quotaLatest} resetChips={resetChips} />
      )}
      {view === "accounting" && <AccountingView state={state} />}
      {view === "diagnostics" && <DiagnosticsView state={state} />}
      <GlassPanels glass={glass} />
    </main>
  );
}
