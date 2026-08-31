import { useEffect, useRef, useState } from "react";
import LiquidGlass from "liquid-glass-react";
import { frostFilter, type GlassKnobs } from "../lib/glass";

// liquid-glass-react as a BACKDROP, not a container.
//
// The library is built to be a floating pill: it centres itself on its
// anchor (top/left 50% + translate(-50%,-50%)) and it restyles its own
// children (`font: 500 20px/1 system-ui`, a white text color, a text
// shadow). Handing it the masthead's real content would mean handing it
// the masthead's typography, and the theme layer owns that. So the lens
// gets an empty spacer sized to the host, and the host's real content
// stays outside it at z-index 1 — the app draws, GC's tokens still rule
// the type.
//
// The lens is also deliberately STATIC: globalMousePos and mouseOffset are
// passed in as fixed zeroes, which is the library's own opt-out for its
// internal mousemove tracking. A dashboard that re-renders Chart.js on a
// 60s hydrate does not need a panel wobbling under the cursor, and a still
// lens is the honest reading of the reduced-motion posture the sheet keeps
// at its end.

const STATIC_MOUSE = { x: 0, y: 0 } as const;

interface HostBox {
  width: number;
  height: number;
  radius: number;
}

function measure(host: HTMLElement): HostBox {
  const rect = host.getBoundingClientRect();
  // Read the RESOLVED radius off the host rather than --gc-radius-md, so
  // the slant overlay's mode-wide `border-radius: 0 !important` reaches the
  // lens too. (The library still draws its own rounded rect, so it cannot
  // follow the slant CUT polygons — a known, recorded limitation.)
  const radius = Number.parseFloat(getComputedStyle(host).borderTopLeftRadius);
  return {
    width: Math.round(rect.width),
    height: Math.round(rect.height),
    radius: Number.isFinite(radius) ? radius : 0,
  };
}

function same(a: HostBox | null, b: HostBox): boolean {
  return !!a && a.width === b.width && a.height === b.height && a.radius === b.radius;
}

/** Renders the lens behind its PARENT element, sized to it. Mount it as the
 * first child of a `position: relative` host and give the host's real
 * children z-index 1 (see `.glass-layer` in the style layer). */
export function GlassLayer({ knobs, light }: { knobs: GlassKnobs; light: boolean }) {
  const anchor = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState<HostBox | null>(null);

  useEffect(() => {
    const host = anchor.current?.parentElement;
    if (!host) return;
    const sync = () => setBox((prev) => (same(prev, measure(host)) ? prev : measure(host)));
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  return (
    <div className="glass-layer" ref={anchor} aria-hidden="true">
      {box && box.width > 0 && box.height > 0
        ? (
          <>
            {/* the frost, painted below the lens — see frostFilter() */}
            <div
              className="glass-frost"
              style={{ backdropFilter: frostFilter(knobs, light), borderRadius: box.radius }}
            />
            <LiquidGlass
              style={{ position: "absolute", top: "50%", left: "50%" }}
              padding="0px"
              cornerRadius={box.radius}
              elasticity={0}
              overLight={light}
              globalMousePos={STATIC_MOUSE}
              mouseOffset={STATIC_MOUSE}
              blurAmount={knobs.blurAmount}
              saturation={knobs.saturation}
              displacementScale={knobs.displacementScale}
              aberrationIntensity={knobs.aberrationIntensity}
            >
              <div style={{ width: box.width, height: box.height }} />
            </LiquidGlass>
          </>
        )
        : null}
    </div>
  );
}
