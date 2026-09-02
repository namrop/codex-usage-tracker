# Backgrounds and glass in the AI Usage dashboard

What this app did with the two atmospheric surfaces Governed Cognition ships —
the theme **background** (`--gc-bg-image`) and **liquid glass** — and what it
refused to do. The doctrine is
`/srv/pharos/workspaces/governed-cognition/SURFACES.md`; this file records the
consumption, the findings that cost real time, and the honest limits.

2026-08-30. The GC snapshot was re-vendored **twice** in this round (two
commits, no app changes in either): once for the namrop theme, the background
assets and the glass runtime, and again for the **opacity slider**, which
landed mid-round and changed the design.

---

## Background

### Wiring

`frontend/src/styles/app.css`, the `body` rule, layered top-to-bottom exactly
as the doctrine's snippet prescribes:

1. the two structure-role grid gradients (pre-existing, unchanged),
2. **the veil** — a flat wash of the theme's own `--bg`,
3. **the image** — `var(--page-bg-image, none)`,
4. over `background-color: var(--bg)`, which always renders.

Per-theme hints ride their published tokens: `background-size` takes
`var(--gc-bg-size, cover)` and `background-repeat` takes
`var(--gc-bg-repeat, no-repeat)`, so johannah tiles at its own 650px and the
dark themes are single `cover` scenes without the app knowing which is which.
The image and its veil are `background-attachment: fixed`; the page scrolls
long, and a tiled toile dragging with the panels reads as a moving floor.

Zero literals: the veil is
`color-mix(in srgb, var(--bg) var(--page-veil-strength), transparent)`, and no
asset url appears in the app layer at all (a test pins that).

### FINDING 1 — a relative url in a custom property does not resolve where the doctrine says it does

`theme-*.css` publishes `--gc-bg-image: url(assets/namrop-bg-*.webp)` with the
comment "Relative URL — resolves beside this stylesheet". **It does not.**
Chromium resolves a relative `url()` carried in a custom property against the
stylesheet where the `var()` is **used**, not the one that declared it. So
`background-image: var(--gc-bg-image)` written in this app's bundle resolved to
`/static/app/assets/assets/namrop-bg-*.webp` and 404'd.

The layering the doctrine prescribes is what made this *silent*: a 404 falls
through to `--gc-bg`, so the page looked exactly as it had before and the
treatment simply never appeared. It was only visible by reading
`getComputedStyle(document.body).backgroundImage` out loud in a live browser.

The fix is app-side and token-shaped (`frontend/src/lib/background.ts`): read
the token, resolve it against the `href` of GC's own managed `#gc-theme` link
— origin-served or vendored snapshot, whichever the loader landed on — and
republish it as `--page-bg-image` for the sheet to consume. It re-runs on
`gc:apply`, `gc:source` and the theme link's `load`, because a source flip
*moves the base the asset resolves against*. No color crosses that boundary,
only a URL.

**Worth carrying upstream.** Every consumer that writes the doctrine's snippet
in its own bundle will hit this, and every one of them will hit it silently.
Either GC emits absolute urls, or the doctrine gains a line saying the consumer
must resolve them.

### FINDING 2 — the base panels had to go opaque

`.title-panel, .card` was `color-mix(in srgb, var(--panel) 92%, transparent)`.
Over a flat color that 8% cost nothing. Over an image it is texture behind
every chart, tile and table on the page. Both are now `background: var(--panel)`,
flat and opaque, and a test pins it — that is the **glass-off** state, and it
stays honest regardless of what glass does on top of it.

### Per-theme findings

| theme | veil | verdict |
|---|---|---|
| **johannah** | **62%** | The one theme that needed its own number. Its background *is* the jlmu.love toile — a busy mid-contrast floral on cream, not a dim scene — and at the dark themes' 30% it competed with the card edges. At 62% it reads as paper grain in the gutters and never approaches a number. Panels are opaque cream; charts, legends and axis labels are as crisp as before the background landed. Glass on johannah is a total no-op by design (below). |
| **namrop** | 30% | The strongest showing. The lighthouse/compass scene is genuinely visible in the gutters and, with glass on, *through all four panels*; the slant cuts read against it. |
| **governed-cognition** | 30% | Faint blueprint arcs; present without asserting. Reads well through the panel lenses. |
| **neo-andean** | 30% | Dimmest of the four — the asset is near-black geometry, so the effect is a low tonal shift rather than a scene. Honest, not broken; nothing to fix at the app layer. |

Chart legibility across all four: unchanged. Bars, axis ticks, donut center
values, legend swatches, status cells and the reset chips all sit on flat
opaque planes and none of them touch the field.

---

## Glass

### The class: the main panels

**Keeper-ruled 2026-08-30, mid-round.** The first pass of this work followed
the original doctrine — glass is chrome, so the app gave it one masthead — and
the keeper called that too narrow: glass should target a *class of panels*. For
ai-usage the named target is **the main panels**.

`frontend/src/components/GlassPanels.tsx` mounts a lens on every top-level
panel of the shell — `.shell > .title-panel` and `.shell > .card`, which today
is the masthead plus **Unified usage**, **Subscriptions** and **Billing**. It
does that by portal rather than by threading a prop through four components:
the panels *are* the class, and none of them should have to know glass exists.
A `MutationObserver` on the shell picks up any panel that arrives later.

The style layer (`app.css`, the GLASS section) does the rest:

```
:is(.title-panel, .card)[data-glass="on"] {
  position: relative;
  background: var(--gc-glass-tint);   /* the slider */
  border-color: transparent;          /* the lens draws its own edge */
  box-shadow: none;                   /* and its own shadow */
}
```

`data-glass="on"` is written **only** by the component that already checked
`GovernedCognition.glassEffective()` — never `.glass()` — so a reader with
`prefers-reduced-transparency` outranks the toggle, and every rule in the
section is dead when glass is off or GC never arrives.

### The slider is what makes it safe

The panel's fill is `var(--gc-glass-tint)`, which derives from
`--gc-glass-opacity`, which the loader publishes onto `<html>` from the picker.
**The app picks no number.** At 0 the panels are near-pure refraction over the
theme scene; at 1 they are effectively the opaque panels they were before. A
test forbids the app from substituting an alpha of its own, because that would
freeze the slider out of the surface it governs.

Verified live: `GovernedCognition.glassOpacity()` reads back, the estate
settings endpoint round-trips `glassOpacity`, `--gc-glass-opacity` lands
inline on `<html>`, and `--gc-glass-tint` resolves through it. Screenshots at
0.05 and 0.90 on namrop and governed-cognition show the range — 30% of the
viewport's pixels move between the two.

**At the default 0.35 nothing on this page is unreadable.** The stat tiles,
digest tiles, reset chips and card headers all carry their own opaque-ish
fills (70–76% washes of `--panel-soft`) *on top of* the panel wash, so their
text sits on roughly 0.8 effective opacity even at the default. The one
surface that gets thinner than the rest is the chart legend column
(`.direct-labels`), which sits beside the chart plate rather than on it — at
0.35 it is comfortably legible on all three dark themes; below about 0.15 on
namrop it starts competing with the lighthouse scene. That is exactly what the
slider is for, and the default was not touched.

### The two limits that still hold

**Chart surfaces and folded numeric tables stay opaque planes inside the glass
panel.** A canvas draws on transparent pixels, so without this the gridlines,
bars and axis ticks would sit directly on refraction — the damage the doctrine
names. The exempt families, all forced to a flat `var(--panel)`:

| family | what it carries |
|---|---|
| `.chart-wrap` | the unified Chart.js canvases |
| `.subscription-quota-chart-wrap` | the two subscription utilisation canvases |
| `.window-card` | the throughput donuts, the reset rail, the provider status heat-strip |
| `.subscription-quota-history` | the history chart plates |
| `.fold` | the folded hourly tables |

The panel around them is glass; the reading surface inside them is not.

**johannah gets no panel glass at all.** Ruled: its reading planes are opaque
paper because that is what the source site authored. The gate is a
`--gc-scheme` read — the theme's own declaration of light vs dark — not a
hard-coded theme name, so a future light theme inherits the rule for free.

### FINDING 3 — liquid-glass-react's frost does not render on current Chromium

The library puts `filter: url(#…)` (its displacement chain) and
`backdrop-filter: blur() saturate()` on the **same element**. Per Filter
Effects an element with a `filter` becomes a *backdrop root*, so its own
backdrop is empty — and Chromium 148 implements exactly that. Verified three
ways on this host, headless and with SwiftShader alike:

- a plain `backdrop-filter: blur(14px)` div over the page: **blurs**;
- the same div with the library's filter added: **paints nothing at all**;
- the same div with its own background plus the filter: paints the background,
  **backdrop unblurred**.

SURFACES.md already warns the library is 14 months idle and that Safari and
Firefox lose the displacement. This is worse and newer: on the one browser the
doctrine says it works, the *frost* is gone too.

Left unaddressed this is not cosmetic. A "glass" panel with no frost is merely
a **transparent** panel, and on a content class that means the page behind it
reads straight through the numbers.

So the app paints the frost itself: one element below the lens with no
`filter` of its own, using the library's own published scale — the one
`glass.css` documents in the same breath as the token,
`blur = (4 + blur*32)px` (`12 + …` over light). Both numbers come from the
estate; only the element they live on moved. The lens still draws on top and
still contributes its edge highlights and whatever refraction Chromium gives
it. `frontend/src/lib/glass.ts` carries the finding at the code.

### FINDING 4 — the estate picker's panel is not a glass surface here (historical)

Under the *original* doctrine (chrome only) the picker panel was the natural
second surface, and it was fully built and screenshotted before being struck.
It fails for a reason specific to this page: **the panel opens directly over
the caption text of the card beneath it.** The gear sits at the bottom-left of
the masthead, so GC positions the panel across the "Unified usage · 7 days"
heading and its two-line caption. At the estate's knobs — 6.5px frost, a 35%
tint — the paragraph behind stayed legible *through* the radio labels;
`johannah` was buried under "Unified usage · 7 days" and `namrop` collided
with the caption.

The revised doctrine makes this moot — the picker is not in the named class,
and the panel is GC's own node — but the finding stands if anyone tries it:
**a floating panel over dense body text is the worst backdrop on the page**,
and it would need an opacity near 1 to work, at which point it is not glass.
The code was removed rather than left dark. GC's picker now renders exactly as
GC ships it, in both glass states.

That round also produced the one bug worth a permanent test: an app rule named
`.gc-switcher-glass` landed on switcher.js's **own** glass-toggle fieldset —
the class already existed inside the panel — and broke the picker's layout in
*both* glass states, silently, because the class exists whether or not the app
mounts anything. `test_the_app_never_restyles_the_estate_pickers_own_nodes`
now forbids the app any `.gc-switcher*` selector except its own mount hole.

### No-op verification

- **Glass OFF vs ON, johannah**: pixel-diff across the full 1600×1100 viewport
  is **0.0%**. Exact visual no-op, still true after the class widened.
- **Glass OFF vs ON, dark themes**: 66% of the viewport differs on namrop and
  governed-cognition, 30% on neo-andean (whose asset is dimmest) — that is the
  four panels, as intended.
- **GC absent**: `glassEffective()` returns false whenever
  `globalThis.GovernedCognition` is missing or throws, so no lens mounts, no
  `data-glass` attribute is written, and every CSS rule in the glass section is
  gated on that attribute. Pinned by test rather than screenshot, since a
  GC-absent page has no theme at all.

### Palette path

`usePalette` re-resolves on `gc:glass` alongside `gc:apply`/`gc:source` and the
`#gc-theme` link's load. Toggling glass (or moving the slider) attaches or
drops `glass.css` and flips `html[data-gc-glass]`, so it is a token-layer
change like any other; re-reading is cheaper than reasoning about which tokens
the toggle can reach. The charts' existing "re-read resolved tokens and
re-render" path is otherwise untouched.

---

## Test changes

`tests/test_theme_conformance.py` gains eight tests (549 → **557**). They pin
the laws that are about reading, plus the mistakes that were actually made:

| test | why |
|---|---|
| `test_background_is_wired_over_the_color_and_honours_theme_hints` | the color must always render under the image; the size/repeat hints must ride their published tokens; **no asset url may be spelled out in the app layer** — the theme owns which file it ships and the hash changes when the asset does |
| `test_chart_bearing_planes_stay_opaque` | the glass-**off** state: the base panel rule stays flat and opaque, pinned at the exact rule that was 92%-translucent before this round |
| `test_glass_targets_the_panel_class_and_stays_fully_gated` | the keeper's ruling, made structural: glass targets `:is(.title-panel, .card)`, not one surface; and every selector in the section carries the mount gate or names an app-owned `.glass-*` element, so glass off is byte-identical to no glass |
| `test_glass_panel_fill_rides_the_opacity_slider` | the panel's wash is `var(--gc-glass-tint)` and nothing else — no app-side `color-mix`, because the slider owns that number and a hard-coded alpha would freeze the reader out |
| `test_chart_surfaces_stay_opaque_inside_a_glass_panel` | the limit the revised doctrine kept: the five chart/table families are exempted by name and forced to a flat opaque plane |
| `test_glass_mounts_off_glass_effective_and_never_the_raw_toggle` | the doctrine's loudest rule, plus: all four prop-feed tokens are actually read as numbers, and `usePalette` subscribes to the glass event |
| `test_the_app_never_restyles_the_estate_pickers_own_nodes` | **earned** — see Finding 4 |
| `test_liquid_glass_renderer_is_exact_pinned_and_stays_third_party` | exact-pin as the doctrine requires, and the renderer stays in the vendor chunk so the app chunk keeps the zero-color-literal law it could never satisfy |

No existing test was changed or relaxed. The purity scan already forbids color
literals; these forbid the two atmosphere literals it cannot see — a
hard-coded asset url and a hard-coded wash.

---

## Honest limitations

- **Four lenses is four filters.** With the class widened, the page now paints
  four `backdrop-filter` planes and four SVG displacement filters instead of
  one. No stutter was observed in headless capture or on the live page, but
  this has not been profiled on a loaded machine, and the dashboard re-renders
  Chart.js on a 60s hydrate. If it ever bites, the cheapest fix is dropping the
  library and keeping the frost planes.
- **The library's displacement is mostly theoretical here.** With the frost
  moved out from under its filter, what the library still contributes is its
  edge highlight, its shadow, and whatever refraction Chromium gives the
  displacement chain — which, given Finding 3, may be little. The lenses read
  correctly as lenses because of the frost the app paints.
- **Slant cuts are not followed.** The library draws its own rounded-rect
  geometry, so under `slanted` a lens's corners are square where the panel's
  are cut. `cornerRadius` is read from the host's *resolved* radius, so the
  mode-wide `border-radius: 0` reaches it, but the polygon cuts cannot. Known
  and accepted per the doctrine's own limitations list. Visible on namrop.
- **Safari and Firefox**: unverified. The app-painted frost is plain
  `backdrop-filter`, which both support, so those browsers should degrade to
  frosted glass rather than to nothing — better than the library's own
  behaviour, but untested.
- **Screenshots are headless Chromium 148 at 1600×1100**, one viewport,
  `pngquant`-compressed. Narrow/mobile breakpoints were not re-shot; the
  wiring is breakpoint-independent, but the masthead's two-column grid
  collapses under 900px and the lenses have not been eyeballed there.
- **The estate settings endpoint is shared.** Another lane was flipping
  `/settings` on :7312 during this round; every capture therefore re-asserts
  the selection and *verifies the rendered document* before shooting, retrying
  on a race. Several retries fired.
- **`background-attachment: fixed`** is a known repaint cost on long scrolls
  and is ignored on iOS Safari. Accepted for a single-operator desktop
  dashboard.
- **Stat tiles were left alone.** The revised doctrine excludes charts and
  dense numeric tables, not tiles, so `.metric` and `.digest-item` keep their
  existing translucent fills and ride the slider like the panel does. At 0.35
  they read cleanly; a reader who wants them harder has the control.

---

## Screenshots

`docs/surfaces/`, live service on :5174 with the estate theme set through
:7312 and the rendered document verified before each shot.

Per theme, `-top` (masthead + stat row + donut), `-charts` (scrolled to the
Chart.js surfaces), `-picker` (estate picker open):

```
docs/surfaces/governed-cognition-glass-{on,off}-{top,charts,picker}.png
docs/surfaces/johannah-glass-{on,off}-{top,charts,picker}.png
docs/surfaces/namrop-glass-{on,off}-{top,charts,picker}.png
docs/surfaces/neo-andean-glass-{on,off}-{top,charts,picker}.png
```

The slider's range, both at glass ON:

```
docs/surfaces/namrop-glass-on-opacity-{005,090}-top.png
docs/surfaces/governed-cognition-glass-on-opacity-{005,090}-top.png
```

The johannah pair is the no-op evidence; the namrop pair is the clearest
reading of the lenses; every `-charts` shot is the chart-legibility evidence.

---

## 2026-09-02 — GC owns the renderer

Keeper-ruled: "GC owns the library, the apps call it." `liquid-glass-react`
left `dependencies`; `components/GlassLayer.tsx`, the knob reads and the
frost shim in `lib/glass.ts` are gone. `GlassPanels` still names the class
(`.shell > .title-panel, .shell > .card`) and still writes the app's own
`data-glass` mounted-flag for the style layer, but it hands each panel to
`GovernedCognition.glassAttach()` and takes it back with `glassDetach()`;
GC mounts one `.gc-glass-layer` span per panel with the whole effect on that
element, sizes a lens to it, and — under the slanted shape — takes the
panel's cut polygon onto the layer (a clip on the panel would blind the
filter). The johannah panel exception is withdrawn by the same day's ruling:
a paper theme's panels glass like every other's, and the slider decides.
The GC snapshot under `static/gc/` was refreshed to the dist that carries
the renderer. Verified in headless Chromium against `:5174` on namrop /
slanted: four panels carry a GC layer, all four clip-handed, lens on the
layer, no old plate markup.
