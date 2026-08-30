import { useEffect, useState } from "react";
import { resolvePalette, whenThemeReady, type Palette } from "../lib/palette";

// Runtime theme swap (GC switcher): the loader dispatches gc:apply when a
// selection is applied and gc:source when the origin/fallback resolution
// changes; the managed #gc-theme link's load event is the guaranteed
// post-stylesheet moment. All three re-resolve the palette so Chart.js and
// the SVG surfaces re-render from the new tokens (re-read + re-render only —
// a theme swap never refetches data).
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
    document.addEventListener(applyEvent, refresh);
    document.addEventListener(sourceEvent, refresh);
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
      document.removeEventListener("load", onCapturedLoad, true);
    };
  }, []);

  return palette;
}
