import { useEffect, useState } from "react";
import { glassEffective, isLightScheme } from "../lib/glass";

export interface GlassState {
  /** true only when GC says the effect should actually render */
  effective: boolean;
  /** the live theme declares light paper (SURFACES.md §A) */
  light: boolean;
}

const OFF: GlassState = { effective: false, light: false };

// The glass preference is a runtime switch like the theme itself: the
// loader dispatches 'gc:glass' on every change, and a theme swap can move
// --gc-scheme under a mounted layer, so gc:apply / gc:source re-read too.
// This mirrors usePalette's subscription exactly — read again, re-render,
// never refetch. GC's renderer mounts and unmounts its layers off the same
// effective state on its own; this state only drives the app's own
// mounted-flag (the style layer's gate) and the attach calls.
export function useGlass(): GlassState {
  const [state, setState] = useState<GlassState>(OFF);

  useEffect(() => {
    let disposed = false;
    const refresh = () => {
      if (disposed) return;
      const effective = glassEffective();
      setState((prev) => {
        const next = { effective, light: effective ? isLightScheme() : false };
        return prev.effective === next.effective && prev.light === next.light ? prev : next;
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
