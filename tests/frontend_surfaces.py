"""Shared accessors for the React + Vite frontend surfaces.

The 2026-08-30 React + Vite conversion moved the dashboard frontend out of
the single Flask template (templates/index.html, retired) into frontend/
(sources) and src/codex_usage_tracker/static/app/ (the committed build the
Flask routes serve). Tests that used to grep the served template for markup
and inline-JS invariants now read these surfaces:

- entry_html / built_html: the Vite entry document (source and built) — the
  theme-contract surface (GC loader as a classic head script).
- app_css / bridge_css: the two style sheets that replaced the template's
  two <style> blocks, byte-for-byte carriers of the token-pure adapter layer
  and the font type bridge.
- frontend sources: every .ts/.tsx under frontend/src — the app JS layer.
- built assets: the hashed bundle in static/app/assets; index-*.js is the
  app chunk (held to the zero-color-literal law), vendor-*.js is third-party
  (React), exempt like the vendored GC snapshot and Chart.js.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = REPO_ROOT / "frontend"
FRONTEND_SRC = FRONTEND_DIR / "src"
BUILT_DIR = REPO_ROOT / "src" / "codex_usage_tracker" / "static" / "app"
GC_SNAPSHOT = REPO_ROOT / "src" / "codex_usage_tracker" / "static" / "gc"


def entry_html() -> str:
    return (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")


def built_html() -> str:
    return (BUILT_DIR / "index.html").read_text(encoding="utf-8")


def app_css() -> str:
    return (FRONTEND_SRC / "styles" / "app.css").read_text(encoding="utf-8")


def bridge_css() -> str:
    return (FRONTEND_SRC / "styles" / "type-bridge.css").read_text(encoding="utf-8")


def source(relative: str) -> str:
    """Read one frontend source file by path relative to frontend/src."""
    return (FRONTEND_SRC / relative).read_text(encoding="utf-8")


def source_files() -> dict[Path, str]:
    """Every TS/TSX/CSS source plus the entry HTML, path -> text."""
    files: dict[Path, str] = {FRONTEND_DIR / "index.html": entry_html()}
    for path in sorted(FRONTEND_SRC.rglob("*")):
        if path.suffix in (".ts", ".tsx", ".css") and path.is_file():
            files[path] = path.read_text(encoding="utf-8")
    return files


def frontend_text() -> str:
    """Concatenation of every frontend source (greppable app surface)."""
    return "\n".join(source_files().values())


def _single(pattern: str) -> Path:
    matches = sorted(BUILT_DIR.glob(pattern))
    assert len(matches) == 1, f"expected exactly one {pattern} in the built assets, got {matches}"
    return matches[0]


def built_app_js() -> str:
    return _single("assets/index-*.js").read_text(encoding="utf-8")


def built_vendor_js() -> str:
    return _single("assets/vendor-*.js").read_text(encoding="utf-8")


def built_css() -> str:
    return _single("assets/index-*.css").read_text(encoding="utf-8")
