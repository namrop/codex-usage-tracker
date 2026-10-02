"""Governed Cognition conformance gate for the dashboard frontend.

The kintsugi refactor (Atrium 07_systems/ai_usage_tracker/
valarin_render_ai_usage_frontend_refactor_kintsugi_2026-08-29.md, akasan
spoken 2026-08-29): the style layer references tokens only, zero literals.
The law lives in governed-cognition; the obedience lives here — this is the
imported purity scan running in this repo's harness so a regression (someone
pasting a hex back into the styles) fails CI.

React + Vite retarget (2026-08-30): the single Flask template died; the
same invariants now bind the frontend sources and the committed build:

- frontend/src/styles/app.css   — the app style layer (was <style> block 1)
- frontend/src/styles/type-bridge.css — the font bridge (was block 2)
- frontend/src/**/*.ts(x)       — the app JS layer (was the inline script)
- frontend/index.html + static/app/index.html — the entry documents
- static/app/assets/index-*.js / index-*.css  — the built app chunk/sheet

Documented exceptions:
  - the type bridge may declare font-family, but only as var(--gc-font-*)
    references (mirrors diadem's excluded bridge sheet);
  - JS may *construct* rgba() strings from resolved token values (the
    withAlpha helper family) — it may not carry color literals;
  - the vendor-*.js chunk (React internals) is third-party, exempt like the
    vendored GC snapshot and Chart.js UMD.

When the governed-cognition repo is present on the host, its own
conformance.scan_css_text also runs on the app sheet for parity with the
estate gate.
"""

from __future__ import annotations

import importlib.util
import re

from frontend_surfaces import (
    GC_SNAPSHOT,
    REPO_ROOT,
    app_css,
    bridge_css,
    built_app_js,
    built_css,
    built_html,
    built_vendor_js,
    entry_html,
    source,
    source_files,
)
from pathlib import Path

GC_REPO = Path("/srv/pharos/workspaces/governed-cognition")

COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)

# A hex COLOR is 3/4/6/8 hex digits (ids like #topbar are structure, not
# color) — the diadem/GC purity-scan rule verbatim.
HEX_COLOR_RE = re.compile(r"#([0-9a-fA-F]{3,8})\b")
COLOR_FN_LITERAL_RE = re.compile(r"\b(?:rgb|rgba|hsl|hsla)\(\s*[\d.]")


def _hex_colors(body: str) -> list[str]:
    found = []
    for match in HEX_COLOR_RE.finditer(body):
        token = match.group(1)
        if len(token) in (3, 4, 6, 8):
            found.append("#" + token)
    return found


def test_template_has_exactly_one_bridge_and_one_app_style_block() -> None:
    """The style layer is exactly two sheets — the app adapter layer and the
    type bridge — imported in that order, and the build bundles exactly one
    stylesheet (no third sheet can smuggle literals in)."""
    main = source_files()[
        next(p for p in source_files() if p.name == "main.tsx")
    ]
    app_at = main.index('import "./styles/app.css";')
    bridge_at = main.index('import "./styles/type-bridge.css";')
    assert app_at < bridge_at, "app layer must load before the type bridge"
    css_imports = re.findall(r"import\s+\"[^\"]*\.css\";", main)
    assert len(css_imports) == 2, "expected the app sheet plus the type bridge, nothing else"
    styles_dir = [p for p in source_files() if p.suffix == ".css"]
    assert sorted(p.name for p in styles_dir) == ["app.css", "type-bridge.css"]
    # One built stylesheet, and the served entry document names it.
    assert built_css()
    assert re.search(r'<link rel="stylesheet"[^>]*href="/static/app/assets/index-[^"]+\.css">', built_html())


def test_app_style_layer_is_token_pure() -> None:
    """Zero color literals in the app style layer, source AND built: colors
    resolve through --gc-* tokens (color-mix over var() for alpha), and
    font-family belongs to the bridge alone."""
    css = COMMENT_RE.sub("", app_css())
    assert _hex_colors(css) == [], "hex color literal(s) in the app style layer"
    assert not COLOR_FN_LITERAL_RE.search(css), (
        "rgb()/rgba()/hsl() literal in the app style layer — colors resolve "
        "through --gc-* tokens (color-mix over var() for alpha)"
    )
    assert "font-family" not in css, (
        "font-family belongs in the type-bridge sheet, families in the theme layer"
    )
    assert "var(--" in css, "style layer is not consuming tokens"
    # The committed build bundles both sheets; the purity scan holds there
    # too (the bridge adds font-family lines but never colors).
    built = COMMENT_RE.sub("", built_css())
    assert _hex_colors(built) == [], "hex color literal(s) in the built stylesheet"
    assert not COLOR_FN_LITERAL_RE.search(built)


def test_type_bridge_only_applies_theme_families() -> None:
    """The bridge is font APPLICATION only: every font-family value is a GC
    font token reference, and no color sneaks in."""
    css = COMMENT_RE.sub("", bridge_css())
    assert _hex_colors(css) == []
    assert not COLOR_FN_LITERAL_RE.search(css)
    families = re.findall(r"font-family:\s*([^;]+);", css)
    assert families, "the bridge exists to apply theme families"
    for value in families:
        assert re.fullmatch(r"var\(--gc-font-[a-z]+\)", value.strip()), (
            f"type bridge may only reference GC font tokens, got: {value.strip()!r}"
        )


def test_inline_js_carries_no_color_literals() -> None:
    """The app JS layer (every frontend source and the built app chunk)
    carries no color literals — it may only CONSTRUCT rgba() from resolved
    token values (the withAlpha exception). The vendor chunk is third-party
    and exempt, like the GC snapshot."""
    for path, text in source_files().items():
        if path.suffix not in (".ts", ".tsx", ".html"):
            continue
        quoted_hex = re.findall(r"[\"'`]#[0-9a-fA-F]{3,8}\b", text)
        assert quoted_hex == [], f"hex color literal(s) in {path.name}: {quoted_hex}"
        assert not COLOR_FN_LITERAL_RE.search(text), (
            f"rgba()/hsl() literal with numeric components in {path.name} — "
            "construct colors from resolved tokens (withAlpha) instead"
        )
    built = built_app_js()
    assert re.findall(r"[\"'`]#[0-9a-fA-F]{3,8}\b", built) == [], (
        "hex color literal(s) in the built app chunk"
    )
    assert not COLOR_FN_LITERAL_RE.search(built), (
        "color-function literal in the built app chunk"
    )


def test_no_external_font_fetch() -> None:
    """All-local law (kintsugi M1): fonts are the theme layer's vendored
    woff2 — no Google Fonts fetch anywhere in the frontend or the build."""
    surfaces = [entry_html(), built_html(), built_css(), built_app_js()]
    surfaces.extend(source_files().values())
    for text in surfaces:
        assert "fonts.googleapis.com" not in text
        assert "fonts.gstatic.com" not in text


def test_gc_loader_stays_a_classic_head_script() -> None:
    """The theme contract: /static/gc/loader.js is named by the page as a
    classic synchronous <script> in <head> — never bundled, never
    type=module — with data-gc-fallback, BEFORE the app bundle, so the theme
    injects pre-paint and an origin outage falls back to the snapshot. The
    picker mounts through GovernedCognition.mountSwitcher."""
    for html in (entry_html(), built_html()):
        loader = re.search(r"<script[^>]*src=\"/static/gc/loader\.js\"[^>]*>", html)
        assert loader, "GC loader script tag missing"
        assert "type=" not in loader.group(0), "loader must stay a classic script"
        assert 'data-gc-fallback="/static/gc"' in loader.group(0)
        head = html.index("</head>")
        assert html.index(loader.group(0)) < head, "loader must load in <head>"
        module_at = re.search(r"<script type=\"module\"", html)
        if module_at:
            assert html.index(loader.group(0)) < module_at.start(), (
                "loader must run before the app bundle"
            )
    joined = "\n".join(source_files().values())
    assert 'mountSwitcher("#gcSwitcher")' in joined
    # Chart.js stays the vendored classic script beside it, never bundled.
    assert '<script src="/static/vendor/chart.umd.min.js"></script>' in built_html()


def test_vendored_gc_snapshot_present_and_carries_declared_sets() -> None:
    """The fallback snapshot must exist and hold the ai-usage sets — an
    origin outage renders yesterday's theme, never unstyled."""
    assert (GC_SNAPSHOT / "loader.js").is_file()
    # alexandria replaced the dropped governed-cognition theme as the default (2026-10-02)
    for name in ("alexandria", "neo-andean", "johannah"):
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
    """Run governed-cognition's own scanner on the app sheet when the repo
    is on this host (parity with the estate gate; skipped elsewhere)."""
    conformance_path = GC_REPO / "conformance.py"
    if not conformance_path.is_file():
        import pytest

        pytest.skip("governed-cognition repo not present on this host")
    spec = importlib.util.spec_from_file_location("gc_conformance", conformance_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    findings = module.scan_css_text(app_css(), "frontend/src/styles/app.css")
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
# The React conversion moved the sheet to frontend/src/styles/app.css
# byte-for-byte; the section grammar is unchanged.
# ---------------------------------------------------------------------------

SLANT_MARKER = "SLANT — the governed-cognition shape axis"
SLANT_SCOPE = 'html[data-gc-shape="slanted"]'


def _slant_split() -> tuple[str, str]:
    """Return (everything outside the slant section, the slant section) of
    the app sheet, comments included. The section runs from its header
    comment to the prefers-reduced-motion block, which stays the sheet's
    last law and is shape-independent."""
    body = app_css()
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


# ---------------------------------------------------------------------------
# Atmosphere axis: the theme background and the liquid-glass surface.
#
# governed-cognition/SURFACES.md (2026-08-30) is the estate doctrine these
# pin. Two laws matter more than the rest and both are about READING:
#
#   1. the field lives BEHIND the page — every plane that carries a chart, a
#      stat tile, a donut or a table stays opaque (SURFACES.md §A, "without
#      placing texture behind the text");
#   2. glass is chrome, gated on GovernedCognition.glassEffective(), and an
#      exact visual no-op when off or when GC never arrives.
#
# The wiring is token-only like everything else in the sheet: the purity scan
# above already forbids color literals, and these forbid the two atmosphere
# literals it cannot see — a hard-coded asset url and a hard-coded blur.
# ---------------------------------------------------------------------------

GLASS_MARKER = "GLASS — the main-panel class"
GLASS_GATE = '[data-glass="on"]'


def _body_rule() -> str:
    """The body's PAINT rule — the sheet also opens with a shared
    `html, body` box-model reset that carries no background."""
    css = app_css()
    for match in re.finditer(r"\nbody \{([^}]*)\}", css):
        if "background" in match.group(1):
            return match.group(1)
    raise AssertionError("no body rule paints the page field")


def test_background_is_wired_over_the_color_and_honours_theme_hints() -> None:
    """SURFACES.md, "Wiring the background": the image layers OVER a
    background-color that always renders, and the per-theme size/repeat
    hints ride their own tokens rather than the app's guesses."""
    body = _body_rule()
    assert "background-color: var(--bg)" in body, (
        "the color must always render — a 404 on the image cannot cost the page"
    )
    assert "var(--page-bg-image, none)" in body, "background image layer missing"
    assert "var(--gc-bg-size, cover)" in body and "var(--gc-bg-repeat, no-repeat)" in body, (
        "per-theme background hints are published tokens, not app guesses"
    )
    # The veil is the doctrine's answer to a texture that competes, and it is
    # the theme's OWN surface color — never a literal wash.
    assert "--page-veil: color-mix(in srgb, var(--bg)" in app_css()
    # No asset url may be spelled out in the app layer: the theme owns which
    # file it ships, and the hash changes when the asset does.
    assert "url(" not in COMMENT_RE.sub("", app_css()), (
        "asset urls belong to the theme layer; the app resolves --gc-bg-image"
    )


def test_chart_bearing_planes_stay_opaque() -> None:
    """The chart-legibility law. .title-panel/.card are the planes every
    canvas, tile, donut and table sits on; over a textured field a
    translucent plane is texture behind the thing being parsed."""
    css = COMMENT_RE.sub("", app_css())
    rule = re.search(r"\n\.title-panel,\n\.card \{([^}]*)\}", css)
    assert rule, "the shared panel rule moved — re-pin this law"
    background = re.search(r"background:\s*([^;]+);", rule.group(1))
    assert background and background.group(1).strip() == "var(--panel)", (
        "chart/table planes must be a flat opaque token, got: "
        f"{background.group(1).strip() if background else None!r}"
    )


def _glass_section() -> str:
    body = app_css()
    marker = body.index(GLASS_MARKER)
    start = body.rfind("/*", 0, marker)
    return COMMENT_RE.sub("", body[start : body.index(SLANT_MARKER)])


def test_glass_targets_the_panel_class_and_stays_fully_gated() -> None:
    """Keeper-ruled 2026-08-30 (revised doctrine): glass targets a CLASS of
    panels, not one showpiece — here the shell's own top-level panels. And
    every rule in the section either carries the mount gate or styles an
    app-owned .glass-* element that exists only while the lens is mounted,
    so glass off is byte-identical to no glass at all."""
    section = _glass_section()
    assert ':is(.title-panel, .card)[data-glass="on"]' in section, (
        "glass must target the main-panel class, not a single surface"
    )
    # Split on rule boundaries rather than lines: the exclusion rule's
    # selector spans several lines and a line scan would skip it entirely.
    for chunk in section.split("}"):
        if "{" not in chunk:
            continue
        selector = " ".join(chunk.split("{")[0].split()).strip()
        if not selector or selector.startswith("@"):
            continue
        assert GLASS_GATE in selector or selector.startswith(".glass-"), (
            f"ungated glass selector (would paint with glass off): {selector!r}"
        )
    # The gate is the app's own mounted-flag. The estate attribute does not
    # know about reduced transparency, so the sheet may never key off it.
    assert 'data-gc-glass="on"' not in COMMENT_RE.sub("", app_css()), (
        "gate on the app's mounted-flag, not the raw estate toggle"
    )


def test_glass_panel_fill_rides_the_opacity_slider() -> None:
    """--gc-glass-opacity is what makes glass-over-content safe: the panel's
    wash is --gc-glass-tint, which derives from it, so the reader tunes
    legibility. The app may not substitute a hand-picked alpha of its own —
    that would freeze the slider out of the surface it governs."""
    section = _glass_section()
    fill = re.search(
        r':is\(\.title-panel, \.card\)\[data-glass="on"\] \{([^}]*)\}', section
    )
    assert fill, "the panel-class rule moved — re-pin this law"
    background = re.search(r"background:\s*([^;]+);", fill.group(1))
    assert background and background.group(1).strip() == "var(--gc-glass-tint)", (
        "the glass panel's fill is the slider-derived tint, got: "
        f"{background.group(1).strip() if background else None!r}"
    )
    assert "color-mix" not in fill.group(1), (
        "no app-side alpha on a glass panel — the slider owns that number"
    )


def test_chart_surfaces_stay_opaque_inside_a_glass_panel() -> None:
    """The limit the revised doctrine kept: 'Charts and dense numeric tables
    stay opaque planes: a canvas has no text the wash can rescue, and
    refraction across gridlines reads as damage.' The panel around them is
    glass; the reading surface inside them is not."""
    section = _glass_section()
    rule = re.search(
        r':is\(\.title-panel, \.card\)\[data-glass="on"\] :is\(([^)]*)\)\s*\{([^}]*)\}',
        section,
        re.S,
    )
    assert rule, "no explicit chart/table exemption inside the glass section"
    families = rule.group(1)
    for family in (".chart-wrap", ".subscription-quota-chart-wrap", ".window-card",
                   ".subscription-quota-history", ".fold"):
        assert family in families, f"{family} is a chart/table plane; exempt it"
    background = re.search(r"background:\s*([^;]+);", rule.group(2))
    assert background and background.group(1).strip() == "var(--panel)", (
        "an exempt plane must be flat and opaque, got: "
        f"{background.group(1).strip() if background else None!r}"
    )


def test_glass_mounts_off_glass_effective_and_never_the_raw_toggle() -> None:
    """SURFACES.md: 'Mount off glassEffective(), never glass().' The
    reader's reduced-transparency setting outranks the preference."""
    joined = "\n".join(source_files().values())
    assert "glassEffective()" in joined
    assert not re.search(r"\bgc\.glass\(\)", joined), (
        "reading the raw toggle would render glass for a reader who asked "
        "for reduced transparency"
    )
    # 2026-09-02 ("GC owns the library, the apps call it"): the knobs are
    # GC's end to end. The app reads none of them and draws nothing; it
    # names its class and calls glassAttach / glassDetach.
    for token in ("--gc-glass-blur", "--gc-glass-saturate", "--gc-glass-displace",
                  "--gc-glass-aberration"):
        assert token not in joined, f"{token} is GC's knob; the app must not read it"
    assert "glassAttach(" in joined and "glassDetach(" in joined
    # And the palette re-resolves on the glass event like it does on a theme
    # swap, so the charts never read stale tokens.
    assert "glassEvent" in source("hooks/usePalette.ts")


def test_the_app_never_restyles_the_estate_pickers_own_nodes() -> None:
    """Earned 2026-08-30: an app rule named .gc-switcher-glass landed on
    switcher.js's OWN glass-toggle fieldset and broke the picker's layout in
    both glass states. The app owns .gc-switcher-mount (its hole in the
    page) and nothing else under the gc-switcher- prefix."""
    css = COMMENT_RE.sub("", app_css())
    for selector in re.findall(r"\.gc-switcher[a-z-]*", css):
        assert selector == ".gc-switcher-mount", (
            f"{selector} belongs to GC's switcher.css, not this app"
        )


def test_liquid_glass_renderer_is_gcs_and_no_library_is_in_the_loop() -> None:
    """Keeper-ruled 2026-09-02: GC owns the renderer. The vendored loader
    carries glassAttach/glassDetach, the app's dependencies name no glass
    library, and neither built chunk carries the old renderer's markup."""
    import json

    manifest = json.loads((REPO_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert "liquid-glass-react" not in manifest.get("dependencies", {}), (
        "the renderer is GC's now; the app must not carry a glass library"
    )
    gc_dir = REPO_ROOT / "src" / "codex_usage_tracker" / "static" / "gc"
    loader = (gc_dir / "loader.js").read_text(encoding="utf-8")
    assert "glassAttach" in loader and "glassDetach" in loader, "re-vendor GC: the snapshot predates the renderer"
    assert ".gc-glass-layer" in (gc_dir / "glass.css").read_text(encoding="utf-8"), "re-vendor GC: glass.css predates the layer rules"
    assert "liquid-glass-react" not in "\n".join(source_files().values())
    assert "glass__warp" not in built_vendor_js(), "the old renderer is still in the vendor chunk"
    assert "glass__warp" not in built_app_js()
    assert "glassAttach(" in built_app_js(), "the app chunk must call GC's renderer"
