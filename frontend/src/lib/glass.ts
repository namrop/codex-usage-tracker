// Governed Cognition liquid-glass bridge (SURFACES.md, "Wiring glass").
//
// GC owns the preference AND the renderer (keeper-ruled 2026-09-02: "GC
// owns the library, the apps call it"). This app names its glass class
// (components/GlassPanels.tsx) and calls glassAttach/glassDetach; the knob
// reads, the frost shim and the third-party renderer bridge that used to live
// here are GC's runtime now. Two rules stay enforced here rather than at
// the call sites:
//
//   1. Mount off glassEffective(), never glass(). The former folds in
//      prefers-reduced-transparency, which outranks the toggle — the same
//      rank order as the reduced-motion clamp in every theme file.
//   2. GC absent is a no-op. Every read here is defensive: no
//      GovernedCognition, no glass, ever.

/** The toggle AND the reader's transparency directive. Never glass(). */
export function glassEffective(): boolean {
  const gc = globalThis.GovernedCognition;
  if (!gc || typeof gc.glassEffective !== "function") return false;
  try {
    return gc.glassEffective() === true;
  } catch {
    return false;
  }
}

/** The live theme's declared scheme. Kept for the style layer's sake; the
 * johannah panel exception it used to gate is withdrawn (keeper, 2026-09-02). */
export function isLightScheme(): boolean {
  return getComputedStyle(document.documentElement).getPropertyValue("--gc-scheme").trim() === "light";
}
