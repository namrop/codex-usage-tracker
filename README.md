# codex-usage-tracker

`codex-usage-tracker` collects Codex subscription snapshots and normalizes private
cross-harness usage, quota, and billing facts into distinct canonical ledgers.
SQLite is the operational hot store; JSONL remains the legacy Codex snapshot and
explicit interchange format.

## What it does

- Loads Codex credentials **read-only** from `~/.hermes/auth.json` (or
  `$HERMES_HOME/auth.json`) and fetches `/backend-api/wham/usage` from
  `chatgpt.com`. It never refreshes or writes the token — see
  [Credential ownership](#credential-ownership).
- Preserves the legacy Codex JSONL snapshot daemon and public-safe projection.
- Collects usage from Hermes, Claude Code, and OpenCode plus quota/account
  observations from Codex, Claude Code, Kimi Code, OpenRouter, DeepSeek, and
  estimated OpenCode Go windows.
- Maintains separate `usage_event_v1`, `quota_observation_v1`, and
  `billing_fact_v1` SQLite ledgers.
- Provides eleven CLI subcommands:
  - `fetch`
  - `daemon`
  - `dump-raw`
  - `commit-ledger`
  - `write-public-projection`
  - `write-public-usage-projection`
  - `collect-all`
  - `migrate-ledger`
  - `export-ledger`
  - `audit-ledger`
  - `dashboard`
- Can write a public-safe derived token chart JSON so a DMZ host never needs any
  private canonical ledger.

## API endpoint

Usage endpoint used by this package:

- `GET https://chatgpt.com/backend-api/wham/usage`
- Requires `Authorization: Bearer <access_token>`
- Optional `ChatGPT-Account-Id` header (if available in auth payload)

## Credential ownership

**Hermes owns the Codex OAuth token. This tracker is a read-only consumer and
must never refresh or write `auth.json`.**

Until 2026-08-15 this tracker did refresh the token, and it caused a live
outage. `auth.json` holds two copies of the Codex credentials — the singleton
at `providers.openai-codex.tokens` and a pool entry at
`credential_pool.openai-codex` — and the pool copy is what serves Hermes's real
API traffic. A refresh here rotated the singleton only, stranding the running
gateway on a token that had just been invalidated; it failed with `HTTP 401:
Provided authentication token is expired`. Writing both copies would not fix
it either, because the gateway holds an *in-memory* pool snapshot. Only the
process that owns the pool can refresh safely.

The old implementation also took no lock and rewrote the whole file
non-atomically, which could clobber the credentials of the eight other
providers sharing it.

Current behaviour when the access token is expired (or expiring within 60 s, or
undecodable): log an error naming the cause and skip the poll, leaving a gap in
the ledger. Hermes refreshes proactively 120 s before expiry whenever it next
selects the Codex provider, so gaps are normally brief. **A persistent gap is a
real signal** that Hermes-side auth needs attention — check `hermes auth
status`, and re-authorize with `hermes auth add openai-codex` if needed.

Full incident:
`07_systems/hermes/incidents/hermes_codex_oauth_dual_uncoordinated_refreshers_auth_store_write_race_incident_2026-08-15.md`
in the Atrium canon.

## Install

```bash
cd /Users/luisramirez/Code/codex-usage-tracker
pip install -e .
```

## CLI usage

The Codex snapshot commands (`fetch`, `daemon`, `dump-raw`,
`commit-ledger`, and `write-public-projection`) accept:

- `--ledger PATH` (optional)
- `--atrium-root PATH` (default `/Users/luisramirez/Digital_Workspace`)

`collect-all` has its own source and destination path flags documented below;
`dashboard` accepts the Codex ledger/Atrium flags plus its host and port.

If `--ledger` is omitted, the default is:

- `CODEX_USAGE_LEDGER_PATH` environment variable (if set), or
- `{atrium-root}/12_runtime/ledgers/codex_usage/codex_usage_ledger.jsonl`

### Fetch

```bash
codex-usage-tracker fetch
codex-usage-tracker fetch --ledger /tmp/test_ledger.jsonl
```

### Daemon

```bash
codex-usage-tracker daemon
codex-usage-tracker daemon --ledger /tmp/test_ledger.jsonl
```

Daemon behavior:

- Sleeps until the next top-of-hour boundary (`minute=0`, `second=0`)
- Fetches and writes ledger row
- Repeats indefinitely
- Handles `SIGINT` / `SIGTERM` and prints a shutdown message

### Dump raw payload

```bash
codex-usage-tracker dump-raw
```

Prints the raw JSON response from the usage endpoint to stdout for inspection.

### Commit ledger

```bash
codex-usage-tracker commit-ledger
codex-usage-tracker commit-ledger --dry-run
codex-usage-tracker commit-ledger --message "Update Codex usage ledger"
```

Validates the configured JSONL ledger and commits only that ledger path inside the Atrium repo if it changed. The command refuses to run when unrelated paths are already staged, so a scheduled ledger commit cannot accidentally sweep in other Atrium work.

### Public projection for Namrop / DMZ hosts

The raw Codex ledger contains private account/runtime fields in `raw_payload` and should not be copied to a DMZ host. Use the projection command to write only the derived chart payload consumed by Namrop:

```bash
codex-usage-tracker write-public-projection \
  --ledger /srv/pharos/atrium/canon/12_runtime/ledgers/codex_usage/codex_usage_ledger.jsonl \
  --public-projection /tmp/namrop-public/codex-token-chart.json \
  --public-projection-source sol-public-projection
```

The payload contains only:

- rolling window timestamps;
- derived token buckets and API/session counts;
- cache hit percentage;
- quota deltas and reset/drop markers;
- a compact `summary` object.

It intentionally excludes raw usage rows, `raw_payload`, email, `user_id`, `account_id`, OAuth tokens, prompts, transcripts, and unrestricted local paths.

For a VPS/DMZ deployment, run this on the trusted host and push only the generated JSON artifact, for example with a separate cron/systemd step:

```bash
rsync -az --chmod=F644 /tmp/namrop-public/codex-token-chart.json \
  namrop-vps:/var/www/namrop-public/codex-token-chart.json
```

`fetch` and `daemon` can also write the public projection immediately after a private ledger append when given `--public-projection PATH`. This keeps the sidecar explicit and avoids surprising untracked files beside the raw ledger. Pass `--no-public-projection` to disable the sidecar write in wrapper scripts that set a default path.

### Dashboard

```bash
cd /Users/luisramirez/Code/codex-usage-tracker
python -m codex_usage_tracker dashboard
python -m codex_usage_tracker dashboard \
  --ledger /tmp/codex_usage_ledger.jsonl \
  --unified-usage-ledger ~/.local/state/codex-usage-tracker/usage_events.sqlite3 \
  --quota-ledger ~/.local/state/codex-usage-tracker/quota_observations.sqlite3 \
  --billing-ledger ~/.local/state/codex-usage-tracker/billing_facts.sqlite3 \
  --port 5174 --host 127.0.0.1
```

Optional args:

- `--ledger PATH` (optional legacy Codex JSONL)
- `--unified-usage-ledger PATH` (private canonical usage SQLite/JSONL)
- `--quota-ledger PATH` (private canonical subscription/quota SQLite/JSONL)
- `--billing-ledger PATH` (private canonical billing SQLite/JSONL)
- `--atrium-root PATH` (default `/Users/luisramirez/Digital_Workspace`)
- `--host HOST` (default `127.0.0.1`)
- `--port PORT` (default `5174`)

#### Frontend build (React + Vite)

The dashboard frontend is a React + Vite app in `frontend/` that builds into
the Flask package's static dir (`src/codex_usage_tracker/static/app/`); the
`/` route serves the built entry document. The build output is committed, so
Python-only checkouts and installed wheels serve the dashboard without node.
After changing anything under `frontend/`, rebuild and commit the output:

```bash
cd frontend
npm install        # first time only
npm run build      # type-checks, then builds into ../src/codex_usage_tracker/static/app/
```

For live-reload development, `npm run dev` serves the app on Vite's dev port
and proxies `/api` and `/static` to a Flask dashboard running on
`127.0.0.1:5174`. The Governed Cognition loader (`/static/gc/loader.js`) and
the vendored Chart.js UMD stay classic `<head>` scripts by contract — never
bundled — so the theme injects before first paint and the app bundle carries
no third-party color literals (`tests/test_theme_conformance.py` enforces
this).

The top-level **AI Usage** view loads unified usage, subscriptions, and billing
independently, so one absent or unavailable private ledger does not hide the
other panels. Request-cost estimates remain separate from posted billing facts.
Multiple accounts for the same subscription provider remain independent in the
private weekly chart as anonymized `account 1`, `account 2`, … lines; they are
never summed, and repeated-provider lines retain the provider color while using
different dash patterns. Raw account/source identities are not returned.
The Codex detail compatibility view was removed outright (keeper-ruled
2026-08-30); its server endpoints stay routable for non-dashboard consumers.
`--ledger` still means the legacy Codex JSONL ledger and is not repurposed
for canonical usage.

Dashboard routes:

- `GET /` — HTML dashboard with charts and tables
- `GET /api/data` — all rows in JSON array, newest first
- `GET /api/summary` — summary payload:
  - total rows, first/last `fetched_at`, current usage percentages, and `plan_type`
- `GET /api/trend` — last 168 rows with fields:
  - `fetched_at`, `session_used_pct`, `weekly_used_pct`, `spark_session_used_pct`, `spark_weekly_used_pct`

## Cross-harness private accounting

`collect-all` maintains three distinct private canonical ledgers without changing
the source harness stores:

- `usage_event_v1` is the additive request/aggregate ledger populated from Hermes
  `llm_usage_events`, Claude Code assistant receipts, and current or legacy
  OpenCode SQLite databases;
- `quota_observation_v1` is the point-in-time snapshot ledger populated from the
  existing Codex snapshot ledger, the authenticated Claude Code CLI `/usage`
  view, estimated OpenCode Go windows, and optional provider account APIs;
- `billing_fact_v1` is the signed monetary ledger for invoice lines, charges,
  credits, refunds, payments, taxes, and adjustments. `collect-all` binds and
  creates this ledger but does not synthesize billing facts from request-cost
  estimates.

SQLite is the operational default. Each fact class has its own type-bound WAL
ledger under `~/.local/state/codex-usage-tracker/`:

- `usage_events.sqlite3`
- `quota_observations.sqlite3`
- `billing_facts.sqlite3`

The three destination paths must be distinct. Reusing or aliasing a destination
is rejected before source collection or any write. Existing JSONL ledgers remain
supported as an explicit compatibility/interchange backend, but SQLite is the
hot store.

On Sol, source defaults are `/var/lib/hermes/primary/state.db`,
`~/.claude/projects`, `~/.local/share/opencode/opencode-stable.db`, and
`~/.local/share/opencode/opencode-local.db`. The existing Codex snapshot ledger
in the Atrium canon is read-only to this command. Every path can be overridden:

```bash
codex-usage-tracker collect-all \
  --state-db /var/lib/hermes/primary/state.db \
  --claude-instance claude-code=/home/luis/.claude \
  --claude-instance claude-code-secondary=/home/luis/.claude-accounts/secondary \
  --opencode-db ~/.local/share/opencode/opencode-stable.db \
  --opencode-db ~/.local/share/opencode/opencode-local.db \
  --usage-ledger ~/.local/state/codex-usage-tracker/usage_events.sqlite3 \
  --quota-ledger ~/.local/state/codex-usage-tracker/quota_observations.sqlite3 \
  --billing-ledger ~/.local/state/codex-usage-tracker/billing_facts.sqlite3
```

`--claude-instance SOURCE_SUFFIX=CONFIG_DIR` is repeatable. Suffixes must be
`claude-code` or begin `claude-code-` and use lowercase alphanumeric
hyphen-separated segments; config directories must be absolute or `~`-expandable.
Each usage root is `CONFIG_DIR/projects`. The primary namespaces remain exactly
`sol:claude-code` and `sol:claude-code-quota`; the declarations above add
`sol:claude-code-secondary` and `sol:claude-code-secondary-quota`. Omitting every
`--claude-instance` retains the legacy `--claude-root` and
`--claude-probe-dir` behavior. The primary declaration selects its transcript
root but leaves quota authentication on Claude Code's normal default layout
(`~/.claude` plus the home-level `~/.claude.json`). Only non-primary instances
set `CLAUDE_CONFIG_DIR` to their declared custom config directory.

Use `--scope usage` or `--scope quota` to run the independently schedulable lanes.
The usage scope reads and writes only usage sources/the usage ledger. The quota
scope reads the Codex/OpenCode quota sources plus enabled provider probes and
writes only the quota ledger. Out-of-scope default paths are not preflighted or
created; billing remains bound only by the compatibility `all` scope.

A lane that fails to read or persist never fails the run (keeper-ruled
2026-08-29): it is retried twice more with a short growing backoff and then
ceded — skipped for that run while every remaining lane still collects and
appends. A ceded lane keeps its `last_error_class` and does not advance
`last_success_at` in `health/lanes-<scope>.json`, the per-run
`health/collect-<scope>-last.json` report names it, and the summary line's
`lanes=N/M` / `ceded=` fields surface it; the external watchdog owns the
alerting. Scheduled health-signaled runs should still add `--strict-sources`,
which now fails the run only for unadjudicated identity conflicts —
quarantined re-derivations that retrying cannot resolve.

Use `--dry-run` to validate adapters, type bindings, and replay/conflict behavior
without creating locks, changing permissions, or writing ledger artifacts. Use
`--no-live-quota` for a completely local run. Successful runs print one compact
JSON summary and no source payloads.

For a bounded recovery from retained legacy Codex snapshots, run only the quota
scope with `--codex-quota-history`, `--codex-quota-history-since <ISO-8601>`,
and `--no-live-quota`. The exclusive cutoff, input-size/snapshot/fact ceilings, and
source-isolated mode bound the operation. It projects only retained Codex
snapshots through the secret-free quota contract and fails on malformed input;
canonical identity makes retries idempotent. Normal scheduled quota runs continue
to read only the newest snapshot.

### Migration, audit, and JSONL interchange

Migrate each legacy JSONL separately and bind the destination explicitly:

```bash
codex-usage-tracker migrate-ledger \
  --source-jsonl ~/.local/state/codex-usage-tracker/unified_usage.jsonl \
  --destination-sqlite ~/.local/state/codex-usage-tracker/usage_events.sqlite3 \
  --fact-type usage_event_v1

codex-usage-tracker migrate-ledger \
  --source-jsonl ~/.local/state/codex-usage-tracker/quota_observations.jsonl \
  --destination-sqlite ~/.local/state/codex-usage-tracker/quota_observations.sqlite3 \
  --fact-type quota_observation_v1
```

Run `migrate-ledger ... --dry-run` first. Audit a SQLite ledger with
`audit-ledger --sqlite PATH --fact-type TYPE`. Export canonical JSONL with
`export-ledger --source-sqlite PATH --destination-jsonl PATH --fact-type TYPE`.
Migration and export reject source/destination aliases.

Live OpenRouter, DeepSeek, and Kimi Code collection reads `OPENROUTER_API_KEY`,
`DEEPSEEK_API_KEY`, and `KIMI_CODING_API_KEY` from
`/var/lib/hermes/primary/.env` by default. Kimi's official
`GET https://api.kimi.com/coding/v1/usages` response becomes exact five-hour
and weekly `provider_unit` observations; only normalized counters and reset
times are retained. Claude subscription limits come from the authenticated
Claude Code CLI itself: the
tracker opens a short-lived safe-mode TUI in a private probe directory, invokes
Claude Code's built-in `/usage` view, captures the displayed five-hour, weekly,
and Fable-week percentages and reset times, and exits without making a model
call. It reuses one deterministic probe session ID to avoid flooding Claude
Code's session history. Each configured instance gets a distinct deterministic
probe directory (for example `claude-probe-secondary`). A non-primary instance's
custom config directory is attached directly to the tmux pane command; the
primary omits `CLAUDE_CONFIG_DIR` and preserves Claude Code's default account
lookup. The probe requires `tmux`. Configure this boundary with
`--claude-command`, `--claude-probe-dir`, and `--claude-quota-timeout`.

The optional `--dotenv PATH` loader recognizes only the OpenRouter, DeepSeek,
and Kimi Code credential names, does no variable or command expansion, and
never copies credentials to ledger rows. Existing process environment values take
precedence. A live provider failure is isolated: other sources still collect,
while the compact summary reports only the source name and exception class.

Canonical writers validate schema version 1, recursively reject credential
field names, canonicalize with RFC 8785/JCS, and enforce idempotent source
identities. SQLite uses exact immutable schema validation, WAL mode, private
`0600` database/sidecar/lock artifacts, transactionally serialized first-start,
and indexed append/query paths. Replaying an identical source identity is a
no-op; replaying it with different content is an error. The strict canonical
writer APIs retain that fail-closed contract. `collect-all` uses an explicit
source-isolated projection policy: a changed identity is omitted while unrelated
facts and sources continue, and only source class, hashes, and changed field names
are reported. Claude Code's mutable streaming receipts are deferred until terminal,
repeated-identical, or file-quiescent; a historical receipt that differs only in
`occurred_at`/`recorded_at` reuses the already-committed canonical timestamp and is
reported as a `canonical_replay`, never overwritten. A previously committed
non-terminal Claude receipt that later becomes `ok` with nondecreasing token counts
keeps the immutable original and appends one deterministic signed `correction`;
zero-token corrections retain finalization metadata without changing totals. The
reconciliation reads the complete ledger and appends inside the canonical writer
lock/SQLite transaction. Duplicate variants, later differing final totals, token
decreases, unrelated field changes, and foreign/manual corrections remain
quarantined conflicts. Explicit audits scan and
revalidate every stored payload. Token accounting keeps input, cache read,
cache write, output, and reasoning buckets separate; reasoning is diagnostic
and is not added twice where canonical output already includes it.

The dashboard exposes private aggregates when configured with
`UNIFIED_USAGE_LEDGER_PATH`, `QUOTA_LEDGER_PATH`, and `BILLING_LEDGER_PATH` (or
the corresponding `create_app` arguments). Usage and subscription responses
include a three-hour ledger-write freshness contract (`fresh`, `stale`, or
`empty`); the UI stops rendering ordinary charts/rows when either canonical lane
is stale:

- `GET /api/unified-usage` supports `provider`, `harness`, `purpose`,
  `model_requested`, `days`, and `before`; it returns token/request-cost totals,
  selected window metadata, harness and provider/model groups, and
  exact/reconstructed coverage. Supplying `hours=1..168` instead of `days`
  aligns the response to
  complete UTC hours and adds zero-filled chart series. The model chart uses
  reported model when available (requested model otherwise), keeps the five
  largest provider/model pairs, and rolls the rest into `Other models`. The
  comparison chart defines OpenAI Codex subscription traffic by
  `provider=openai-codex` and Claude Code traffic by `harness=claude_code`;
- `GET /api/subscriptions` supports `provider`, `harness`, `quota_name`,
  `days`, and `before`; it returns the latest observation per subscription
  window and normalized observation history;
- `GET /api/billing` supports `provider`, `transaction_kind`, `status`,
  `currency`, `days`, and `before`; it returns signed totals separately by
  currency and transaction kind plus an allowlisted transaction projection.

Private reads with no explicit window default to the latest eight days
(keeper ruling 2026-08-29) rather than reading the whole ledger; the response
`window` metadata reports `default_days` and a `next_before` cursor. Older
history is fetched with timestamp-cursor paging: `before=<RFC 3339>` returns
the half-open window `[before - days, before)` (page length `days`, default
eight), and passing the returned `next_before` as the next request's `before`
yields disjoint, gapless pages even while collectors append. An explicit
`days` without `before` keeps its historical now-anchored, upper-unbounded
semantics, still capped by the per-request row limit.

Billing totals are never combined with `usage_event_v1` estimated or actual
request costs. Private API reads are schema-bound, indexed for typed filters,
and capped per request. These endpoints and all three canonical ledgers are
private. Do not publish those APIs or ledgers to a DMZ.

### Unified public usage projection

To publish a provider-neutral usage chart, derive a separate allowlisted JSON
artifact from the type-bound `usage_event_v1` SQLite ledger on the trusted host:

```bash
codex-usage-tracker write-public-usage-projection \
  --usage-ledger ~/.local/state/codex-usage-tracker/usage_events.sqlite3 \
  --public-projection /tmp/namrop-public/ai-usage.json \
  --public-projection-source sol-unified-usage
```

The artifact uses the exact `namrop_public_usage_projection.v3` contract with
`schema_version` 3 and, by default, contains exactly 168 complete UTC hourly
buckets. Empty hours are represented by zero rows. It publishes only bucket
boundaries, token buckets, derived prompt/total tokens and cache-hit percentage,
aggregate request attempt counts, coarse measurement confidence, bounded
public-safe `provider_rows`, `model_rows`, and `harness_rows`, and a compact
summary. Harness labels use the finite `Hermes`, `Claude Code`, and `Other`
allowlist. Reasoning tokens remain diagnostic because canonical output already
includes reasoning; they are not added to totals twice. An `api_attempt` counts
as one request and a `historical_aggregate` contributes its
`reconstructed_call_count`.

The writer accepts only a `usage_event_v1` SQLite input, validates the exact v3
allowlist, and atomically replaces the destination. The compatibility
`--quota-ledger` argument is accepted but ignored; v3 has no subscription or
quota rows. It excludes raw harness/provider/model values, purpose,
account/source identity, costs, prompts, transcripts, extension payloads,
credentials, and local paths. **Do not publish** the source SQLite ledger or the
private dashboard APIs.

The older `write-public-projection` command remains a compatibility boundary for
the legacy Codex snapshot/token-correlation chart. It is additive and is not
replaced by `write-public-usage-projection`; choose the artifact contract your
consumer expects.

## Design notes

- `docs/model-routing-metrics-and-tracker-requirements-2026-06-06.md` — requirements note for evolving this tracker into the data plane for quota-aware model routing and pricing decisions. It captures the Codex default-until-reserve policy, DeepSeek/MiMo fallback evaluation context, required new ledgers/metrics, collection frequencies, and dashboard/API targets.

#### Tailscale funnel

```bash
chmod +x daemon/setup-tailscale.sh
./daemon/setup-tailscale.sh
```

## Deployment

The package includes a small macOS LaunchAgent setup for both hourly collection and dashboard web serving.

### Architecture

- Tracker agent: `com.lux.codex-usage-tracker`
  - Runs `python3 -m codex_usage_tracker daemon --atrium-root /Users/luisramirez/Digital_Workspace`.
  - Scheduled hourly at minute `0`.
- Dashboard agent: `com.lux.codex-dashboard`
  - Runs `python3 -m codex_usage_tracker dashboard --port 5174 --atrium-root /Users/luisramirez/Digital_Workspace`.
  - Runs continuously to serve the web UI.
- Ledger autocommit agent: `com.lux.codex-usage-ledger-autocommit`
  - Runs `python3 -m codex_usage_tracker commit-ledger --atrium-root /Users/luisramirez/Digital_Workspace`.
  - Scheduled daily at 23:55 local time.
  - Commits only `12_runtime/ledgers/codex_usage/codex_usage_ledger.jsonl` and refuses unrelated staged files.

### Install

From the repository root:

```bash
./daemon/install.sh
```

`install.sh`:

- Detects the Python path using `CODEX_TRACKER_PYTHON`, then `which python3`, then `/usr/bin/python3`.
- Replaces the `PYTHON_PATH` placeholder in both plist files.
- Copies both plists into `~/Library/LaunchAgents/`.
- Loads both agents with `launchctl`.

You can override Python for an install run with:

```bash
CODEX_TRACKER_PYTHON=/path/to/venv/bin/python3 ./daemon/install.sh
```

### Status

```bash
./daemon/status.sh
```

This prints:

- `launchctl list` entries for `com.lux.codex*`
- The latest 5 log lines for both:
  - `~/Library/Logs/codex-usage-tracker.log`
  - `~/Library/Logs/codex-dashboard.log`

### Uninstall

```bash
./daemon/uninstall.sh
```

This unloads both LaunchAgents and removes the plist files from `~/Library/LaunchAgents/`.

### Logs

If either service is not running, logs are useful for diagnosing startup issues:

- Tracker: `tail -n 50 ~/Library/Logs/codex-usage-tracker.log`
- Dashboard: `tail -n 50 ~/Library/Logs/codex-dashboard.log`

## Ledger schema

Each JSONL entry contains:

- `id` (`uuid4` string)
- `fetched_at` (UTC ISO 8601)
- `plan_type` (`str`)
- `session_used_pct` (`float`)
- `weekly_used_pct` (`float`)
- `session_reset_at` (`int | None`)
- `weekly_reset_at` (`int | None`)
- `credits_balance` (`str`)
- `credits_has_credits` (`bool`)
- `spark_session_used_pct` (`float | None`)
- `spark_weekly_used_pct` (`float | None`)
- `spark_session_reset_at` (`int | None`)
- `spark_weekly_reset_at` (`int | None`)
- `raw_payload` (full API response object)

## Atrium subbeam path

Default ledger path when not provided:

`{atrium-root}/12_runtime/ledgers/codex_usage/codex_usage_ledger.jsonl`
