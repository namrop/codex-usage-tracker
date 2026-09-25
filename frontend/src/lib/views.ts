// Focused navigation (keeper authorization 2026-09-14): the one long page
// becomes four real views, routed by the URL hash so a reload or a shared
// link lands on the same view. Nothing here reads the DOM — App.tsx owns the
// hashchange listener and hands the parsed location down.
//
// Deep links that predate the views keep working: the reset chips and the
// hero's soonest-reset chip link to `#subscription-card-<provider>`, and
// those cards live in the Now view's collapsible provider detail, so such an
// anchor resolves to Now and carries the id along for the reveal.

export type ViewId = "now" | "history" | "accounting" | "diagnostics";

export interface ViewDefinition {
  id: ViewId;
  label: string;
  // One line, shown as the nav link's title: what the view answers.
  description: string;
}

export const VIEWS: readonly ViewDefinition[] = Object.freeze([
  { id: "now", label: "Now", description: "Capacity by account, burn, resets, provider health" },
  { id: "history", label: "History", description: "Throughput, model, cache and quota history charts and tables" },
  { id: "accounting", label: "Accounting", description: "API list-price equivalent, billing facts, rulings per dollar" },
  { id: "diagnostics", label: "Diagnostics", description: "Collection freshness, coverage and raw observation evidence" },
]);

export const DEFAULT_VIEW: ViewId = "now";

export interface ViewLocation {
  view: ViewId;
  // An element id to reveal inside the view (a provider card), or null.
  anchor: string | null;
}

const VIEW_IDS: ReadonlySet<string> = new Set(VIEWS.map((view) => view.id));

// Element-id prefixes that belong to a view. Only ids the app actually
// renders are listed; an unknown anchor falls back to the default view
// with no anchor rather than pretending it will be found.
const ANCHOR_PREFIXES: readonly [string, ViewId][] = Object.freeze([
  ["subscription-card-", "now"],
  ["capacity", "now"],
  ["burn", "now"],
  ["resetRail", "now"],
  ["providerStatus", "now"],
  // Weekly allowance use (2026-09-14): the Now strip links here; the panel
  // is the first card on History and carries id="weeklyUtilization".
  ["weeklyUtilization", "history"],
  // OpenCode Go usage explanation (2026-09-14): the small card at the end of
  // History since 2026-09-25 (UI review item 9), id="goUsage".
  ["goUsage", "history"],
  ["subscriptionUnified", "history"],
  ["modelToken", "history"],
  ["tokenComposition", "history"],
  ["cacheHitRate", "history"],
  ["fiveHourDonut", "history"],
  ["weeklyDonut", "history"],
  ["unified", "history"],
  ["listPrice", "accounting"],
  ["billing", "accounting"],
  ["rulings", "accounting"],
  ["diagnostics", "diagnostics"],
  ["subscriptionsRaw", "diagnostics"],
]);

export function viewForAnchor(anchor: string): ViewId | null {
  for (const [prefix, view] of ANCHOR_PREFIXES) {
    if (anchor.startsWith(prefix)) return view;
  }
  return null;
}

export function parseViewHash(hash: string | null | undefined): ViewLocation {
  const raw = String(hash || "").replace(/^#/, "").trim();
  if (!raw) return { view: DEFAULT_VIEW, anchor: null };
  let decoded = raw;
  try {
    decoded = decodeURIComponent(raw);
  } catch (error) {
    decoded = raw;
  }
  const lowered = decoded.toLowerCase();
  if (VIEW_IDS.has(lowered)) return { view: lowered as ViewId, anchor: null };
  const owner = viewForAnchor(decoded);
  if (owner) return { view: owner, anchor: decoded };
  return { view: DEFAULT_VIEW, anchor: null };
}

export function viewHref(view: ViewId): string {
  return `#${view}`;
}

export function isViewId(value: unknown): value is ViewId {
  return typeof value === "string" && VIEW_IDS.has(value);
}

// Same-document anchor navigation (second fix pass, 2026-09-14). The
// browser fires hashchange only when the hash CHANGES; a reset chip clicked
// a second time (same #subscription-card-<key>) navigates silently, so the
// fold that hides the card never learns it should reopen. App.tsx feeds a
// click's plain facts here and bumps the reveal itself when the hash
// repeats. Anything that is not a plain primary click on a same-document
// fragment link is the browser's business and returns null: modifier keys
// (new tab / window / download), other buttons, an event another handler
// already handled, a named target, a download link, or an href that leaves
// the document (other origin, path or query — those are real navigations).
export interface AnchorClickFacts {
  href: string | null | undefined;
  currentHref: string;
  button?: number;
  metaKey?: boolean;
  ctrlKey?: boolean;
  shiftKey?: boolean;
  altKey?: boolean;
  target?: string | null;
  download?: string | null;
  defaultPrevented?: boolean;
}

export interface SameDocumentAnchorNavigation {
  hash: string;
  location: ViewLocation;
  // The hash is what the document already shows: hashchange will not fire.
  sameHash: boolean;
}

export function sameDocumentAnchorNavigation(facts: AnchorClickFacts): SameDocumentAnchorNavigation | null {
  if (facts.defaultPrevented) return null;
  if ((facts.button ?? 0) !== 0) return null;
  if (facts.metaKey || facts.ctrlKey || facts.shiftKey || facts.altKey) return null;
  if (facts.download !== null && facts.download !== undefined) return null;
  const target = String(facts.target || "").trim().toLowerCase();
  if (target && target !== "_self") return null;
  const href = facts.href;
  if (typeof href !== "string" || !href) return null;
  let current: URL;
  let next: URL;
  try {
    current = new URL(facts.currentHref);
    next = new URL(href, current);
  } catch (error) {
    return null;
  }
  if (next.origin !== current.origin || next.pathname !== current.pathname || next.search !== current.search) return null;
  if (!next.hash || next.hash === "#") return null;
  return {
    hash: next.hash,
    location: parseViewHash(next.hash),
    sameHash: next.hash === current.hash,
  };
}
