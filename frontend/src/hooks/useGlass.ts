import { useEffect, useState } from "react";
import {
  glassEffective,
  isLightScheme,
  NO_GLASS,
  readGlassKnobs,
  whenGlassTokensReady,
  type GlassKnobs,
} from "../lib/glass";

export interface GlassState {
  /** true only when GC says the effect should actually render */
  effective: boolean;
  /** the live theme declares light paper (SURFACES.md §A) */
  light: boolean;
  knobs: GlassKnobs;
}

const OFF: GlassState = { effective: false, light: false, knobs: NO_GLASS };

// The glass preference is a runtime switch like the theme itself: the
// loader dispatches 'gc:glass' on every change, and a theme swap can move
// --gc-scheme and the tint under a mounted lens, so gc:apply / gc:source
// re-read too. This mirrors usePalette's subscription exactly — read the
// resolved tokens again, re-render, never refetch.
export function useGlass(): GlassState {
  const [state, setState] = useState<GlassState>(OFF);

  useEffect(() => {
    let disposed = false;
    const refresh = () => {
      if (disposed) return;
      if (!glassEffective()) {
        setState((prev) => (prev.effective ? OFF : prev));
        return;
      }
      // glass.css lands asynchronously behind the event; wait for the
      // first knob before mounting so the lens never renders at zero.
      whenGlassTokensReady().then(() => {
        if (disposed || !glassEffective()) return;
        setState({ effective: true, light: isLightScheme(), knobs: readGlassKnobs() });
      });
    };

    const gc = globalThis.GovernedCognition;
    const glassEvent = gc ? gc.glassEvent : "gc:glass";
    const applyEvent = gc ? gc.applyEvent : "gc:apply";
    const sourceEvent = gc ? gc.sourceEvent : "gc:source";
    document.addEventListener(glassEvent, refresh);
    document.addEventListener(applyEvent, refresh);
    document.addEventListener(sourceEvent, refresh);
    // The loader may finish after this effect runs (it restores the stored
    // preference during its own boot), so seed from whatever is true now
    // and again once the theme layer has resolved.
    refresh();
    const seeded = window.setTimeout(refresh, 250);

    return () => {
      disposed = true;
      window.clearTimeout(seeded);
      document.removeEventListener(glassEvent, refresh);
      document.removeEventListener(applyEvent, refresh);
      document.removeEventListener(sourceEvent, refresh);
    };
  }, []);

  return state;
}
