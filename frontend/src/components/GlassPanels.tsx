import { useEffect } from "react";
import type { GlassState } from "../hooks/useGlass";

// THE GLASS CLASS (SURFACES.md, "Choosing surfaces — keeper-ruled
// 2026-08-30, revised"). For ai-usage the named target is THE MAIN PANELS —
// the shell's own top-level panels as a class, not one showpiece.
//
// GC DRAWS (keeper-ruled 2026-09-02: "GC owns the library, the apps call
// it"). This component's whole job is to name the class and hand each panel
// to GovernedCognition.glassAttach() while glass is effective, and to take
// it back with glassDetach(); the lens, the layer, the frost and the knobs
// that lived beside it in GlassLayer.tsx and lib/glass.ts are GC's runtime
// now. The panels are the shell's children and none of them has to know
// that glass exists.
//
// What makes glass-over-content safe is the opacity slider: --gc-glass-opacity
// drives --gc-glass-tint, the wash the panels paint, so a reader who finds
// glass-over-content noisy raises it until the panel is effectively opaque.
//
// One limit still holds, in the style layer beside this: chart surfaces and
// the folded numeric tables stay OPAQUE planes inside the glass panel (a
// canvas has no text a wash can rescue). The johannah limit is withdrawn
// (keeper, 2026-09-02: a paper theme's panels glass like every other's).

const PANELS = ".shell > .title-panel, .shell > .card";
const FLAG = "data-glass";

export function GlassPanels({ glass }: { glass: GlassState }) {
  const on = glass.effective;

  useEffect(() => {
    if (!on) return;
    const shell = document.querySelector(".shell");
    const GC = globalThis.GovernedCognition;
    if (!shell || !GC || typeof GC.glassAttach !== "function") return;
    let current: HTMLElement[] = [];

    const scan = () => {
      const found = Array.from(document.querySelectorAll<HTMLElement>(PANELS));
      if (found.length === current.length && found.every((el, i) => el === current[i])) return;
      current.forEach((el) => {
        if (!found.includes(el)) {
          el.removeAttribute(FLAG);
          GC.glassDetach(el);
        }
      });
      found.forEach((el) => {
        if (!current.includes(el)) {
          el.setAttribute(FLAG, "on");
          GC.glassAttach(el);
        }
      });
      current = found;
    };

    scan();
    // The shell's panels are static today, but a panel that appears later
    // must get its layer; a panel that leaves must take its flag with it.
    const observer = new MutationObserver(scan);
    observer.observe(shell, { childList: true });

    return () => {
      observer.disconnect();
      current.forEach((el) => {
        el.removeAttribute(FLAG);
        GC.glassDetach(el);
      });
    };
  }, [on]);

  return null;
}
