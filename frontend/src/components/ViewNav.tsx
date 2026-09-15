import { VIEWS, viewHref, type ViewId } from "../lib/views";

// The shared view switcher (2026-09-14). Real links, not buttons: the URL
// hash IS the view state, so the browser's history, reload and copied links
// all work, keyboard users get ordinary link semantics, and aria-current
// names the selected view for assistive tech while the style layer draws
// it. The refresh control lives here so every view can reach it.

export function ViewNav({ current, refreshing, onRefresh }: {
  current: ViewId;
  refreshing: boolean;
  onRefresh: () => void;
}) {
  return (
    <nav className="view-nav" aria-label="Dashboard views">
      <ul className="view-nav-list">
        {VIEWS.map((view) => (
          <li key={view.id}>
            <a
              className="view-nav-link"
              href={viewHref(view.id)}
              aria-current={current === view.id ? "page" : undefined}
              title={view.description}
            >
              {view.label}
            </a>
          </li>
        ))}
      </ul>
      <div className="toolbar" aria-label="Refresh controls">
        <button className="refresh-btn" id="refreshUsage" disabled={refreshing} onClick={onRefresh}>
          {refreshing ? "Refreshing…" : "Refresh"}
        </button>
      </div>
    </nav>
  );
}
