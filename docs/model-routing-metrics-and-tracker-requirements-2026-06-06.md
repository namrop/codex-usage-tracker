# Model routing metrics and tracker requirements

Created: 2026-06-06T02:03:28-04:00  
Context source: Discord thread `OpenCode Go Cost Geometry` / Codex usage tracker planning discussion  
Related Atrium note: `/Users/luisramirez/Digital_Workspace/20_digital_architecture/loci/llmport_quota_aware_model_routing_subsystem_note_2026-06-05.md`

## Compression

`codex-usage-tracker` should evolve from a quota dashboard into the data plane for an LLMPort-style **quota-aware model routing and budget governor**.

The near-term routing policy is:

> Codex remains default while healthy. When projected Codex usage threatens a protected reserve, move non-vision/non-critical work to DeepSeek-class fallbacks and preserve Codex for vision, final judgment, and high-priority emergencies.

To make good pricing/model-switching decisions, the tracker needs to collect more than hourly Codex percentage snapshots. It needs quota state, **Codex token/API-call accounting**, token/cost correlation, direct-provider spend, routing decisions, model capabilities, and task outcomes at enough frequency to support projections before the user runs out of quota.

## Current data sources

### Already tracked / available

1. **Codex usage snapshots**
   - Ledger: `/Users/luisramirez/Digital_Workspace/12_runtime/ledgers/codex_usage/codex_usage_ledger.jsonl`
   - Schema: `/Users/luisramirez/Digital_Workspace/12_runtime/ledgers/codex_usage/schema.md`
   - Source endpoint: `GET https://chatgpt.com/backend-api/wham/usage`
   - Auth source: `~/.hermes/auth.json`, provider `openai-codex`

2. **Hermes token accounting**
   - DB: `/Users/luisramirez/.hermes/state.db`
   - Table/filter: `sessions` where `billing_provider = 'openai-codex'`
   - Current correlation implementation: `src/codex_usage_tracker/token_correlation.py`
   - This is the current source for Codex-side token/API-call counts, but it is session/window-granularity unless the Hermes provider layer records individual Codex calls.
   - Useful fields already available in Hermes session rows:
     - `started_at`
     - `billing_provider`
     - `model`
     - `api_call_count`
     - `input_tokens`
     - `cache_read_tokens`
     - `cache_write_tokens`
     - `output_tokens`
     - `reasoning_tokens`

3. **Dashboard surfaces**
   - `GET /api/data`
   - `GET /api/summary`
   - `GET /api/trend`
   - `GET /api/token-ledger`
   - `GET /api/token-chart`

### Sources used for provider pricing comparisons

- DeepSeek official pricing: `https://api-docs.deepseek.com/quick_start/pricing`
- Xiaomi MiMo official pricing: `https://platform.xiaomimimo.com/docs/en-US/price/pay-as-you-go`
- MiniMax pricing/caching: `https://platform.minimax.io/docs/guides/pricing-paygo`, `https://platform.minimax.io/docs/api-reference/text-prompt-caching`
- Moonshot/Kimi pricing: `https://platform.moonshot.ai/`
- Z.ai pricing: `https://docs.z.ai/guides/overview/pricing`
- Qwen/OpenRouter references used for rough comparison: `https://openrouter.ai/qwen/qwen3.7-plus`, `https://openrouter.ai/qwen/qwen3.7-max`
- OpenCode Go subscription bucket comparison: `https://opencode.ai/docs/go/`

## Current budget and routing constraints

- Luis has a Codex subscription of about `$200/month`, treated as about `$46–50/week`.
- Total agentic operations budget ceiling: about `$100/week` all-in.
- Therefore direct API fallback/experiments/emergency spillover should usually fit in roughly `$35–50/week`.
- Codex should remain default while healthy because it is already paid for, works well, and supports vision.
- Codex reserve must be protected because the past two weeks exhausted usage completely.
- DeepSeek V4 Pro has worked well as serious fallback.
- DeepSeek V4 Flash API is viable as default for selected cheap/lower-risk use cases.
- Local DeepSeek V4 Flash on acubens exists but is slow and effectively single-threaded; treat as local/offline resilience, not throughput default.
- DeepSeek currently lacks vision in Luis's routing context, so Codex/Gemini-class vision-capable models must be preserved for vision tasks.
- Gemini 2.5 Flash burned about `$300` of GCP credit in two days during an emergency fallback; Gemini-like lanes need hard spend caps.
- MiMo should be tested against DeepSeek before promotion.

## What to track next

### 1. Quota state snapshots

Frequency: **hourly at top-of-hour**, matching current daemon. Also fetch immediately on manual `fetch` and before high-cost routing changes if integrated into Hermes later.

Keep current fields and add normalized status fields:

```json
{
  "fetched_at": "ISO-8601 UTC",
  "plan_type": "pro",
  "allowed": true,
  "limit_reached": false,
  "session_used_pct": 5.0,
  "weekly_used_pct": 17.0,
  "session_reset_at": 1780700000,
  "weekly_reset_at": 1781137839,
  "session_reset_after_seconds": 8640,
  "weekly_reset_after_seconds": 449280,
  "hours_until_session_reset": 2.4,
  "hours_until_weekly_reset": 124.8,
  "raw_payload_present": true,
  "unknown_state": false
}
```

Requirements:

- Preserve `null` for unknown usage values. Do not coerce API dropouts to `0%`.
- Capture `allowed` and `limit_reached` from `raw_payload.rate_limit` so a reset/zero percent cannot falsely imply availability.
- Preserve aggregate and per-model windows for every `additional_rate_limits[]` row, not only Spark.

Why:

- The router needs to distinguish healthy, capped, unknown, reset, and transient-dropout states.

### 2. Codex token/API-call accounting rows

Frequency: **per Codex API call where Hermes can capture it**; otherwise **per Hermes session row and hourly correlation window** from `~/.hermes/state.db`.

This is distinct from the hourly Codex quota snapshot. The quota endpoint tells us percent-used and reset state; the token/accounting rows tell us what work caused that movement.

Suggested normalized row:

```json
{
  "id": "uuid",
  "started_at": "ISO-8601 UTC",
  "completed_at": "ISO-8601 UTC",
  "provider": "openai-codex",
  "model": "gpt-5.3-codex-spark",
  "session_id": "Hermes session id if available",
  "route_decision_id": "uuid if routed automatically",
  "codex_usage_window_start": "prior usage snapshot timestamp if correlated",
  "codex_usage_window_end": "next usage snapshot timestamp if correlated",
  "api_calls": 1,
  "input_tokens": 12345,
  "cache_read_tokens": 100000,
  "cache_write_tokens": 0,
  "output_tokens": 1200,
  "reasoning_tokens": 500,
  "prompt_tokens": 112345,
  "total_tokens": 114045,
  "cache_hit_pct": 88.98,
  "source": "provider_response|hermes_session|hourly_correlation",
  "quota_session_delta_pct": 0.2,
  "quota_weekly_delta_pct": 0.1,
  "latency_ms": 4200,
  "error_class": null
}
```

Requirements:

- Track Codex tokens and API calls with the same bucket vocabulary used for direct providers: input, cache read, cache write, output, reasoning, prompt, total.
- Keep `api_calls` explicit. Request count is not the cost model, but it is necessary for debugging throttling, retry loops, tool-call explosions, and provider-side anomalies.
- Preserve the correlation-window linkage to Codex quota snapshots. A token row is most useful when it can answer: “these tokens/API calls occurred between the two quota samples that moved weekly usage by X%.”
- If Hermes only has session-level aggregation, label the row `source: "hermes_session"` or `source: "hourly_correlation"` rather than pretending it is per-call.
- Do not assign direct dollar spend to Codex token rows. Codex subscription allocation belongs in weekly budget state; these rows measure consumption pressure, not marginal API dollars.

Why:

- The router must know not only “Codex is at 74% weekly,” but which token/API-call shape is pushing it there and whether equivalent work can safely move to DeepSeek/MiMo/local lanes.

### 3. Quota burn-rate projection rows

Frequency: **recompute hourly** after each new usage snapshot. Also recompute on dashboard/API request from current ledgers.

Derived fields:

```json
{
  "computed_at": "ISO-8601 UTC",
  "window_end": "latest usage snapshot time",
  "weekly_used_pct": 74.0,
  "session_used_pct": 12.0,
  "positive_weekly_burn_last_6h_pct": 2.0,
  "positive_weekly_burn_last_12h_pct": 4.0,
  "positive_weekly_burn_last_24h_pct": 11.0,
  "positive_weekly_burn_last_48h_pct": 23.0,
  "net_weekly_delta_last_24h_pct": 8.0,
  "positive_weekly_burn_rate_24h_pct_per_hour": 0.458,
  "projected_weekly_used_at_reset_pct": 96.0,
  "projected_hours_until_reserve_crossing": 11.2,
  "projection_confidence": "low|medium|high"
}
```

Requirements:

- Track **positive burn** separately from net deltas. Resets/dropouts can make net deltas negative and hide usage pressure.
- Suppress or label projections when the last samples are unknown/null.
- Keep multiple horizons: 6h, 12h, 24h, 48h, 168h.

Why:

- The routing policy should enter caution before the reserve is crossed, not after.

### 4. Protected reserve policy state

Frequency: **hourly** and exposed as latest JSON/API state.

Suggested row:

```json
{
  "computed_at": "ISO-8601 UTC",
  "policy_mode": "normal|caution|preserve|emergency",
  "protected_reserve_pct": 20.0,
  "codex_default_until_weekly_used_pct": 80.0,
  "weekly_used_pct": 74.0,
  "hours_until_weekly_reset": 52.0,
  "projected_weekly_used_at_reset_pct": 91.0,
  "recommended_default_provider": "openai-codex",
  "recommended_fallback_provider": "deepseek-v4-pro",
  "reason_codes": [
    "codex_below_reserve",
    "projection_crosses_threshold_before_reset"
  ]
}
```

Initial provisional modes:

| Mode | Trigger | Routing |
|---|---|---|
| `normal` | weekly safely below reserve risk | Codex default |
| `caution` | around 70–75% weekly or projected reserve crossing | move cheap/text-only/background work to local/DeepSeek Flash; use DeepSeek Pro for non-vision reasoning |
| `preserve` | around 80% weekly | Codex stops being default; preserve for vision/high-priority/final judgment |
| `emergency` | 90–95% weekly or projected reserve exhaustion | Codex only by explicit need |

Dynamic reserve inputs:

- base reserve, initially 20%;
- hours until weekly reset;
- recent burn rate;
- known upcoming high-priority/vision work;
- late-week depletion history;
- direct API spend already incurred;
- total weekly budget remaining.

Why:

- Luis is not sure 20% is the right reserve. The tracker should produce evidence to tune it.

### 5. Direct provider spend ledger

Frequency: **per provider API call**. Aggregate hourly/daily/weekly.

This is required before broad DeepSeek/MiMo/Gemini fallback can be trusted.

Suggested row:

```json
{
  "id": "uuid",
  "started_at": "ISO-8601 UTC",
  "completed_at": "ISO-8601 UTC",
  "provider": "deepseek",
  "model": "deepseek-v4-pro",
  "account_or_project_fingerprint": "masked/fingerprint only",
  "session_id": "Hermes session id if available",
  "task_id": "routing task id if available",
  "route_decision_id": "uuid if routed automatically",
  "api_calls": 1,
  "input_tokens": 12345,
  "cache_read_tokens": 100000,
  "cache_write_tokens": 0,
  "output_tokens": 1200,
  "reasoning_tokens": 500,
  "billed_usd": 0.0123,
  "latency_ms": 4200,
  "http_status": 200,
  "error_class": null,
  "fallback_or_retry": false
}
```

Requirements:

- Never store raw API keys.
- Store key/account/project fingerprints only if needed for attribution.
- Use provider-reported billable token fields when available; only estimate when the provider does not report enough detail.
- Record cache-hit/cache-write fields separately.

Why:

- The current Codex ledger can project hypothetical costs, but actual provider spend requires actual call accounting.

### 6. Weekly budget state

Frequency: **hourly** plus update on every direct provider call.

Suggested state:

```json
{
  "week_start": "ISO-8601 UTC",
  "week_end": "ISO-8601 UTC",
  "budget_cap_usd": 100.0,
  "codex_subscription_allocated_usd": 46.15,
  "direct_provider_spend_usd": 12.37,
  "experiment_spend_usd": 3.25,
  "emergency_buffer_usd": 10.0,
  "projected_total_agentic_spend_usd": 61.77,
  "remaining_budget_usd": 38.23,
  "budget_mode": "healthy|watch|preserve|hard_stop"
}
```

Why:

- Quota percentage and dollar budget are separate constraints. The router needs both.

### 7. Routing decision ledger

Frequency: **per automatic routing decision**, not merely hourly.

Suggested row:

```json
{
  "id": "uuid",
  "decided_at": "ISO-8601 UTC",
  "task_class": "vision|coding|review|summarization|classification|background|final_review",
  "requested_provider": "openai-codex",
  "selected_provider": "deepseek",
  "selected_model": "deepseek-v4-pro",
  "policy_mode": "preserve",
  "codex_weekly_used_pct": 83.0,
  "codex_session_used_pct": 20.0,
  "direct_provider_weekly_spend_usd": 18.5,
  "capability_requirements": ["text", "tools", "json"],
  "excluded_reasons": {
    "openai-codex": "protected_reserve",
    "deepseek-v4-flash": "task_requires_higher_quality",
    "gemini-2.5-flash": "spend_guard"
  },
  "user_override": false
}
```

Why:

- Model switching must be auditable. Without a decision ledger, bad routing policies cannot be debugged or backtested.

### 8. Capability matrix

Frequency: **manual/versioned updates**, with automatic checks where possible. Recompute eligibility per routing decision.

Model/provider fields:

```json
{
  "provider": "deepseek",
  "model": "deepseek-v4-pro",
  "supports_vision": false,
  "supports_tools": true,
  "supports_json_schema": true,
  "supports_reasoning": true,
  "context_window_tokens": 1000000,
  "max_output_tokens": 384000,
  "supports_cache_read_pricing": true,
  "cache_hit_usd_per_mtok": 0.003625,
  "input_usd_per_mtok": 0.435,
  "output_usd_per_mtok": 0.87,
  "known_quirks": ["reasoning_content_history_format"],
  "data_policy": "unknown|zero_retention|retained|training_possible"
}
```

Why:

- Cost-only switching will route vision/high-trust work to models that cannot satisfy the task.

### 9. Task outcome metrics

Frequency: **per task completion**, with periodic aggregation by task class/model.

Suggested fields:

```json
{
  "task_id": "uuid",
  "route_decision_id": "uuid",
  "completed_at": "ISO-8601 UTC",
  "task_class": "coding",
  "provider": "deepseek",
  "model": "deepseek-v4-pro",
  "success": true,
  "retry_count": 1,
  "human_intervention_count": 0,
  "tests_passed": true,
  "schema_valid": true,
  "tool_error_count": 0,
  "hallucination_flag": false,
  "latency_to_useful_answer_ms": 22000,
  "cost_usd": 0.08,
  "cleanup_cost_class": "none|low|medium|high"
}
```

Why:

- The real metric is not raw token cost. It is cost per successful useful task.

### 10. Backtest artifacts

Frequency: **nightly or on-demand**, not necessarily hourly.

Questions to answer:

- Would a 20% Codex reserve have prevented the last two runouts?
- Would 10%, 15%, 25%, or 30% have performed better?
- If fallback triggered at 70/75/80/85%, what direct API spend would have resulted?
- Which task classes consumed Codex late in the week?
- How much Codex would have remained for vision/high-priority tasks?
- Would total agentic spend have stayed below `$100/week`?

Artifact shape:

```json
{
  "backtest_id": "uuid",
  "created_at": "ISO-8601 UTC",
  "history_window": "2026-W23",
  "policy_variant": "reserve_20_projected_burn_24h",
  "codex_runout_prevented": true,
  "codex_reserved_pct_at_reset": 8.0,
  "estimated_direct_api_spend_usd": 22.4,
  "estimated_total_agentic_spend_usd": 68.6,
  "violated_budget_cap": false,
  "notes": ["projection would have entered caution 14h before preserve threshold"]
}
```

## Recommended collection frequencies

| Data class | Frequency | Why |
|---|---:|---|
| Codex usage snapshot | hourly top-of-hour | matches reset/quota ledger and enough for weekly burn projection |
| Codex token/API-call accounting | per Codex call if available; otherwise per Hermes session/window | explains what token/API-call shape caused quota movement |
| Token correlation with Hermes sessions | hourly after usage snapshot; also on dashboard read | aligns quota movement with prior sample window |
| Policy state / reserve mode | hourly after correlation | produces current routing recommendation |
| Direct provider spend | per API call | required for hard `$100/week` budget enforcement |
| Direct spend rollups | hourly/daily/weekly | dashboard and threshold checks |
| Routing decisions | per automatic decision | auditability and backtesting |
| Task outcomes | per task completion | cost-per-success, not just cost-per-token |
| Capability matrix | versioned/manual; validate weekly or on provider change | provider capabilities/pricing change over time |
| Provider pricing snapshot | daily while experimenting; weekly once stable | direct pricing changes can invalidate routing economics |
| Backtests | nightly or on-demand | tune reserve and fallback thresholds |

## Proposed repo artifacts

Add these as implementation targets:

```text
src/codex_usage_tracker/policy_state.py
src/codex_usage_tracker/codex_call_accounting.py
src/codex_usage_tracker/provider_spend.py
src/codex_usage_tracker/routing_decisions.py
src/codex_usage_tracker/capability_matrix.py
src/codex_usage_tracker/backtesting.py
```

Add ledgers under Atrium runtime or repo-local output:

```text
12_runtime/ledgers/model_routing/policy_state_ledger.jsonl
12_runtime/ledgers/model_routing/codex_token_call_ledger.jsonl
12_runtime/ledgers/model_routing/direct_provider_spend_ledger.jsonl
12_runtime/ledgers/model_routing/routing_decision_ledger.jsonl
12_runtime/ledgers/model_routing/task_outcome_ledger.jsonl
12_runtime/ledgers/model_routing/provider_pricing_snapshots.jsonl
12_runtime/ledgers/model_routing/backtests/*.json
```

Add dashboard/API routes:

```text
GET /api/policy-state
GET /api/budget-state
GET /api/codex-call-accounting
GET /api/provider-spend
GET /api/routing-decisions
GET /api/backtests/latest
```

## Implementation sequencing

1. **Policy state from existing data**
   - No new provider integration required.
   - Compute quota burn, reserve mode, and recommended default/fallback from Codex ledger + Hermes state DB.

2. **Codex token/API-call accounting**
   - Normalize existing Hermes Codex session rows into the same token-bucket vocabulary used by provider spend rows.
   - Add per-call capture later if the Hermes provider layer exposes individual Codex request/response usage.
   - Keep hourly correlation rows so quota deltas stay explainable even before per-call capture exists.

3. **Direct provider pricing table**
   - Version the DeepSeek/MiMo/MiniMax/Kimi/GLM/Qwen price assumptions.
   - Keep sources and timestamps.

4. **Direct provider spend capture**
   - Start with DeepSeek because it is already a serious fallback.
   - Add MiMo during benchmark testing.

5. **Dashboard budget cards**
   - Current Codex weekly/session state.
   - Protected reserve mode.
   - Codex token/API-call burn this week and in the current quota window.
   - Direct API spend this week.
   - Total projected agentic spend vs `$100/week`.

6. **Routing decision ledger**
   - Add only when Hermes is actually allowed to route automatically based on policy.

7. **Task outcome tracking**
   - Start coarse: task class, provider/model, success/failure, retry count, cost.
   - Add richer quality metrics later.

8. **Backtesting**
   - Use existing weeks to pick the initial reserve threshold.

## Design cautions

- Do not treat Codex weekly percentage as money. It is a quota/cap signal, not direct spend.
- Do not treat request counts as cost. Luis's workload is cache-heavy and token-heavy; cache-hit pricing dominates.
- Do not let negative/reset deltas hide positive usage burn.
- Do not coerce null API states to `0%`.
- Do not route vision tasks to DeepSeek unless a future DeepSeek vision-capable route exists and is validated.
- Do not allow Gemini-like fallbacks without explicit hard spend caps.
- Do not promote MiMo on price alone; benchmark task success against DeepSeek.
- Do not optimize for cheapest token if cleanup cost increases.

## Success criteria

The tracker is good enough for model-switching decisions when it can answer, from data:

1. How much Codex quota remains, and when does it reset?
2. At current burn, will Codex cross the protected reserve before reset?
3. What Codex token buckets and API-call counts caused the current quota movement?
4. Which provider should be default right now, and why?
5. How much direct API spend has happened this week?
6. Would switching this task violate capability requirements?
7. What did similar tasks cost and how often did they succeed on each model?
8. Would the last two Codex runouts have been prevented under this policy?
9. Will total agentic operations stay below `$100/week`?
