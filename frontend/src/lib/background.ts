// The page background (SURFACES.md, "Wiring the background") — and the one
// thing the doctrine's CSS snippet cannot do on its own here.
//
// THE FINDING. GC publishes the theme's image as a RELATIVE url in a custom
// property, on the promise that it "resolves beside this stylesheet". That
// promise holds when the var() is used in the theme sheet itself. It does
// not hold across sheets: Chromium resolves a relative url() carried by a
// custom property against the stylesheet where the var() is USED, not the
// one that declared it. So `background-image: var(--gc-bg-image)` written in
// this app's bundle resolves `assets/namrop-bg-*.webp` against
// /static/app/assets/ and 404s — the layered wiring degrades to --gc-bg
// exactly as designed, which is why the failure is silent and why the whole
// treatment was invisible until the computed value was read out loud.
//
// THE FIX, app-side and token-shaped: read the token, resolve it against the
// href of the very stylesheet that declared it (GC's own managed #gc-theme
// link — origin-served or the vendored snapshot, whichever the loader
// resolved), and republish the absolute url as --page-bg-image for the style
// layer to consume. No color crosses this boundary; only a URL.

const TOKEN = "--gc-bg-image";
const PUBLISHED = "--page-bg-image";
const URL_RE = /^url\((["']?)(.*?)\1\)$/;

/** The stylesheet that declared --gc-bg-image: GC's managed theme link. */
function themeBase(): string {
  const link = document.getElementById("gc-theme") as HTMLLinkElement | null;
  return (link && link.href) || document.baseURI;
}

/** Republish the theme's background image as an absolute url. Returns the
 * value published, or null when the theme ships no image (`none`). */
export function applyBackgroundImage(): string | null {
  const root = document.documentElement;
  const raw = getComputedStyle(root).getPropertyValue(TOKEN).trim();
  const match = URL_RE.exec(raw);
  if (!match || !match[2]) {
    // A theme with no background emits the literal `none`; drop ours and
    // the style layer's own `none` fallback takes over.
    root.style.removeProperty(PUBLISHED);
    return null;
  }
  let absolute: string;
  try {
    absolute = new URL(match[2], themeBase()).href;
  } catch {
    root.style.removeProperty(PUBLISHED);
    return null;
  }
  root.style.setProperty(PUBLISHED, `url("${absolute}")`);
  return absolute;
}
