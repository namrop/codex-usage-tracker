# codex-usage-tracker

`codex-usage-tracker` is a tiny utility that reads your Codex OAuth token, fetches usage
from ChatGPT’s usage endpoint, and appends normalized snapshots to a JSONL ledger.

## What it does

- Loads Codex credentials from `~/.hermes/auth.json`.
- Refreshes expired access tokens automatically.
- Fetches `/backend-api/wham/usage` from `chatgpt.com`.
- Writes one normalized ledger row per run to a JSONL file.
- Provides a small CLI with five subcommands:
- `fetch`
- `daemon`
- `dump-raw`
- `commit-ledger`
- `dashboard`

## API endpoint

Usage endpoint used by this package:

- `GET https://chatgpt.com/backend-api/wham/usage`
- Requires `Authorization: Bearer <access_token>`
- Optional `ChatGPT-Account-Id` header (if available in auth payload)

Token refresh flow:

- `POST https://auth.openai.com/oauth/token`
- `grant_type=refresh_token`
- `client_id=app_EMoamEEZ73f0CkXaXp7hrann`
- `refresh_token=<refresh_token>`

## Install

```bash
cd /Users/luisramirez/Code/codex-usage-tracker
pip install -e .
```

## CLI usage

Each subcommand accepts:

- `--ledger PATH` (optional)
- `--atrium-root PATH` (default `/Users/luisramirez/Digital_Workspace`)

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

### Dashboard

```bash
cd /Users/luisramirez/Code/codex-usage-tracker
python -m codex_usage_tracker dashboard
python -m codex_usage_tracker dashboard --ledger /tmp/test_ledger.jsonl --port 5174 --host 127.0.0.1
```

Optional args:

- `--ledger PATH` (optional)
- `--atrium-root PATH` (default `/Users/luisramirez/Digital_Workspace`)
- `--host HOST` (default `127.0.0.1`)
- `--port PORT` (default `5174`)

Dashboard routes:

- `GET /` — HTML dashboard with charts and tables
- `GET /api/data` — all rows in JSON array, newest first
- `GET /api/summary` — summary payload:
  - total rows, first/last `fetched_at`, current usage percentages, and `plan_type`
- `GET /api/trend` — last 168 rows with fields:
  - `fetched_at`, `session_used_pct`, `weekly_used_pct`, `spark_session_used_pct`, `spark_weekly_used_pct`

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
