# React + Vite frontend conversion — 2026-08-30

The keeper ruled the estate is React + Vite. This converts the dashboard
frontend from the single ~4000-line Flask template
(`src/codex_usage_tracker/templates/index.html`, now deleted) to a React +
Vite app, like-for-like: same charts, same tiles, same fold law, same theme
contract. The Flask JSON API is untouched. Full suite:
`./.venv/bin/python -m pytest tests/ -q` → **549 passed** (was 548; the +1
is a new theme-contract test, see below).

## What moved where

| Was (templates/index.html) | Now |
|---|---|
| `<head>` (GC_CONFIG, GC loader, Chart.js vendor scripts, title/viewport) | `frontend/index.html` (Vite entry) → built `static/app/index.html` |
| `<style>` block 1 (app adapter layer, 1363 lines, incl. SLANT section) | `frontend/src/styles/app.css` — **byte-for-byte** (dedented 6 spaces) |
| `<style id="gc-type-bridge">` | `frontend/src/styles/type-bridge.css` — byte-for-byte |
| Body markup (hero/digest, unified card, subscriptions, billing) | `frontend/src/components/OpsHero.tsx`, `UnifiedUsageCard.tsx`, `SubscriptionsCard.tsx`, `BillingCard.tsx` (same ids, classes, aria wiring) |
| getCss/getColor probe, withAlpha, refreshPalette, rank/harness colors | `frontend/src/lib/palette.ts` |
| format helpers (formatTimestamp/UtcHour/percent/int/compact/apiModes/countdown) | `frontend/src/lib/format.ts` |
| provider rank slots, short labels, display names, aliases, card anchors | `frontend/src/lib/providers.ts` |
| dataset builders + the three chart options builders + replaceChartData | `frontend/src/lib/chart.ts` |
| quota helpers (confidence labels/dashes, normalized series, missing msg) | `frontend/src/lib/quota.ts` |
| staleLaneSuffix / ceded lanes | `frontend/src/lib/freshness.ts` |
| the four loaders, hydrate cycle, 60s + visibilitychange, last-good law | `frontend/src/hooks/useDashboardData.ts` |
| whenThemeReady + gc-theme-link reload path | `frontend/src/lib/palette.ts` + `frontend/src/hooks/usePalette.ts` |
| fold controller (localStorage prefs, ?expand, column tiers) | `frontend/src/hooks/useFoldState.ts` + `frontend/src/components/Fold.tsx` |
| fold summary derivation (was MutationObserver + cell scraping) | `tableFoldSummary` in `Fold.tsx`, computed from the same data the tables render (same output strings) |
| renderDonut / donutArcPath | `frontend/src/components/Donut.tsx` (JSX SVG) |
| reset rail + chip ticker (was 1s DOM ticker on `[data-reset-at]`) | `frontend/src/components/ResetRail.tsx` + `useNow.ts` (1s tick inside the leaf countdown component; still quiet when hidden) |
| provider status strip | `frontend/src/components/ProviderStatus.tsx` |
| renderDirectLabels / renderSeriesTable | `DirectLabels.tsx` / `SeriesTable.tsx` |
| chart instances by canvas id | `ChartCanvas.tsx` — Chart.js via refs, create-once/update-on-change, destroy on unmount |

Build: `frontend/` (`npm run build` = `tsc --noEmit && vite build`) outputs
to `src/codex_usage_tracker/static/app/` with hashed asset names, an app
chunk (`index-*.js`), a vendor chunk (`vendor-*.js`, React), and an
unminified stylesheet (`index-*.css`). **Build output is committed** so the
Python suite and installed wheels never need node. `dashboard.py`'s `/`
route now serves `static/app/index.html` (still `no-store`; the hashed
assets may cache — a rebuild changes the hashes, so caching can never hide
a deploy). `pyproject.toml` package-data ships `static/app/` and the GC
snapshot.

## Theme contract (survived, and now pinned)

- `/static/gc/loader.js` stays a classic synchronous `<head>` script with
  `data-gc-fallback="/static/gc"` — not bundled, not type=module. Vite
  leaves it untouched (its "can't be bundled" build warning is the desired
  behavior).
- Chart.js stays the vendored UMD classic script (`window.Chart` behind a
  typed guard) — never bundled, so no third-party color literals enter the
  app chunk.
- The picker mounts via `GovernedCognition.mountSwitcher("#gcSwitcher")`
  (typed against the re-vendored `loader.d.ts`).
- Theme swap: `usePalette` re-reads resolved custom properties and
  re-renders charts on `gc:apply`, `gc:source`, and the `#gc-theme` link's
  `load` (capture-phase — gc:apply fires before the sheet finishes
  loading, so the link load remains the guaranteed post-stylesheet hook).
  Difference from the old page: a theme swap now re-renders from cached
  state instead of re-running the whole hydrate fetch cycle (per the
  conversion mandate: re-read + re-render, not refetch).
- `withAlpha` (and barDataset's `${color}A8`) still construct rgba/hex
  alpha from RESOLVED token values only; zero color literals in app CSS,
  app TS/TSX, or the built app chunk.

## Every test change and why

New helper: `tests/frontend_surfaces.py` — accessors for the frontend
sources and built assets.

**tests/test_theme_conformance.py** — all 15 tests kept by name and
invariant, retargeted (each documents this in its docstring):
- style-block structure test → exactly two sheets imported in order, one
  built stylesheet, no third sheet.
- app-layer purity → `app.css` source AND built stylesheet (hex/rgb scans;
  font-family stays bridge-only in the source check since the bundle
  legitimately concatenates the bridge).
- bridge test → `type-bridge.css`.
- inline-JS purity → every `.ts/.tsx/.html` source AND the built app
  chunk; the vendor chunk (React) is exempt like the GC snapshot/Chart.js
  (documented in the module docstring).
- external-font ban → entry + built HTML + all sources + built assets.
- GC snapshot & slant-overlay tests → unchanged (same snapshot paths).
- the seven SLANT tests → same grammar checks against `app.css` (the sheet
  moved byte-for-byte, so `_slant_split` just reads the file).
- **NEW** `test_gc_loader_stays_a_classic_head_script` (count 15 → 16):
  pins the loader-as-classic-sync-head-script contract, data-gc-fallback,
  loader-before-bundle ordering, mountSwitcher, and the vendored Chart.js
  script tag in both entry documents.

**tests/test_dashboard_api.py** — 17 tests retargeted from served-HTML
greps to the React sources, each preserving its ruled invariant:
- overview/raw-ledger, fold-law, column-tier, fold-state tests → Fold
  component + useFoldState + per-card sources. The fold "tbodies ship in
  the DOM while folded" invariant is now "the Fold body renders children
  unconditionally" (asserted: no conditional render on `open`).
- codex-detail retirement → absences scanned across all sources AND the
  built app chunk; the survivors (`refreshUsage`, visibility-gated
  hydrate) asserted in their new homes.
- estate-charts, caption-diet, meter-honesty, stale-lane, authority/
  freshness, threshold-chain, group-seeding, mobile CSS, placeholder-path,
  tick-responsiveness, rank-palette, quota-history tests → the same
  strings/regexes against `SubscriptionsCard.tsx` / `UnifiedUsageCard.tsx`
  / `lib/*.ts` / `app.css`, with mechanical adaptations where the code
  moved to typed/JSX form (e.g. `subscriptionQuotaDatasets(palette,
  chartRows)`, `chartBucketLimit={48}`, `canvasId="..."`).
- alias parity still asserts the frontend map equals
  `dashboard._QUOTA_PROVIDER_ALIASES` (parsed from `providers.ts`).
- the `.innerHTML` ban translates to: no `.innerHTML`, no
  `dangerouslySetInnerHTML` anywhere in the frontend.
- `staleLaneSuffix(freshness)` count 4 → definition in `lib/freshness.ts`
  plus exactly 2 hook callers + the OpsHero mirror (same four surfaces).

**tests/test_multi_claude_instances.py** — 2 tests: the account-aware
color/dash helper greps moved to the frontend sources/built chunk; API
assertions unchanged.

**tests/test_unified_usage_api_cli.py** — 2 tests: stale-notice strings
now asserted in `useDashboardData.ts` (the API freshness assertions are
unchanged); the provider-neutral page test still hits `/` for what the
served shell truly carries (title, vendored Chart.js script tag, named
build asset) and reads the sources for panel wiring. Adaptations:
`typeof Chart === "undefined"` (the guard inverted direction),
`if (!document.hidden && booted.current) hydrate();` (first hydrate gated
on theme-ready), `Promise.allSettled` explicitly asserted.

## Known regressions / deliberate deviations (honest list)

1. **Theme swap no longer refetches.** Old: gc-theme link load re-ran the
   full hydrate (4 API calls). New: palette re-resolve + chart re-render
   from cached state (mandated). Data can be up to 60s older at the moment
   of a theme swap.
2. **Chart-failure fallbacks keep tables/labels.** Old: if a chart builder
   threw, its direct labels and fold table were skipped too (they rendered
   after the chart in the same try). New: labels and tables render from
   the model regardless; only the summary line swaps to the "chart
   unavailable" text. Strictly more information on failure.
3. **Reset-rail summary on total subscriptions failure** stays "Loading
   subscription windows…" (matches the old placeholder path, which never
   re-rendered the rail).
4. **`renderDirectLabels`' "(dashed)" label suffix** was dead code (no
   caller passed styles) and was not ported; dashed swatches for
   subscription confidence survive in `DirectLabels`' `dashed` prop.
5. **Chip role classes** are computed at hydrate/render time (as before);
   between hydrates only the countdown text ticks — unchanged behavior,
   noted because the mechanism moved from a DOM ticker to `useNow`.
6. **Fold summaries** are computed from the data arrays instead of
   scraping rendered cells via MutationObserver. Output strings are
   identical for every current fold (verified against the noun/metric
   specs each fold declared).
7. **`?expand=all`** still opens every fold for one load without writing
   prefs; because fold state is per-component React state, a fold toggled
   after load behaves identically to before.
8. **Stale-refresh behavior** for the unified panel matches the old
   showPanel path: a stale refresh hides the content (keeping last-good
   DOM hidden underneath) — not a regression, recorded for clarity.

## Verification

- Full suite: 549 passed.
- Scratch instance on `127.0.0.1:5199` against the real read-only ledgers,
  screenshotted with headless chromium (hero digest, tiles, both donuts,
  reset rail, status strip, all three unified charts, both quota history
  charts with confidence labels, all provider cards incl. balance/role
  accents, folds collapsed with live summaries, billing empty state).
- Live service `codex-dashboard.service` (port 5174) restarted and
  re-screenshotted serving the new build: `/tmp/ai-usage-shots/
  dashboard-live-5174.png` (plus `dashboard-react.png` /
  `dashboard-react-full.png` from the scratch run).
