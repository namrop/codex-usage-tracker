// Governed Cognition liquid-glass bridge (SURFACES.md, "Wiring glass").
//
// GC owns the PREFERENCE; this app draws. Two rules from the doctrine are
// enforced here rather than at the call sites:
//
//   1. Mount off glassEffective(), never glass(). The former folds in
//      prefers-reduced-transparency, which outranks the toggle — the same
//      rank order as the reduced-motion clamp in every theme file.
//   2. GC absent is a no-op. Every read here is defensive: no
//      GovernedCognition, no glass, ever.
//
// The four prop feeds are published in glass.css as deliberately UNITLESS
// numbers scaled to liquid-glass-react's own prop scales, so they are read
// with getComputedStyle + parseFloat and handed straight to the library.

export interface GlassKnobs {
  blurAmount: number;
  saturation: number;
  displacementScale: number;
  aberrationIntensity: number;
}

// What a missing glass.css resolves to: an invisible lens. The component
// never mounts in that state anyway, but the zeroes mean a token layer that
// half-arrives degrades toward "no effect" rather than toward garbage.
export const NO_GLASS: GlassKnobs = {
  blurAmount: 0,
  saturation: 100,
  displacementScale: 0,
  aberrationIntensity: 0,
};

const BLUR_TOKEN = "--gc-glass-blur";

function rootStyle(): CSSStyleDeclaration {
  return getComputedStyle(document.documentElement);
}

function knob(style: CSSStyleDeclaration, name: string, fallback: number): number {
  const value = Number.parseFloat(style.getPropertyValue(name));
  return Number.isFinite(value) ? value : fallback;
}

export function readGlassKnobs(): GlassKnobs {
  const style = rootStyle();
  return {
    blurAmount: knob(style, BLUR_TOKEN, NO_GLASS.blurAmount),
    saturation: knob(style, "--gc-glass-saturate", NO_GLASS.saturation),
    displacementScale: knob(style, "--gc-glass-displace", NO_GLASS.displacementScale),
    aberrationIntensity: knob(style, "--gc-glass-aberration", NO_GLASS.aberrationIntensity),
  };
}

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

/** The live theme's declared scheme — the library's `overLight` feed, and
 * the per-theme choice SURFACES.md §A makes for the masthead: on a light
 * paper theme the reading plane stays opaque paper and glass is confined to
 * chrome that carries no prose. */
export function isLightScheme(): boolean {
  return rootStyle().getPropertyValue("--gc-scheme").trim() === "light";
}

/** glass.css is attached by loader.js only while the toggle is on, and the
 * <link> resolves asynchronously — so the knobs can be empty for a frame or
 * two after 'gc:glass'. Poll for the first token, with a deadline so a
 * pathological outage still renders (the same shape as whenThemeReady). */
export function whenGlassTokensReady(deadlineMs = 2000): Promise<void> {
  const started = Date.now();
  return new Promise((resolve) => {
    (function poll() {
      if (rootStyle().getPropertyValue(BLUR_TOKEN).trim() || Date.now() - started > deadlineMs) {
        resolve();
        return;
      }
      setTimeout(poll, 40);
    })();
  });
}

// THE RENDERER'S GAP, and the shim that closes it.
//
// liquid-glass-react puts `filter: url(#...)` (its displacement chain) and
// `backdrop-filter: blur() saturate()` on the SAME element. Per Filter
// Effects, an element with a `filter` becomes a BACKDROP ROOT — so its own
// backdrop is empty, and current Chromium implements exactly that: the
// library's frost does not render at all. Verified on this host
// (Chromium 148, headless=new, software and SwiftShader alike): a plain
// backdrop-filter div blurs; the same div with the library's filter added
// paints nothing. That is upstream's bug, not a theme bug, and the honest
// consequence of ignoring it would be a "glass" surface that is merely
// TRANSPARENT — strictly worse for reading than the opaque panel it
// replaced.
//
// So the app paints the frost itself, one element BELOW the lens with no
// `filter` of its own, using the library's own published scale — the one
// glass.css documents in the same breath as the token: "library scale:
// backdrop blur = (4 + blur * 32)px". The lens still draws on top and still
// contributes its edge highlights and (where it works) its refraction; only
// the frost moved. Nothing here invents a knob: both numbers come from the
// estate's tokens.
const BLUR_BASE = 4;
const BLUR_BASE_OVER_LIGHT = 12;
const BLUR_SCALE = 32;

export function frostFilter(knobs: GlassKnobs, light: boolean): string {
  const base = light ? BLUR_BASE_OVER_LIGHT : BLUR_BASE;
  const blur = base + knobs.blurAmount * BLUR_SCALE;
  return `blur(${blur}px) saturate(${knobs.saturation}%)`;
}
