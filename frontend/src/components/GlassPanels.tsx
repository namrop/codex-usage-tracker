import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { GlassLayer } from "./GlassLayer";
import type { GlassState } from "../hooks/useGlass";

// THE GLASS CLASS (SURFACES.md, "Choosing surfaces — keeper-ruled
// 2026-08-30, revised"). The first pass of the doctrine said glass was
// chrome only and this app gave it one masthead; the keeper overruled that
// as too narrow. For ai-usage the named target is THE MAIN PANELS — the
// shell's own top-level panels as a class, not one showpiece.
//
// What makes that safe is the opacity slider: --gc-glass-opacity drives
// --gc-glass-tint, the wash these panels paint, so a reader who finds
// glass-over-content noisy raises it until the panel is effectively opaque.
// Legibility is a control now, not a design guess, so the app's job is to
// target the class honestly and get out of the way.
//
// Two limits still hold, and they live in the style layer beside this:
// chart surfaces and the folded numeric tables stay OPAQUE planes inside
// the glass panel (a canvas has no text a wash can rescue), and johannah
// gets no panel glass at all — its reading planes are opaque paper because
// that is what the source site authored.
//
// The lens mounts by portal rather than by threading a prop through four
// components: the panels are the shell's children, they are the class, and
// none of them should have to know that glass exists.

const PANELS = ".shell > .title-panel, .shell > .card";
const FLAG = "data-glass";

export function GlassPanels({ glass }: { glass: GlassState }) {
  // johannah keeps the jlmu treatment — ruled, and read from the theme's
  // own --gc-scheme rather than from its name.
  const on = glass.effective && !glass.light;
  const [hosts, setHosts] = useState<HTMLElement[]>([]);

  useEffect(() => {
    if (!on) {
      setHosts([]);
      return;
    }
    const shell = document.querySelector(".shell");
    if (!shell) return;
    let current: HTMLElement[] = [];

    const scan = () => {
      const found = Array.from(document.querySelectorAll<HTMLElement>(PANELS));
      if (found.length === current.length && found.every((el, i) => el === current[i])) return;
      current.forEach((el) => el.removeAttribute(FLAG));
      found.forEach((el) => el.setAttribute(FLAG, "on"));
      current = found;
      setHosts(found);
    };

    scan();
    // The shell's panels are static today, but a panel that appears later
    // must get its lens; a panel that leaves must take its flag with it.
    const observer = new MutationObserver(scan);
    observer.observe(shell, { childList: true });

    return () => {
      observer.disconnect();
      current.forEach((el) => el.removeAttribute(FLAG));
      setHosts([]);
    };
  }, [on]);

  if (!on) return null;
  return (
    <>
      {hosts.map((host, index) => createPortal(
        <GlassLayer knobs={glass.knobs} light={glass.light} />,
        host,
        `glass-panel-${index}`,
      ))}
    </>
  );
}
