# OpenCode Go native usage explanation — payload contract

Keeper request 2026-09-14 (message 1549262223074009089): explain what
consumes the shared Go weekly allowance. Read-only, current UTC week only,
no history rebuild, no routing, no notifications.

## Pieces

| Piece | Owner | Path |
| --- | --- | --- |
| Pure projector | this task | `src/codex_usage_tracker/go_usage.py` (`build_go_usage`) |
| Projector tests | this task | `tests/test_go_usage.py` |
| Frontend helper + card + loader | this task | `frontend/src/lib/goUsage.ts`, `frontend/src/components/GoUsageCard.tsx`, `frontend/src/hooks/useDashboardData.ts`, `frontend/src/views/HistoryView.tsx`, `frontend/src/lib/views.ts`, `frontend/src/styles/app.css` |
| Frontend tests | this task | `frontend/tests/goUsage.test.ts`, `tests/test_go_usage_frontend.py` |
| Route `GET /api/opencode-go-usage` | dashboard | `src/codex_usage_tracker/dashboard.py`; tested with JSONL and SQLite |
| SPA rebuild (`static/app`) | frontend build | `src/codex_usage_tracker/static/app/` |

## Projector signature

```python
from codex_usage_tracker.go_usage import build_go_usage

payload = build_go_usage(usage_rows, quota_rows, generated_at=datetime_utc)
```

* `usage_rows`: canonical, validated `usage_event_v1` rows for provider
  `opencode-go`. The route may over-supply (whole last 168h is fine); the
  projector keeps only rows whose `occurred_at` is inside the current UTC
  week (Monday 00:00 UTC) and not after `generated_at`, de-duplicates on
  `(source_namespace, source_event_id)`, counts corrections with their
  signed tokens, and counts every drop reason.
* `quota_rows`: `quota_observation_v1` rows for provider `opencode-go`
  (latest 168h or the current week). The projector keeps only
  `quota_name == "week"`, `unit == "percent"`,
  `x_source_surface == "opencode_go_usage_api"`, observed inside the
  current week and not after `generated_at`, with an integral
  `used_value` in 0..100, then takes the latest by `observed_at`. The
  cookie-backed `opencode_go_server_function` surface is ignored.
* `generated_at`: timezone-aware `datetime`; `ValueError` otherwise.
* Never raises on missing native, unknown models, empty inputs or
  malformed rows; the output is always JSON-serializable (Decimal
  strings, ints, bools, ISO-8601 `Z` timestamps).

## Integrated route

`GET /api/opencode-go-usage` reads both canonical ledgers with an explicit
`provider=opencode-go` filter and the current UTC-week start / inclusive-now
time bounds. It retains the private API integrity checks and combined row cap.
JSONL reads use the canonical side-effect-free reader, not the lock/chmod reader.
Missing stores return JSON 404, unsupported suffixes 400, excessive rows 413,
and malformed/non-regular/unavailable stores 503. The route makes no provider
calls and does not write either ledger.

The frontend loader treats **404** as "not configured"
and says so; any other non-2xx or a body failing `isGoUsagePayload`
reads "unavailable" (or "showing last good data" after a first success).
The same private-read guards as `/api/weekly-utilization` apply (the
projector prints no `x_*`, `*_ref`, namespace or identity fields, so the
response is safe to serve as-is).

## Payload

```jsonc
{
  "generated_at": "2026-09-15T03:39:00Z",
  "window": { "started_at": "2026-09-14T00:00:00Z", "ends_at": "2026-09-21T00:00:00Z", "as_of": "2026-09-15T03:39:00Z" },
  "account": { "provider": "opencode-go", "label": "OpenCode Go", "provider_account_index": 1, "provider_account_count": 1 },
  "native": {
    "percent": 24,                 // int 0..100 or null (missing); NEVER 0 for missing
    "status": "ok",                // x_provider_state passthrough ("ok" | "rate_limited") or null
    "observed_at": "2026-09-15T02:50:00Z",
    "resets_at": "2026-09-21T00:00:00Z",
    "fresh": true,                 // age < 3h
    "age_seconds": 2940,
    "capped": false,               // percent >= 100
    "percent_resolution": 1,
    "source_surface": "opencode_go_usage_api",
    "basis": "provider_reported"
  },
  "models": [                      // priced rows first by quota units desc, then unpriced by tokens desc, then name
    {
      "model": "glm-5.3",
      "event_count": 3, "correction_count": 0, "missing_token_event_count": 0,
      "input_tokens": 1000000, "cache_read_tokens": 0, "cache_write_tokens": 0, "output_tokens": 100000,
      "total_tokens": 1100000,     // sum of the four buckets; reasoning NOT added
      "reasoning_tokens": 20000,   // diagnostic only
      "multiplier": "4",           // null when unpriced
      "estimated_list_cost_usd": "1.84000000",   // 8 dp; null when unpriced
      "estimated_quota_units": "7.36000000",     // list cost × multiplier; null when unpriced
      "estimated_quota_pct": "24.533",           // units / 30 × 100, 3 dp; null when unpriced
      "share_of_modeled_pct": "100.000",         // share of the priced total; null when unpriced
      "pricing_status": "priced",                // "priced" | "unpriced"
      "basis": "current_tariff_estimate"
    }
  ],
  "totals": {
    "event_count": 4, "priced_event_count": 3, "unpriced_event_count": 1,
    "model_count": 2, "priced_model_count": 1, "unpriced_model_count": 1,
    "input_tokens": 1000010, "cache_read_tokens": 0, "cache_write_tokens": 0, "output_tokens": 100005,
    "total_tokens": 1100015, "reasoning_tokens": 20000,
    "estimated_list_cost_usd": "1.84000000",   // sums of the priced rows exactly as printed
    "estimated_quota_units": "7.36000000",
    "estimated_quota_pct": "24.533",
    "weekly_limit_quota_units": "30",
    "basis": "current_tariff_estimate"
  },
  "reconciliation": {
    "status": "within_rounding",   // native_missing | native_stale | native_capped | modeled_incomplete | usage_ahead_of_native | within_rounding | modeled_below | modeled_above
    "native_pct": 24,
    "native_lower_pct": "24",      // band [p, p+1) below 100; "100" with upper null when capped
    "native_upper_pct": "25",
    "modeled_pct": "24.533",
    "modeled_quota_units": "7.36000000",
    "modeled_complete": true,      // false when any unpriced event or missing token field
    "latest_usage_at": "2026-09-14T12:00:00Z",   // occurred_at of the latest counted usage row; null when none
    "native_observed_at": "2026-09-15T02:50:00Z", // same as native.observed_at; null when missing
    "usage_ahead_of_native": false, // any counted row strictly after native_observed_at; null when native is missing
    "matches_within_rounding": true,   // null unless status is within_rounding / modeled_below / modeled_above
    "band_gap_pct": null,          // signed distance to the band edge when outside it; null otherwise
    "native_basis": "provider_reported",
    "modeled_basis": "current_tariff_estimate"
  },
  "coverage": {
    "source_usage_rows": 7, "usage_rows_in_window": 4, "duplicate_rows": 1, "correction_rows": 0,
    "missing_token_rows": 1, "unpriced_events": 1, "unpriced_models": ["mystery-model"],
    "latest_usage_at": "2026-09-14T12:00:00Z",   // null when no row counted
    "usage_rows_after_native": 0,                // counted rows after native.observed_at; null when native is missing
    "ignored_usage_rows": { "malformed": 0, "other_fact_type": 0, "other_provider": 0, "invalid_timestamp": 0, "before_window": 2, "future": 0 },
    "source_quota_rows": 5, "native_candidates": 3,
    "ignored_quota_rows": { "malformed": 0, "other_fact_type": 0, "other_provider": 0, "other_quota_name": 2, "other_source_surface": 0, "other_unit": 0, "invalid_timestamp": 0, "before_window": 0, "future": 0, "missing_percent": 0, "non_integer_percent": 0, "out_of_range": 0 }
  },
  "tariff": {
    "as_of": "2026-09-15T03:31:00Z",
    "source_url": "https://opencode.ai/docs/go/#usage-limits",
    "basis": "current_tariff_estimate",
    "weekly_limit_quota_units": "30",
    "models": { "glm-5.3": { "input_usd_per_m": "1.40", "output_usd_per_m": "4.40", "cache_read_usd_per_m": "0.26", "cache_write_usd_per_m": "0", "multiplier": "4" } }
  },
  "notes": [ "..." ]               // basis, floor/cap, tariff caveat, reasoning caveat, plus conditional coverage notes
}
```

## Semantics

* Native percent is the provider's figure: `floor(min(100, used / limit × 100))`,
  `100` when rate-limited. Below 100 it stands for `[p, p+1)`; at 100 it
  means "at least 100%" and no exact remainder is claimed.
* Modeled points: canonical tokens × current tariff per bucket → list
  cost; × model multiplier → quota units; ÷ 30 × 100 → points. Decimal
  arithmetic throughout. Reasoning tokens are never priced or added.
* Rows are never rescaled to the native total; unknown models stay
  unpriced with their tokens counted; missing token fields are counted,
  not zeroed.
* Reconciliation status is decided in priority order: `native_missing`,
  `native_stale`, `native_capped`, `modeled_incomplete`,
  `usage_ahead_of_native`, then the band test (`within_rounding` /
  `modeled_below` / `modeled_above`). Only the band test sets
  `matches_within_rounding` and `band_gap_pct`; every other status leaves
  both null.
  * `modeled_incomplete`: any unpriced event or missing token field. The
    priced figure is still shown, but it is not the whole week, so no
    match or gap is asserted.
  * `usage_ahead_of_native`: a counted usage row occurred strictly after
    `native.observed_at`. Model rows and totals still run through
    `generated_at` (activity is never discarded); the note names both
    cutoffs. Rows after `generated_at` are `future` and are never counted
    as post-native activity. Capped native stays `native_capped` ("at
    least 100") even when usage runs ahead; `usage_ahead_of_native` is
    still reported as a fact.
  * The band test compares the unrounded quota units (tokens × 2-dp rate
    × multiplier, exact in Decimal) against `p × 30 / 100` and
    `(p+1) × 30 / 100`, lower-inclusive, upper-exclusive. The 3-dp
    `modeled_pct` / `estimated_quota_pct` are display-only and may print
    on the band edge (e.g. 1,017,835 glm-5.3 input tokens = 18.99958…%
    prints `19.000` but sits inside `[18, 19)`). No Decimal reaches the
    payload.
* The legacy `x_opencode_quota_cost_usd` field is unweighted local cost
  under a misleading name and is never used.
* The tariff lives in `go_usage.py` (not `list_pricing.py`) because a
  financial list-price equivalent and a quota-unit debit are different
  quantities. `tariff.as_of` / `source_url` travel with every payload.

## Verification

```bash
cd /srv/pharos/workspaces/ai-usage-go-explanation
PYTHONPATH=$PWD/src /srv/pharos/repos/codex-usage-tracker/.venv/bin/python -m pytest tests/test_go_usage.py tests/test_go_usage_frontend.py tests/test_weekly_utilization_frontend.py tests/test_theme_conformance.py tests/test_capacity_navigation.py -q
cd frontend && npm test && npx tsc --noEmit
```
