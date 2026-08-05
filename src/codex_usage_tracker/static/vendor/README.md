# Vendored Chart.js

- Package: Chart.js
- Version: 4.5.1
- Upstream: https://www.chartjs.org/
- Retrieved from: https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js
- SHA-256: `48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a`
- License: MIT (license header retained in `chart.umd.min.js`)

The dashboard serves this local copy so provider/model charts do not depend on a browser reaching a third-party CDN.

## Bounded Bklit design-reference provenance

Bklit commit `c57f66bfa7c3198edb677b567ce08cbf364ae159` was reviewed on `2026-07-28` as a design reference only. The review was bounded to the `line-chart` and `live-line` registry closures and informed the dashboard’s lifecycle handling for dynamic line-chart instances.

No runtime code or dependency was adopted from Bklit. Vendored Chart.js remains the dashboard charting runtime.

The Shai-Hulud check performed during that review was incident-specific. It is recorded as review context, not as a universal or future safety guarantee for Bklit, its dependency graph, or later revisions.
