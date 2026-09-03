import { useEffect, useState } from "react";

// Live viewport width for chart breakpoints. The option builders used to
// sample window.innerWidth inside a useMemo, which froze the tick limit at
// whatever the window was on first render (audit finding 2026-09-02).
export function useViewportWidth(): number {
  const [width, setWidth] = useState(() => window.innerWidth);
  useEffect(() => {
    const onResize = () => setWidth(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return width;
}
