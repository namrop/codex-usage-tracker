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


# ---------------------------------------------------------------------------
# Shape axis: the slant consumer contract.
#
# Keeper-ruled 2026-08-29 ("use the pattern that diadem used"). These mirror
# diadem's tests-static slant-obedience section: the app consumes the
# published namrop grammar, never re-declares it, and rounded mode cannot
# see any of it. GC's standing marks are the law being pinned here — radius
# 0 mode-wide, cuts only on the ruled families, the border -> lift swap, and
# the keeper-tuned 28/24/18/14/12 scale reached only through its tokens.
# ---------------------------------------------------------------------------

SLANT_MARKER = "SLANT — the governed-cognition shape axis"
SLANT_SCOPE = 'html[data-gc-shape="slanted"]'


def _slant_split() -> tuple[str, str]:
    """Return (everything outside the slant section, the slant section) of the
    app style block, comments included. The section runs from its header
    comment to the prefers-reduced-motion block, which stays the sheet's last
    law and is shape-independent."""
    _attrs, body = _style_blocks()[0]
    marker = body.index(SLANT_MARKER)
    start = body.rfind("/*", 0, marker)
    assert start != -1
    end = body.index("@media (prefers-reduced-motion", start)
    return body[:start] + body[end:], body[start:end]


def _uncommented(css: str) -> str:
    return COMMENT_RE.sub("", css)


def test_slant_section_exists_and_owns_every_clip_path() -> None:
    """Rounded mode must be byte-identical to the pre-shape sheet: no
    clip-path may exist outside the slant section."""
    outside, slant = _slant_split()
    assert "clip-path" not in _uncommented(outside), (
        "clip-path outside the slant section — rounded mode would change shape"
    )
    assert _uncommented(slant).count("clip-path:") >= 3


def test_every_slant_selector_is_scoped_to_the_shape_attribute() -> None:
    """Nothing in the section may leak into rounded mode."""
    _outside, slant = _slant_split()
    for line in _uncommented(slant).splitlines():
        if "{" not in line or "}" in line or line.lstrip().startswith("@"):
            continue
        if line.strip() == ") {":  # a multiline :is() closing its selector
            continue
        assert SLANT_SCOPE in line or ":is(" in line, (
            f"unscoped slant selector (rounded mode would match it): {line.strip()!r}"
        )
    assert f"{SLANT_SCOPE} :is(" in _uncommented(slant)


def test_cuts_ride_the_published_tier_tokens_only() -> None:
    """The keeper-tuned scale is reached through its tokens — never a
    literal, never a calc-derived half tier (retired at GC 99e4608)."""
    _outside, slant = _slant_split()
    rules = _uncommented(slant)
    for tier in ("xl", "lg", "md", "sm", "pill"):
        assert f"var(--gc-cut-{tier})" in rules, f"cut tier {tier} unused"
    assert "calc(" not in rules, "cut tiers are published, not derived"
    assert not re.search(r"--gc-cut:\s*[\d.]", rules), "literal cut value in the slant section"


def test_published_clip_templates_are_consumed_never_redeclared() -> None:
    """GC declares the templates on * so each element composes its polygon
    from its own --gc-cut. A local copy would freeze the tier again."""
    _outside, slant = _slant_split()
    rules = _uncommented(slant)
    assert "clip-path: var(--gc-clip-diagonal)" in rules
    assert "clip-path: var(--gc-clip-octagon)" in rules
    assert "polygon(" not in rules, (
        "the grammar is published upstream — consumers reference it, they do not copy it"
    )


def test_enclosed_surfaces_carry_the_ruled_border_to_lift_swap() -> None:
    """RULED: slant swaps hairline borders for background lifts, because
    clip-path shears a painted border off at the cut."""
    _outside, slant = _slant_split()
    rules = _uncommented(slant)
    swaps = rules.count("border-color: transparent;")
    lifts = rules.count("linear-gradient(var(--gc-card-lift), var(--gc-card-lift))")
    assert swaps >= 2 and swaps == lifts, (
        f"border -> lift swap unbalanced: {swaps} border resets, {lifts} lifts"
    )


def test_radius_is_zero_mode_wide_under_slant() -> None:
    """RULED: under slant the base geometry is square; only the cut families
    carry geometry. The !important beats the literal pill/dot radii the token
    axis cannot reach."""
    _outside, slant = _slant_split()
    rules = _uncommented(slant)
    assert f"{SLANT_SCOPE} *,\n" in rules or f"{SLANT_SCOPE} *," in rules
    assert "border-radius: 0 !important;" in rules


def test_charts_are_never_clipped_under_slant() -> None:
    """A canvas carries its axis labels in its own corners; a cut there eats
    the reading."""
    _outside, slant = _slant_split()
    rules = _uncommented(slant)
    chart_rule = re.search(
        r"html\[data-gc-shape=\"slanted\"\] :is\(([^)]*canvas[^)]*)\)\s*\{([^}]*)\}",
        rules,
        re.S,
    )
    assert chart_rule is not None, "no explicit chart exemption in the slant section"
    assert "clip-path: none" in chart_rule.group(2)
    assert ".chart-wrap" in chart_rule.group(1)


def test_vendored_overlay_publishes_the_full_grammar_surface() -> None:
    """The fallback snapshot must carry the pill tier and the templates on *
    (GC 99e4608) — an origin outage must not fall back to a broken slant."""
    overlay = (GC_SNAPSHOT / "shape-slant.css").read_text(encoding="utf-8")
    assert "--gc-cut-pill: 7px" in overlay
    assert "--gc-clip-diagonal:" in overlay
    assert "--gc-clip-octagon:" in overlay
    templates = overlay[overlay.index("--gc-clip-diagonal:") :]
    scope = overlay.rfind("{", 0, overlay.index("--gc-clip-diagonal:"))
    assert overlay[:scope].rstrip().endswith("*"), (
        "clip templates must be declared on * — a :root declaration freezes "
        "--gc-cut at the root value and per-element tiers can never resolve"
    )
    assert "polygon(" in templates
