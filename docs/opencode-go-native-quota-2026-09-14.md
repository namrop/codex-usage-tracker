# OpenCode Go native quota cutover (2026-09-14)

Keeper acceptance (Discord 1531875202860974264, message 1549262223074009089,
"Agreed. Implement it please"): the provider's own usage API is the OpenCode
Go quota authority; the tracker's first-use USD replay is retired from
collection and from every operator projection. Backend: `quota.py`
(reader), `collector.py` (lane), `dashboard.py` and `weekly_utilization.py`
(projections). Tests: `tests/test_opencode_go_native_quota.py` plus the
updated collector/dashboard/weekly pins. This note records the contract and
pre-activation verification; live activation/readback is a separate step.

## Why the estimate was wrong

The replay valued each `opencode-go` usage event at the recorded quota cost
(or list price) and anchored every window on first use. Go's shared
allowance actually consumes `round(cost × model.costMultiplier)`, the weekly
window is Monday 00:00 UTC and the monthly window is the subscription
anniversary (not a fixed 30 days). On 2026-09-14 the app showed 25.1 %
weekly while the provider reported 100 % (rate-limited). Those rows are not
allowance readings and are never shown again; they stay in the raw ledger
for audit.

## Reader

- `GET https://opencode.ai/zen/go/v1/usage`, header
  `Authorization: Bearer <OPENCODE_GO_API_KEY>`. No cookie, no workspace id.
  `OPENCODE_GO_API_KEY` is added to the collector's dotenv allowlist.
- Response `usage.{rolling,weekly,monthly}` → quota rows `five_hour`
  (`window_kind` rolling), `week` (fixed), `month` (fixed). Same namespace
  (`sol:opencode-go`), provider (`opencode-go`), harness (`opencode_go_api`)
  and account identity as the cookie reader, so the cutover continues one
  series per window and one account; only `x_source_surface` changes
  (`opencode_go_usage_api` vs the cookie reader's
  `opencode_go_server_function`).
- Unit and precision: `unit = percent`, `limit_value = 100`, `used_value` is
  the provider's `percent`, which the server floor-rounds and caps at 100.
  Rows carry `measurement_confidence = exact` (it is the provider's own
  reading) and `x_percent_resolution = 1` (whole points; no unrounded cost
  is claimed). A fractional percent is rejected as malformed rather than
  rounded.
- Status: `ok` → `x_provider_state = ok`; `rate-limited` → `rate_limited`.
  Anything else is malformed.
- Resets: `resetsAt` is the provider's absolute timestamp and is stored as
  given (millisecond precision). Meanings: rolling = the five-hour window's
  server-side first-use anchor plus five hours, and an idle account reads
  `now + 5h` without any cycle having opened, so the rolling row claims no
  `window_ends_at`; weekly = next Monday 00:00 UTC; monthly = next
  subscription anniversary. The weekly and monthly rows set
  `window_ends_at = resets_at`; `window_started_at` is never derived.
- Failures: HTTP status, transport, JSON decode, non-object payloads,
  missing windows, and bad `percent` / `status` / `resetsAt` types, values
  or ranges all raise a sanitized `ValueError` with no key, URL or provider
  message. The lane cedes and the ledger keeps its last good rows.

## Collector lane (`opencode_go_quota`)

The existing `quota_lanes` selector gates both native and cookie readers:
an unselected lane reports zero discoveries without probing. Timer definitions
are unchanged.

1. `OPENCODE_GO_API_KEY` present → native reader.
2. Otherwise `OPENCODE_GO_WORKSPACE_ID` + `OPENCODE_GO_AUTH_COOKIE` →
   the existing cookie reader.
3. Otherwise the lane reports `sources.opencode_go_quota = {discovered: 0}`
   without ceding or warning: unavailable, not estimated.

A failing reader cedes the lane (bounded retries; an empty result is
terminal without backoff) and nothing is written. The estimate is never a
fallback, and the quota scope no longer reads OpenCode databases or the
usage ledger at all. `quota.derive_opencode_go_quotas` remains as an
uncalled pure function for audit.

## Projections: append-only exclusion

`dashboard._is_retired_opencode_go_estimate(row)`: provider `opencode-go`
and (`measurement_confidence == estimated` or `unit == usd`). Applied in
`/api/subscriptions` (latest, observations, time series and both unified
cuts), `/api/quota-burn`, `/api/weekly-utilization`, and inside the pure
builders so a direct call cannot re-admit them. Cookie and native percent
rows are retained; other providers are untouched. Nothing in the ledger is
deleted, rewritten or pruned.

For burn rates only, a native reading starts a new source epoch after the
latest cookie observation. A native-labelled rate needs enough observations
within that native epoch; cookie-only rates and valid cookie history remain.

Account presence survives: the roster is numbered before the drop, so an
account whose only evidence is retired rows still appears (weekly window
`no_observations`, counted as excluded). Each payload reports what it left
out: `exclusions.retired_opencode_go_estimate_rows` on subscriptions and
burn, `coverage.retired_opencode_go_estimate_rows` on weekly.

**Superseded for the weekly panel (2026-09-25).** Weekly allowance use now
leaves Go out entirely, keeper message 1553002864555593851 in the unified
usage ledger thread: "I think opencode go should be omitted from the weekly
allowance use calculations. It's weird enough. Right now it says that I've
only used 47% when it's actually capped." Weekly reports
`coverage.omitted_provider_rows` instead of the retired-row count, and the
weekly paragraph below is historical. Subscriptions, burn and the Go card are
unchanged. See `weekly-utilization-semantics.md`.

Weekly: this installation has no eligible provider-observed Go completed cycle
yet. Once native readings straddle a Monday reset it can contribute. Until
then `completed` is null for Go and the aggregate is the mean
of contributing accounts (not 0 %, not the replay's 26.7 %). A Go-specific
note in `weekly.notes` states the exclusion, the history reset and the
whole-point resolution. Historical recovery (re-deriving past Go weeks
from provider data) is deferred; the verified native endpoint supplies current
window state, not historical cycles. Today's tariff is not applied as a
correction to past quota observations.

## Public metadata (additive)

Subscriptions `latest` / `observations` rows, chartable time-series
entries, burn series entries and `weekly.windows[].current` carry:

| field | values |
| --- | --- |
| `source_surface` | allowlisted reader tag (`opencode_go_usage_api`, `opencode_go_server_function`, `claude_oauth_usage_api`, `claude_code_cli_usage`, `z_ai_usage_quota`) or null |
| `percent_resolution` | `1` (whole points) or null |
| `provider_state` | `ok`, `rate_limited`, `inactive_or_not_reported`, or null |

Percentages keep their existing serialization (`100.0`, one-decimal
strings in weekly); where `percent_resolution` is 1 the UI should render an
integer. Aggregated means may still be decimals.

## Verification (worktree, 2026-09-14)

```bash
PYTHONPATH=$PWD/src /srv/pharos/repos/codex-usage-tracker/.venv/bin/python -m pytest -q
npm --prefix frontend test
npm --prefix frontend run build
```

Final integrated execution on September 15: **903 pytest tests passed**,
**87 Node tests passed**, TypeScript and Vite build passed. Counts are separate;
pytest includes a frontend-runner wrapper. A pre-existing file-mtime equality
test flaked once during integration and passed on the subsequent full run.

The real native reader returned three windows (0/100/78 percent for
five-hour/week/month). A sandboxed `collect_all(scope='quota')` invocation
with the existing API key fetched, appended and read back those three native
rows without invoking another provider or reading OpenCode usage databases.

Read-only candidate projection over real ledgers plus those captured provider
rows verified one Go account, no USD quota rows in the public cuts, no fabricated
completed cycle, and a recomputed completed aggregate over five accounts.
Desktop (1200px) and phone (390px) checks exercised Current/Completed and
Quota points/Tokens modes (eight cases), expanded model evidence, native integer
labels, Go exhaustion and no page-level horizontal overflow.

`/api/opencode-go-usage` uses the existing canonical side-effect-free JSONL
reader when applicable: no chmod or lock-file creation. SQLite reads retain
the existing integrity and bounded-query behavior. Tests cover safe JSON errors
for missing/non-regular/unavailable stores and combined row limits.

Model contribution and reconciliation details live in
`go-native-usage-explanation.md`. The frontend rejects malformed successful
payloads rather than dereferencing missing tariff/window objects.
