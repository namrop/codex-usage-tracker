"""Governed Cognition conformance gate for the dashboard template.

The kintsugi refactor (Atrium 07_systems/ai_usage_tracker/
valarin_render_ai_usage_frontend_refactor_kintsugi_2026-08-29.md, akasan
spoken 2026-08-29): the style layer references tokens only, zero literals.
The law lives in governed-cognition; the obedience lives here — this test is
the imported purity scan running in this repo's harness so a regression
(someone pasting a hex back into the template) fails CI.

Documented exceptions:
  - the <style id="gc-type-bridge"> block may declare font-family, but only
    as var(--gc-font-*) references (mirrors diadem's excluded bridge sheet);
  - JS may *construct* rgba() strings from resolved token values (the
    withAlpha helper) — it may not carry color literals.

When the governed-cognition repo is present on the host, its own
conformance.scan_css_text also runs on the main style block for parity with
the estate gate.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "src" / "codex_usage_tracker" / "templates" / "index.html"
GC_SNAPSHOT = REPO_ROOT / "src" / "codex_usage_tracker" / "static" / "gc"
GC_REPO = Path("/srv/pharos/workspaces/governed-cognition")

STYLE_BLOCK_RE = re.compile(r"<style(?P<attrs>[^>]*)>(?P<body>.*?)</style>", re.S)
SCRIPT_BLOCK_RE = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S)
COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)

# A hex COLOR is 3/4/6/8 hex digits (ids like #topbar are structure, not
# color) — the diadem/GC purity-scan rule verbatim.
HEX_COLOR_RE = re.compile(r"#([0-9a-fA-F]{3,8})\b")
COLOR_FN_LITERAL_RE = re.compile(r"\b(?:rgb|rgba|hsl|hsla)\(\s*[\d.]")


def _style_blocks() -> list[tuple[str, str]]:
    text = TEMPLATE.read_text(encoding="utf-8")
    return [(m.group("attrs"), m.group("body")) for m in STYLE_BLOCK_RE.finditer(text)]


def _hex_colors(body: str) -> list[str]:
    found = []
    for match in HEX_COLOR_RE.finditer(body):
        token = match.group(1)
        if len(token) in (3, 4, 6, 8):
            found.append("#" + token)
    return found


def test_template_has_exactly_one_bridge_and_one_app_style_block() -> None:
    blocks = _style_blocks()
    assert len(blocks) == 2, "expected the app style block plus the type bridge"
    assert "gc-type-bridge" in blocks[1][0]


def test_app_style_layer_is_token_pure() -> None:
    attrs, body = _style_blocks()[0]
    assert "gc-type-bridge" not in attrs
    css = COMMENT_RE.sub("", body)
    assert _hex_colors(css) == [], "hex color literal(s) in the app style layer"
    assert not COLOR_FN_LITERAL_RE.search(css), (
        "rgb()/rgba()/hsl() literal in the app style layer — colors resolve "
        "through --gc-* tokens (color-mix over var() for alpha)"
    )
    assert "font-family" not in css, (
        "font-family belongs in the gc-type-bridge block, families in the theme layer"
    )
    assert "var(--" in css, "style layer is not consuming tokens"


def test_type_bridge_only_applies_theme_families() -> None:
    _attrs, body = _style_blocks()[1]
    css = COMMENT_RE.sub("", body)
    assert _hex_colors(css) == []
    assert not COLOR_FN_LITERAL_RE.search(css)
    for match in re.finditer(r"font-family:\s*([^;]+);", css):
        value = match.group(1).strip()
        assert re.fullmatch(r"var\(--gc-font-[a-z]+\)", value), (
            f"type bridge may only reference GC font tokens, got: {value!r}"
        )


def test_inline_js_carries_no_color_literals() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    for match in SCRIPT_BLOCK_RE.finditer(text):
        if "src=" in match.group("attrs"):
            continue
        js = match.group("body")
        quoted_hex = re.findall(r"[\"'`]#[0-9a-fA-F]{3,8}\b", js)
        assert quoted_hex == [], f"hex color literal(s) in inline JS: {quoted_hex}"
        assert not COLOR_FN_LITERAL_RE.search(js), (
            "rgba()/hsl() literal with numeric components in inline JS — "
            "construct colors from resolved tokens (withAlpha) instead"
        )


def test_no_external_font_fetch() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "fonts.googleapis.com" not in text
    assert "fonts.gstatic.com" not in text


def test_vendored_gc_snapshot_present_and_carries_declared_sets() -> None:
    """The fallback snapshot must exist and hold the ai-usage sets — an
    origin outage renders yesterday's theme, never unstyled."""
    assert (GC_SNAPSHOT / "loader.js").is_file()
    for name in ("governed-cognition", "neo-andean", "johannah"):
        css = (GC_SNAPSHOT / f"theme-{name}.css").read_text(encoding="utf-8")
        for rank in range(1, 11):
            assert f"--gc-cat-ai-usage-series-rank-rank-{rank}:" in css, (
                f"{name}: series-rank member rank-{rank} missing from snapshot"
            )
        for member in ("claude_code", "claude_code_secondary", "hermes", "opencode"):
            assert f"--gc-cat-ai-usage-harness-{member}:" in css, (
                f"{name}: harness member {member} missing from snapshot"
            )


def test_gc_repo_conformance_scan_agrees_when_available() -> None:
    """Run governed-cognition's own scanner on the app style block when the
    repo is on this host (parity with the estate gate; skipped elsewhere)."""
    conformance_path = GC_REPO / "conformance.py"
    if not conformance_path.is_file():
        import pytest

        pytest.skip("governed-cognition repo not present on this host")
    spec = importlib.util.spec_from_file_location("gc_conformance", conformance_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    _attrs, body = _style_blocks()[0]
    findings = module.scan_css_text(body, "index.html:<style>")
    assert findings == [], "\n".join(findings)
