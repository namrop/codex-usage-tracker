# AI Usage dashboard: navigation, capacity summary and burn semantics (2026-09-14)

First implementation tranche after the keeper's live review (Discord
1531875202860974264, message 1549229183702532227). Scope: account-level
capacity summary, focused navigation, consistent account labels, distinct
burn projection states, list-price wording. This note records what the
frontend and the private API now mean, the exact commands that verify it,
and what was deliberately left for later. Parent verification is recorded
below; implementation and preview verification are not production activation.

## Views

The one long page is four hash-routed views under one shared header (the
AI Usage hero with its digest and the Governed Cognition picker) and one
compact nav bar of real links (`aria-current="page"` marks the selected
view; the refresh button lives on the bar so every view can reach it).

| hash | view | what it renders |
| --- | --- | --- |
| `#now` (default, also empty hash) | Now | Capacity by account · Window burn rate · Resets and provider health (reset countdowns + provider API status strip) · Subscriptions by provider (per-provider cards behind a fold, collapsed by default) |
| `#history` | History | Unified usage · 7 days (events / tokens / soonest reset / substitution tiles, the two throughput donuts, tokens across provider models, token composition, cache hit rate, the by-harness and by-provider-and-model folds) · Subscription quota history (unified weekly + five-hour utilization charts with their fold tables) |
| `#accounting` | Accounting | API list-price equivalent · 7 days (headline tile, sparkline, priced share) · Billing ledger · 30 days · Rulings per dollar |
| `#diagnostics` | Diagnostics | Collection freshness and coverage (usage and quota ledger freshness with per-lane collector health, unified coverage tiles, quota observation coverage per provider, burn sampling evidence with the raw burn observations fold) · Raw quota observations (the `subscriptionsRaw` fold) |

Every chart, table and fold key from before the split is still rendered;
none moved behind a promise. Inactive views **unmount** rather than hide, so
a Chart.js canvas is never built inside `display:none` (the 0×0 blank-chart
failure) — a view's charts are constructed at their real size when the view
becomes active. All data hooks live above the views: switching a view never
refetches, and last-good data, the 60-second refresh cadence and the
per-panel failure notices are shared by all four.

Deep links that predate the views keep working. The reset chips (rail and
hero) still link to `#subscription-card-<provider>`; `lib/views.ts`
resolves such an anchor to the Now view, the provider-detail fold opens for
that render (no preference is written), and the card is scrolled into view
and focused once the subscriptions loader has settled.

Second fix pass (parent browser QA 2026-09-14): a repeated click on the
*same* reset link fires no `hashchange`, so App also listens for plain
primary clicks on same-document fragment links
(`sameDocumentAnchorNavigation` in `lib/views.ts`, Node-tested) and bumps
the reveal itself when the hash repeats; modifier clicks, other buttons,
named targets, downloads and cross-document hrefs are left to the browser,
and no fake hash or query is written. The reveal (`lib/reveal.ts`,
Node-tested) waits frame by frame for the card to exist and have a box,
then scrolls to it and focuses it (the cards are `tabIndex={-1}`), retrying
while focus has not landed, within a bounded frame budget.

On a phone (≤ 640 px) the hero keeps its title, theme picker and the four
digest tiles (two abreast) and folds the descriptive subtitle behind a
native `<details>`; the capacity card does the same with its caption. The
text is unchanged and one tap away; desktop is unchanged. The parent
measures the resulting geometry in a real browser.

## Capacity by account

`frontend/src/lib/capacity.ts` (Node-tested) derives one row per provider
account from `/api/subscriptions` `latest` rows, joined to `/api/quota-burn`
series by canonical provider, quota name, harness and account ordinal —
never by array position or percentage.

- **Labels.** `accountDisplayLabel()` in `lib/providers.ts` is the one
  label: "Claude 1" / "Claude 2" from the API's `provider_account_index` of
  `provider_account_count`, bare "Claude" for a single account. The
  capacity summary, the reset rail, the burn table and the provider cards
  all call it.
- **Window kinds.** `model` (five_hour, seven_day, week …), `model_specific`
  (a general window name with a suffix, e.g. `seven_day_fable` → "seven day
  · Fable"), `tool` (search/tool names or request-counted units, e.g. Z.AI
  `web_search_month`, or a rate-limited search backend's `web_search_limit`
  — classified as a tool *before* the window kind is consulted, so an
  "unknown" window with a zero remaining and no cap is a tool constraint,
  never an empty balance), `allowance` (a balance-denominated "unknown"
  window with a provider-reported limit: OpenRouter `api_key_quota`, listed
  beside the balance and metered from the provider's `remaining`/`limit` —
  its `used_value` is the key's lifetime usage, kept as a raw field and
  shown as "key lifetime usage", never as the allowance's consumption),
  `balance` (lifetime windows, or balance units with no cap).
- **Units stay raw.** Percent windows say "N% left"; dollar caps "$16.00 of
  $20.00 left"; counted quotas "0 of 4000 requests left"; a counted quota
  with no denominator "0 requests left" (no percentage, still a
  constraint); balances "$0.00 balance". Nothing is summed or averaged
  across accounts or providers.
- **State.** `exhausted` only when a general model window is at its cap or a
  balance is empty (an empty balance binds even if an allowance remains);
  `constrained` when a model-specific cap, a tool quota or a key allowance
  is at its cap (headline says which model, tool or key, and that the
  account is not exhausted) or the busiest general window is at ≥ 90 % (the
  rail chips' violation band — a display convention, not a policy);
  `available` otherwise; `stale` when no fresh window exists (older than the
  subscriptions freshness threshold, or the window reset after the sample)
  — a stale row shows its reading but asserts nothing. Exhausted rows sort
  first, then constrained, then by provider order and account.
- **Confidence** travels with every window with the backend's precedence
  (`_quota_row_confidence`): partial coverage outranks the measurement, and
  the measurement is kept beside it, so a partial row reads "Partial · exact
  measurement" or "Partial · estimated measurement" — never "Exact" alone.
  Plain rows read Exact / Estimated / Unknown; an estimated window (OpenCode
  Go) is never rendered as exact.

## Burn projection states

`/api/quota-burn` series now carry `harness`, `quota_scope`, `unit`,
`provider_account_index`, `provider_account_count`, `observation_count`,
`projection_status` and `projection_reason`. The estimator is unchanged
(rate at the observed active-use rate over the last three monotonic rises,
no forecast past the window's own reset). The status is the claim the row
can honestly make, decided in this order:

| `projection_status` | `projection_reason` | meaning |
| --- | --- | --- |
| `not_applicable` | `balance_window`, `no_utilization_ratio` | a lifetime/unknown window or a row with no used/limit ratio: shown, never forecast |
| `stale` | `observation_stale`, `reset_passed_since_observation` | latest sample older than the ledger max age (3 h), or the window reset after it — asserts nothing about now (not even "exhausted") |
| `exhausted` | `used_at_cap` | fresh reading at 100 % (a tool quota at its cap is exhausted like any cap) |
| `insufficient_observations` | `single_observation`, `no_positive_rate` | no positive measured rate |
| `not_applicable` | `no_reset_reported`, `reset_not_in_future`, `window_longer_than_seven_days` | positive rate but nothing to forecast against |
| `projected_before_reset` | `empty_before_reset_at_observed_rate` | `projected_exhaustion_at` is set |
| `resets_first` | `reset_before_empty_at_observed_rate` | the window resets before it empties at the observed rate |

The empties-first / resets-first comparison is anchored at the latest
observation on both legs (the ETA extrapolates from that sample, so the
reset is measured from it too); `seconds_to_reset` in the payload stays the
countdown from now. Parent review 2026-09-14 caught the cross-anchor
comparison and pinned it in `tests/test_quota_burn_review_regressions.py`.

Account ordinals are derived from the same private canonical identity and
the same sort as `/api/subscriptions`, against the dashboard's 168-hour
roster (one ledger query covers the roster and the burn window), so a quiet
primary account never lets the secondary account read as "1". The roster is
authoritative: a series whose account is outside it (observed only in the
burn read's baseline margin or default window) is left out of the burn
payload rather than inserted and renumbered (also from the parent review).
The private account reference never crosses the API.

## Financial copy

"Est. spend" / "Est. spend (list)" is now **API list-price equivalent** on
the hero digest, the Accounting headline and the rulings-per-dollar
denominator. Billing facts stay their own record class; nothing is billed
by this app, and no charge is fabricated.

## Commands

```bash
cd /srv/pharos/workspaces/ai-usage-capacity-navigation
# Reuse the installed environment, but import the candidate source:
PYTHONPATH=$PWD/src /srv/pharos/repos/codex-usage-tracker/.venv/bin/python -m pytest -q
# Install frontend dependencies with npm ci if they are not already present.
npm --prefix frontend test
npm --prefix frontend run build
```

`tests/test_capacity_navigation.py` holds the regression tests for this
tranche: burn ordinals agreeing with subscriptions (multi-account,
reorder/filter), every projection status, estimated confidence, the view
router and nav, view hierarchy, deep-link reveal, the capacity helper's
purity, shared labels, burn and financial copy, phone containment rules,
and the Node runner. `tests/test_quota_burn_review_regressions.py` holds
the parent review's two backend regressions (same-anchor ETA comparison,
authoritative roster). The Node runner is skipped only when `node` or
`frontend/node_modules/esbuild` is absent.

## Verified first-tranche result

- Parent reran the full suite: **766 pytest tests passed**. This suite includes
  one wrapper that invokes the frontend tests; do not add the counts together.
- Frontend separately: **40 Node tests passed**, TypeScript and Vite build
  exited 0. The two Vite notices about classic GC/Chart.js scripts are expected;
  the existing script-loading contract deliberately does not bundle them.
- Independent backend and frontend reviews both returned `passed: true` with
  no blocking logic or security findings after two bounded correction passes.
- Browser: all four views at **1200 × 900** and **390 × 844**, with no root
  horizontal overflow. All five History canvases had non-zero dimensions and
  populated datasets. The first phone capacity account began at **y=638 px**.
- Repeated activation of the same Kimi reset link reopened the provider fold
  and focused `subscription-card-kimi-k3-coding`, including after collapsing it.
- The preview uses candidate assets and its burn projection over real sanitized
  observations from the existing read-only private API. Public account ordinals
  become anonymous adapter keys for that preview; this is not a direct
  production-ledger startup or production deployment claim.
- Existing integration checkout remained at `bc36d80` during verification. No
  production service restart, collector change, provider routing change, or NixOS activation
  was performed in this tranche.

Non-blocking review notes retained: after a deep-link reveal, returning to Now
can reopen provider details; long History view mounts still recreate the full
legacy folded tables; burn projections can show a past, conditional ETA rather
than proof of current exhaustion. Follow-on filter work must preserve common
account-roster scope across consumers.

## Deferred (not in this tranche)

- Provider routing, threshold enforcement and notifications are not part of the
  requested presentation changes; the summary describes, it does not act.
- Remaining items from the approved review: date/filter controls and source-linked
  usage exploration; recorded-ruling drill-down and real billing integration;
  exact rebuildable longer-history summaries without row-limit fallback.
- These remain follow-ons to this first implementation pass, not a separate
  keeper restriction or a claim that the whole six-part review is implemented.
