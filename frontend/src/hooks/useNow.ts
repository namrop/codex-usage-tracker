import { useEffect, useState } from "react";

// One-second wall-clock tick for countdown surfaces; quiet when the tab is
// hidden. Reduced-motion still gets text updates — a countdown IS the
// content. Used inside the leaf countdown components so a tick never
// re-renders the whole dashboard tree.
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => {
      if (document.hidden) return;
      setNow(Date.now());
    }, intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}
