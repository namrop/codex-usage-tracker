import { useEffect, useState } from "react";
import { resolvePalette, whenThemeReady, type Palette } from "../lib/palette";

// Runtime theme swap (GC switcher): the loader dispatches gc:apply when a
// selection is applied and gc:source when the origin/fallback resolution
// changes; the managed #gc-theme link's load event is the guaranteed
// post-stylesheet moment. All three re-resolve the palette so Chart.js and
// the SVG surfaces re-render from the new tokens (re-read + re-render only —
// a theme swap never refetches data).
//
// gc:glass joins them (2026-08-30): toggling glass attaches or drops
// glass.css, which is a token layer like any other, and it flips
// html[data-gc-glass] — anything the style layer resolves off that
// attribute moves under the charts' feet. The charts read colors at render
// time, so the cheap correct answer is to re-read on that event too rather
// than reason about which tokens the toggle can and cannot reach.
export function usePalette(): Palette {
  const [palette, setPalette] = useState<Palette>(() => resolvePalette());

  useEffect(() => {
    let disposed = false;
    const refresh = () => {
      if (!disposed) setPalette(resolvePalette());
    };
    const gc = globalThis.GovernedCognition;
    const applyEvent = gc ? gc.applyEvent : "gc:apply";
    const sourceEvent = gc ? gc.sourceEvent : "gc:source";
    const glassEvent = gc ? gc.glassEvent : "gc:glass";
    document.addEventListener(applyEvent, refresh);
    document.addEventListener(sourceEvent, refresh);
    document.addEventListener(glassEvent, refresh);
    // gc:apply fires synchronously (possibly before the sheet finishes
    // loading), so also catch the #gc-theme link's load. Capture-phase on
    // document because load does not bubble and the loader may (re)create
    // the link at any time.
    const onCapturedLoad = (event: Event) => {
      const target = event.target as Element | null;
      if (target && target.id === "gc-theme") refresh();
    };
    document.addEventListener("load", onCapturedLoad, true);
    whenThemeReady().then(refresh);
    return () => {
      disposed = true;
      document.removeEventListener(applyEvent, refresh);
      document.removeEventListener(sourceEvent, refresh);
      document.removeEventListener(glassEvent, refresh);
      document.removeEventListener("load", onCapturedLoad, true);
    };
  }, []);

  return palette;
}
