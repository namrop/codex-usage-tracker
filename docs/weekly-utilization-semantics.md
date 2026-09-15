# Weekly allowance utilization: `GET /api/weekly-utilization` (2026-09-14)

Keeper request (Discord 1531875202860974264, message 1549243774775853067):
"what percentage of my weekly windows I am using ... how much I am leaving on
the table, aggregate, and per provider". This is a display metric over the
quota ledger's existing observations: no routing, no policy, no new storage,
cron or rollup. Backend: `src/codex_usage_tracker/weekly_utilization.py`
(pure projection) and the route in `dashboard.py`. Tests:
`tests/test_weekly_utilization.py`.

## Request

`GET /api/weekly-utilization?weeks=N` — private dashboard only. `weeks`
defaults to 4 and accepts 1..12 (`400` otherwise). The read covers
`7 × (weeks + 1)` days (`lookback_days`) so the oldest requested cycle has
its start, through the existing bounded `_private_facts` reader (SQL bound
plus the request-wide row cap → `413`; malformed ledger → `503`;
unconfigured or missing ledger file → `404`). Future-dated facts are cut at
`generated_at`, exactly as `/api/subscriptions` does.

## Which windows

- One **primary** weekly quota per account, from the dashboard's existing
  `_PRIMARY_WEEKLY_QUOTAS`: `openai-codex/week`, `anthropic/seven_day`,
  `opencode-go/week`, `z-ai-glm/week`, `kimi-k3-coding/week`. Provider
  aliases resolve through the existing canonical map.
- Any other quota of the same family (`seven_day_fable`, any
  `<primary>_<model>`) is a **`model_cap`** row: shown, never in a
  denominator. Five-hour windows, balances and monthly tool quotas are out.
- A window is keyed by provider, quota name and private account identity;
  harness/scope/unit variants of the same account window merge into it, so
  one account is never counted twice.
- **Account roster.** Ordinals (`provider_account_index` of
  `provider_account_count`) come from the same 168-hour subscriptions roster
  every other surface uses. Observations from accounts outside that roster
  are ignored (counted in a note), never inserted, so a retired or quiet
  account cannot renumber `Claude 2`. Every tracked weekly roster account gets a primary
  window row even without data (`status: no_observations`). The public
  `label` is the frontend's short name plus ordinal when a provider has more
  than one account (`Claude 1`), bare otherwise (`Codex`). No account
  reference, source namespace or payload path is serialized.

## Current

`window.current` is the latest observation of that window with a usable
ratio (`used_value / limit_value`, the existing helper). `status` and
`eligible`:

| status | eligible | meaning |
| --- | --- | --- |
| `fresh` | yes | observed within the existing 3h freshness threshold, with no known reset since that observation |
| `stale` | no | latest usable reading older than 3h |
| `reset_passed_since_observation` | no | the reading's own reset has passed since it was taken |
| `no_utilization_ratio` | no | observations exist but none carries a usable ratio (e.g. `inactive_or_not_reported`) |
| `no_observations` | no | roster account with no weekly observation in the read |

`remaining_pct = 100 − used_pct`. Excluded windows keep their values and are
counted in `excluded_account_count`; they are never read as 0% used.

## Completed cycles

Reset periods are **inferred from observed reset targets**, not calendar
weeks. Walking one window's observations in order, consecutive samples whose
`resets_at` lie within an absolute 5-minute tolerance of the cluster's first
target form one cycle (targets flap by seconds across minute/day
boundaries). A null-reset sample joins the open cycle only while that
boundary is still ahead, so a mid-window counter drop it carries is still
seen, and a null reset alone never creates a cycle.

Each cycle reports the **last valid sample observed at/before its target**
(`used_pct`, `last_observed_at`, `last_sample_seconds_before_reset`, that
sample's `confidence`). It is a polling estimate, "observed at reset", not
the provider's terminal figure. Samples observed after the target — still
carrying the old target for a few minutes, or a null target — belong to the
next window: they never replace the last pre-reset reading, are not scanned
for counter drops, and are never synthesized into one. A cycle is
`eligible` only when all hold:

- a later cluster exists and its first observation is at/after this cycle's
  target (5-minute tolerance) with a forward target;
- the last valid pre-target sample is within 3h before the reset;
- the counter never fell by more than 1 point between successive pre-target
  samples of the cycle.

Otherwise the cycle stays visible with `reasons`:
`early_boundary_replacement`, `target_not_forward`, `missing_tail`,
`counter_decreased` (not called a new week, no provider-side cause
asserted), `unconfirmed_completion` (target passed, no rollover observed
yet), `no_valid_sample` (no usable reading at/before the target),
`beyond_requested_weeks` (eligible but older than the newest `weeks`
eligible cycles). The open window (future target, no successor) is not a
cycle. A cycle row is an observed boundary record ("window end"), not proof
that a reset occurred: an early replacement can carry a nominal date still
in the future.

`window.completed` is the mean over that window's newest `weeks` eligible
cycles; `remaining_pct` is the unused-at-reset share.

## Aggregation

`weighting: "equal_account"`. Headline and per-provider `used_pct` are
arithmetic means of **account** percentages: for `current`, each eligible
primary window's value; for `completed`, each account's own cycle mean. Two
Claude accounts count twice, one Codex account once, and an account with
more sampled cycles is not overweighted. Model caps never contribute.
`estimated_account_count` counts contributors whose reading (current) or any
counted cycle (completed) is not `exact`; they are included, not dropped.
`cycle_count` is the number of counted cycles (0 for `current`). All
arithmetic is `Decimal`; percentages serialize as one-decimal strings.

`coverage` reports `source_rows` (weekly observations of roster accounts),
`invalid_rows` (unusable timestamp or ratio) and `excluded_cycles`
(displayed cycles that did not count). `notes` state the weighting (counting
only tracked primary weekly accounts — roster accounts of providers without
a weekly quota, such as OpenRouter or DeepSeek, are not windows), the
freshness rule and the observed-at-reset caveat in one line each.

## Interface and verification

- A compact summary on Now links to `#weeklyUtilization`, the full panel at
  the top of History. Current/Completed mode changes the aggregate and the
  provider rows together. Account and model-cap details, boundary records,
  exclusion reasons and methodology expand in place.
- Repeated reset targets have distinct evidence-row keys (window, target,
  last observation and row index), so target replacements do not collide in
  React reconciliation. A target timestamp is not a unique cycle identity.
- Parent verification: **794 pytest tests passed**, **54 Node tests passed**,
  TypeScript and Vite build passed. The Python suite includes a Node-runner
  wrapper; the counts are not additive. Independent review passed after the
  roster-count and boundary-sampling regressions were corrected.
- Real API-observation replay and browser checks covered Current and Completed
  at **1440px** and **320px**, including expanded evidence, five provider rows,
  six primary accounts, two separately labelled Fable caps, no root overflow,
  and focus on the deep-linked panel. The fixed snapshot gave 45.5% current
  use and 54.0% completed-cycle use; these are dated verification values, not
  static dashboard data.
- The preview replay used sanitized observations from the existing private
  API with public ordinals mapped to anonymous adapter keys. Backend endpoint
  tests exercised temporary JSONL and SQLite ledgers; production activation
  and readback are a separate operational step.

Verification commands from the candidate root:

```bash
PYTHONPATH=$PWD/src /srv/pharos/repos/codex-usage-tracker/.venv/bin/python -m pytest -q
npm --prefix frontend test
npm --prefix frontend run build
```
