// Deep-link reveal (second fix pass, 2026-09-14). A reset chip's link names
// a provider card that lives inside a collapsed fold, under a subscriptions
// loader that may still be in flight. Scrolling to and focusing that card
// therefore has to wait: for the card to exist, for the fold to have opened
// (a display:none element neither scrolls nor takes focus — browser QA saw
// focus stay on <body>), and for focus to actually land. This loop asks
// once per animation frame, gives up after a bounded number of frames, and
// is written against a tiny candidate interface so Node can test it
// without a DOM.

export interface RevealCandidate {
  // Rendered and laid out (not inside a hidden ancestor or a closed fold).
  revealed: boolean;
  scrollIntoView(): void;
  focus(): void;
  // Whether focus is on the candidate now.
  focused(): boolean;
}

export interface FrameScheduler {
  frame(callback: () => void): number;
  cancel(id: number): void;
}

// Enough frames to cover a fold opening plus a hydrate settling; not so
// many that a target that never appears keeps a loop alive.
export const REVEAL_MAX_ATTEMPTS = 30;

export function scheduleReveal(
  find: () => RevealCandidate | null,
  scheduler: FrameScheduler,
  maxAttempts: number = REVEAL_MAX_ATTEMPTS,
): () => void {
  let attempts = 0;
  let pending: number | null = null;
  let cancelled = false;
  const step = () => {
    pending = null;
    if (cancelled) return;
    attempts += 1;
    const candidate = find();
    if (candidate && candidate.revealed) {
      candidate.scrollIntoView();
      candidate.focus();
      if (candidate.focused()) return;
    }
    if (attempts >= maxAttempts) return;
    pending = scheduler.frame(step);
  };
  pending = scheduler.frame(step);
  return () => {
    cancelled = true;
    if (pending !== null) {
      scheduler.cancel(pending);
      pending = null;
    }
  };
}

// The DOM adapter: null until the element exists; revealed once it has a
// box (a closed fold's body is display:none, so its children have none).
export function domRevealCandidate(doc: Document, id: string): RevealCandidate | null {
  const element = doc.getElementById(id);
  if (!element) return null;
  return {
    get revealed() {
      if (element.closest("[hidden]")) return false;
      return element.getClientRects().length > 0;
    },
    scrollIntoView: () => element.scrollIntoView({ block: "start" }),
    focus: () => {
      if (typeof element.focus === "function") element.focus({ preventScroll: true });
    },
    focused: () => doc.activeElement === element,
  };
}
