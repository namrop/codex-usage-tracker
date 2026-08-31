import { useEffect } from "react";
import { applyBackgroundImage } from "../lib/background";
import { whenThemeReady } from "../lib/palette";

// The theme's background image is a resolved token like any other, so it
// re-resolves on exactly the moments the palette does: gc:apply (a selection
// was applied), gc:source (origin/fallback flipped, which MOVES the base the
// relative asset url resolves against), and the managed #gc-theme link's own
// load event — the guaranteed post-stylesheet moment, and the only one at
// which a freshly swapped theme's --gc-bg-image is readable.
export function usePageBackground(): void {
  useEffect(() => {
    let disposed = false;
    const refresh = () => {
      if (!disposed) applyBackgroundImage();
    };
    const gc = globalThis.GovernedCognition;
    const applyEvent = gc ? gc.applyEvent : "gc:apply";
    const sourceEvent = gc ? gc.sourceEvent : "gc:source";
    document.addEventListener(applyEvent, refresh);
    document.addEventListener(sourceEvent, refresh);
    const onCapturedLoad = (event: Event) => {
      const target = event.target as Element | null;
      if (target && target.id === "gc-theme") refresh();
    };
    document.addEventListener("load", onCapturedLoad, true);
    whenThemeReady().then(refresh);
    refresh();
    return () => {
      disposed = true;
      document.removeEventListener(applyEvent, refresh);
      document.removeEventListener(sourceEvent, refresh);
      document.removeEventListener("load", onCapturedLoad, true);
    };
  }, []);
}
