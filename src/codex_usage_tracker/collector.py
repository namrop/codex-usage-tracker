"""Cross-harness collection orchestration.

Collectors are read-only with respect to harness stores. The only writes are the
canonical usage, quota, and optional billing ledgers supplied by the caller.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from .claude_instances import ClaudeInstance, normalize_claude_instances
from .canonical_ledger import (
    AppendResult,
    ConflictSummary,
    TOKEN_FIELDS,
    ValidationError,
    _read_facts_without_side_effects,
    append_facts,
    append_facts_quarantined,
    append_facts_quarantined_reconciled,
    append_sqlite_facts,
    append_sqlite_facts_quarantined,
    append_sqlite_facts_quarantined_reconciled,
    canonical_json,
    normalize_fact,
    query_sqlite_facts,
)
from .quota import (
    codex_quota_observations,
    collect_claude_code_quota,
    collect_deepseek_quota,
    collect_kimi_code_quota,
    collect_opencode_go_quota as collect_exact_opencode_go_quota,
    collect_openrouter_quota,
    collect_z_ai_quota,
    derive_opencode_go_quotas,
)
from .usage_adapters import collect_claude_usage, collect_hermes_usage, collect_opencode_usage

ALLOWED_DOTENV_KEYS = frozenset(
    {
        "OPENROUTER_API_KEY",
        "DEEPSEEK_API_KEY",
        "KIMI_CODING_API_KEY",
        "Z_AI_API_KEY",
        "ZAI_API_KEY",
        "OPENCODE_GO_WORKSPACE_ID",
        "OPENCODE_GO_AUTH_COOKIE",
    }
)


def load_allowed_dotenv(
    path: str | Path | None,
    *,
    allowed_keys: Iterable[str] = ALLOWED_DOTENV_KEYS,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Parse only explicitly allowed dotenv keys without expansion or execution.

    Existing environment values win. ``export`` is accepted, but interpolation,
    command substitution, multiline values, and arbitrary dotenv keys are not.
    """
    result = dict(environ if environ is not None else os.environ)
    if path is None:
        return result
    target = Path(path).expanduser()
    if not target.exists():
        return result
    allowed = set(allowed_keys)
    for number, raw in enumerate(target.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"{target}:{number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in allowed:
            continue
        value = value.strip()
        if "\n" in value or "\r" in value:
            raise ValueError(f"{target}:{number}: multiline values are not allowed")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        # Values stay literal: no ${...}, backtick, or $(...) evaluation.
        result.setdefault(key, value)
    return result


def _source_result(discovered: int) -> dict[str, int]:
    return {"discovered": discovered}


def _ledger_backend(path: str | Path) -> str:
    suffix = Path(path).expanduser().suffix.casefold()
    if suffix == ".jsonl":
        return "jsonl"
    if suffix in {".sqlite3", ".sqlite", ".db"}:
        return "sqlite"
    raise ValueError(f"unsupported canonical ledger suffix: {suffix or '<none>'}")


def _append_canonical_ledger(
    path: str | Path,
    facts: Iterable[dict[str, Any]],
    *,
    fact_type: str,
    dry_run: bool,
):
    """Dispatch only explicitly supported ledger suffixes with a type binding."""
    backend = _ledger_backend(path)
    rows = list(facts)
    if backend == "sqlite":
        return append_sqlite_facts(path, rows, fact_type=fact_type, dry_run=dry_run)
    if any(not isinstance(row, dict) or row.get("fact_type") != fact_type for row in rows):
        raise ValidationError(f"ledger requires fact_type {fact_type}")
    return append_facts(path, rows, dry_run=dry_run)


def _append_canonical_ledger_quarantined(
    path: str | Path,
    facts: Iterable[dict[str, Any]],
    *,
    fact_type: str,
    dry_run: bool,
    equivalent_changed_fields: frozenset[str] = frozenset(),
) -> tuple[AppendResult, tuple[ConflictSummary, ...]]:
    """Persist one source batch without weakening the strict ledger APIs."""
    backend = _ledger_backend(path)
    rows = list(facts)
    if backend == "sqlite":
        return append_sqlite_facts_quarantined(
            path,
            rows,
            fact_type=fact_type,
            dry_run=dry_run,
            equivalent_changed_fields=equivalent_changed_fields,
        )
    if any(not isinstance(row, dict) or row.get("fact_type") != fact_type for row in rows):
        raise ValidationError(f"ledger requires fact_type {fact_type}")
    return append_facts_quarantined(
        path,
        rows,
        dry_run=dry_run,
        equivalent_changed_fields=equivalent_changed_fields,
    )


def _append_canonical_ledger_quarantined_reconciled(
    path: str | Path,
    facts: Iterable[dict[str, Any]],
    *,
    fact_type: str,
    dry_run: bool,
    reconcile: Any,
    equivalent_changed_fields: frozenset[str] = frozenset(),
) -> tuple[AppendResult, tuple[ConflictSummary, ...], Any]:
    """Run semantic reconciliation inside the canonical writer boundary."""
    backend = _ledger_backend(path)
    rows = list(facts)
    if backend == "sqlite":
        return append_sqlite_facts_quarantined_reconciled(
            path,
            rows,
            fact_type=fact_type,
            reconcile=reconcile,
            dry_run=dry_run,
            equivalent_changed_fields=equivalent_changed_fields,
        )
    if any(not isinstance(row, dict) or row.get("fact_type") != fact_type for row in rows):
        raise ValidationError(f"ledger requires fact_type {fact_type}")
    return append_facts_quarantined_reconciled(
        path,
        rows,
        reconcile=reconcile,
        dry_run=dry_run,
        equivalent_changed_fields=equivalent_changed_fields,
    )


def _aggregate_append_results(
    batches: Iterable[tuple[AppendResult, int]],
) -> dict[str, int]:
    discovered = appended = replayed = quarantined = 0
    for result, conflict_count in batches:
        discovered += result.discovered
        appended += result.appended
        replayed += result.replayed
        quarantined += conflict_count
    summary = {
        "discovered": discovered,
        "appended": appended,
        "replayed": replayed,
    }
    if quarantined:
        summary["quarantined"] = quarantined
    return summary


def _validate_distinct_ledger_paths(bindings: list[tuple[str | Path, str]]) -> None:
    resolved: list[tuple[Path, str]] = []
    for raw_path, fact_type in bindings:
        path = Path(raw_path).expanduser()
        canonical = path.resolve(strict=False)
        for other, other_type in resolved:
            aliases = canonical == other
            if not aliases and os.path.lexists(path) and os.path.lexists(other):
                try:
                    aliases = os.path.samefile(path, other)
                except OSError:
                    aliases = False
            if aliases:
                raise ValueError(
                    f"canonical usage, quota, and billing ledgers must use distinct paths ({fact_type}, {other_type})"
                )
        resolved.append((canonical, fact_type))


def _preflight_ledger_binding(path: str | Path, fact_type: str, *, dry_run: bool) -> None:
    target = Path(path).expanduser()
    backend = _ledger_backend(target)
    if not os.path.lexists(target):
        return
    if backend == "sqlite":
        if dry_run:
            append_sqlite_facts(target, [], fact_type=fact_type, dry_run=True)
        else:
            query_sqlite_facts(target, fact_type=fact_type, limit=0)
        return
    rows = _read_facts_without_side_effects(target)
    if any(row.get("fact_type") != fact_type for row in rows):
        raise ValidationError(f"ledger requires fact_type {fact_type}")


_CLAUDE_FINALIZATION_MUTABLE_FIELDS = frozenset(
    {"occurred_at", "recorded_at", "request_status", *TOKEN_FIELDS}
)


def _fact_sha256(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(row)).encode("utf-8")).hexdigest()


def _identity_sha256(row: Mapping[str, Any]) -> str:
    namespace = str(row.get("source_namespace") or "")
    event_id = str(row.get("source_event_id") or "")
    return hashlib.sha256(f"{namespace}\0{event_id}".encode("utf-8")).hexdigest()


def _reconcile_finalized_claude_rows(
    existing: list[dict[str, Any]],
    rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], tuple[int, list[dict[str, Any]]]]:
    """Convert safe monotonic Claude stream finalizations into signed corrections.

    The caller supplies the complete canonical snapshot while holding the writer
    boundary. The original attempt remains immutable; only one deterministic
    signed token delta may be added. Differing incoming variants, decreases,
    unrelated mutations, and non-generated corrections stay quarantined.
    """
    incoming = [normalize_fact(row) for row in rows]
    existing = [normalize_fact(row) for row in existing]
    if not existing:
        return incoming, (0, [])

    baselines: dict[tuple[str, str], dict[str, Any]] = {}
    corrections: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in existing:
        if row.get("record_kind") == "correction":
            target_namespace = row.get("corrects_source_namespace")
            target_event = row.get("corrects_source_event_id")
            if isinstance(target_namespace, str) and isinstance(target_event, str):
                corrections.setdefault((target_namespace, target_event), []).append(row)
            continue
        identity = (str(row.get("source_namespace")), str(row.get("source_event_id")))
        baselines[identity] = row

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in incoming:
        identity = (str(row.get("source_namespace")), str(row.get("source_event_id")))
        grouped.setdefault(identity, []).append(row)

    reconciled: list[dict[str, Any]] = []
    generated = 0
    resolutions: list[dict[str, Any]] = []
    for identity, variants in grouped.items():
        if len({canonical_json(row) for row in variants}) > 1:
            # Preserve all variants so the canonical writer quarantines the
            # ambiguous identity instead of manufacturing multiple deltas.
            reconciled.extend(variants)
            continue
        row = variants[0]
        baseline = baselines.get(identity)
        if baseline is None:
            reconciled.extend(variants)
            continue
        changed_fields = sorted(
            key for key in set(baseline) | set(row) if baseline.get(key) != row.get(key)
        )
        target_corrections = corrections.get(identity, [])
        safe_generated_corrections = all(
            correction.get("x_claude_stream_finalization") is True
            for correction in target_corrections
        )
        eligible = (
            bool(changed_fields)
            and set(changed_fields).issubset(_CLAUDE_FINALIZATION_MUTABLE_FIELDS)
            and baseline.get("harness") == "claude_code"
            and row.get("harness") == "claude_code"
            and baseline.get("request_status") == "unknown"
            and row.get("request_status") == "ok"
            and safe_generated_corrections
        )
        deltas: dict[str, int] = {}
        if eligible:
            for field in TOKEN_FIELDS:
                effective = int(baseline.get(field) or 0) + sum(
                    int(correction.get(field) or 0) for correction in target_corrections
                )
                current = int(row.get(field) or 0)
                deltas[field] = current - effective
                if current < effective:
                    eligible = False
                    break
            # A prior generated finalization may prove idempotence, but it may
            # not be incrementally extended. A later differing final total is
            # ambiguous and remains quarantined.
            if target_corrections and any(deltas.values()):
                eligible = False
        if not eligible:
            reconciled.extend(variants)
            continue

        # Replay one immutable baseline per exact incoming duplicate so the
        # generic writer preserves its discovered/replayed accounting.
        reconciled.extend([baseline] * len(variants))
        incoming_sha = _fact_sha256(row)
        if not target_corrections:
            correction = dict(row)
            correction.update(
                {
                    "source_event_id": f"{row['source_event_id']}:stream-finalization",
                    "event_uid": None,
                    "logical_call_id": None,
                    "provider_request_id": None,
                    "record_kind": "correction",
                    "request_status": None,
                    "error_class": None,
                    "latency_ms": None,
                    "attempt_no": None,
                    "reconstructed_call_count": None,
                    "estimated_cost_usd": None,
                    "actual_cost_usd": None,
                    "cost_source": None,
                    "pricing_version": None,
                    "cost_status": "included",
                    "corrects_source_namespace": row["source_namespace"],
                    "corrects_source_event_id": row["source_event_id"],
                    "x_claude_stream_finalization": True,
                    "x_final_request_status": "ok",
                    "x_finalized_source_sha256": incoming_sha,
                }
            )
            for field, delta in deltas.items():
                correction[field] = delta
            reconciled.append(normalize_fact(correction))
            generated += 1
        resolutions.append(
            {
                "source": "claude_code",
                "resolution": "canonical_correction",
                "identity_sha256": _identity_sha256(row),
                "existing_sha256": _fact_sha256(baseline),
                "incoming_sha256": incoming_sha,
                "changed_fields": changed_fields,
            }
        )
    return reconciled, (generated, resolutions)


def collect_all(
    *,
    state_db: str | Path,
    claude_root: str | Path = "~/.claude/projects",
    claude_instances: (
        Iterable[ClaudeInstance | Mapping[str, Any] | tuple[Any, Any]]
        | Mapping[str, Any]
        | None
    ) = None,
    opencode_dbs: Iterable[str | Path],
    codex_ledger: str | Path,
    usage_ledger: str | Path,
    quota_ledger: str | Path,
    billing_ledger: str | Path | None = None,
    dotenv: str | Path | None = None,
    claude_quota_command: str | Path | None = None,
    claude_probe_dir: str | Path = "~/.local/state/codex-usage-tracker/claude-probe",
    claude_quota_timeout: float = 45.0,
    live_quota: bool = True,
    dry_run: bool = False,
    environment: Mapping[str, str] | None = None,
    source_prefix: str = "sol",
    scope: str = "all",
    codex_quota_history: bool = False,
    codex_quota_history_since: str | None = None,
    strict_sources: bool = False,
) -> dict[str, Any]:
    """Collect independently persistable usage and/or quota source batches."""
    configured_claude_instances = normalize_claude_instances(claude_instances)
    claude_usage_source_keys = (
        {instance.source_key for instance in configured_claude_instances}
        if configured_claude_instances
        else {"claude_code"}
    )
    if scope not in {"all", "usage", "quota"}:
        raise ValueError("scope must be one of: all, usage, quota")
    if codex_quota_history:
        if scope != "quota":
            raise ValueError("Codex quota history requires scope quota")
        if live_quota:
            raise ValueError("Codex quota history requires live quota to be disabled")
        if not codex_quota_history_since:
            raise ValueError("Codex quota history requires history-since")
    elif codex_quota_history_since is not None:
        raise ValueError("history-since requires Codex quota history")
    collect_usage_scope = scope in {"all", "usage"}
    collect_quota_scope = scope in {"all", "quota"}

    # Reject aliasing and wrong pre-existing type bindings before reading any
    # in-scope source or writing any in-scope destination.
    bindings: list[tuple[str | Path, str]] = []
    if collect_usage_scope:
        bindings.append((usage_ledger, "usage_event_v1"))
    if collect_quota_scope:
        bindings.append((quota_ledger, "quota_observation_v1"))
    if scope == "all" and billing_ledger is not None:
        bindings.append((billing_ledger, "billing_fact_v1"))
    _validate_distinct_ledger_paths(bindings)
    for ledger_path, ledger_fact_type in bindings:
        _ledger_backend(ledger_path)
        _preflight_ledger_binding(ledger_path, ledger_fact_type, dry_run=dry_run)

    env = (
        load_allowed_dotenv(dotenv, environ=environment)
        if collect_quota_scope and not codex_quota_history
        else {}
    )
    sources: dict[str, dict[str, int]] = {}
    warnings: list[dict[str, str]] = []
    conflicts: list[dict[str, Any]] = []
    stabilized_replays: list[dict[str, Any]] = []
    generated_corrections = 0

    def collect_source(
        source: str,
        collect: Any,
        *,
        require_rows: bool = False,
    ) -> list[dict[str, Any]]:
        try:
            rows = list(collect())
            if require_rows and not rows:
                raise ValueError(f"{source} returned no rows")
        except Exception as exc:
            warnings.append({"source": source, "error": type(exc).__name__})
            sources[source] = _source_result(0)
            return []
        sources[source] = _source_result(len(rows))
        return rows

    def persist_source(
        source: str,
        path: str | Path,
        rows: Iterable[dict[str, Any]],
        *,
        fact_type: str,
    ) -> tuple[AppendResult, int]:
        nonlocal generated_corrections
        prepared_rows = list(rows)
        equivalent_fields = (
            frozenset({"occurred_at", "recorded_at"})
            if source in claude_usage_source_keys and fact_type == "usage_event_v1"
            else frozenset()
        )
        if source in claude_usage_source_keys and fact_type == "usage_event_v1":
            append_result, summaries, reconciliation = (
                _append_canonical_ledger_quarantined_reconciled(
                    path,
                    prepared_rows,
                    fact_type=fact_type,
                    dry_run=dry_run,
                    reconcile=_reconcile_finalized_claude_rows,
                    equivalent_changed_fields=equivalent_fields,
                )
            )
            generated, resolutions = reconciliation
            generated_corrections += generated
            stabilized_replays.extend(
                {**resolution, "source": source} for resolution in resolutions
            )
        else:
            append_result, summaries = _append_canonical_ledger_quarantined(
                path,
                prepared_rows,
                fact_type=fact_type,
                dry_run=dry_run,
                equivalent_changed_fields=equivalent_fields,
            )
        quarantined = [summary for summary in summaries if summary.resolution == "quarantined"]
        stabilized = [summary for summary in summaries if summary.resolution == "canonical_replay"]
        if quarantined:
            warnings.append(
                {
                    "source": source,
                    "error": "IdentityConflictError",
                    "quarantined": str(len(quarantined)),
                }
            )
        for summary in quarantined:
            conflicts.append(
                {
                    "source": source,
                    "error": "IdentityConflictError",
                    "identity_sha256": summary.identity_sha256,
                    "existing_sha256": summary.existing_sha256,
                    "incoming_sha256": summary.incoming_sha256,
                    "changed_fields": list(summary.changed_fields),
                }
            )
        for summary in stabilized:
            stabilized_replays.append(
                {
                    "source": source,
                    "resolution": "canonical_replay",
                    "identity_sha256": summary.identity_sha256,
                    "existing_sha256": summary.existing_sha256,
                    "incoming_sha256": summary.incoming_sha256,
                    "changed_fields": list(summary.changed_fields),
                }
            )
        return append_result, len(quarantined)

    paths: dict[str, str] = {}
    result: dict[str, Any] = {
        "dry_run": dry_run,
        "scope": scope,
        "paths": paths,
        "sources": sources,
        "warnings": warnings,
        "conflicts": conflicts,
        "stabilized_replays": stabilized_replays,
    }
    opencode: list[dict[str, Any]] | None = None

    if collect_usage_scope:
        hermes = collect_source(
            "hermes",
            lambda: collect_hermes_usage(state_db, f"{source_prefix}:hermes"),
        )
        claude_sources: list[tuple[str, list[dict[str, Any]]]] = []
        if configured_claude_instances:
            for instance in configured_claude_instances:
                source = instance.source_key
                rows = collect_source(
                    source,
                    lambda instance=instance: collect_claude_usage(
                        Path(instance.config_dir) / "projects",
                        instance.usage_namespace(source_prefix),
                    ),
                )
                claude_sources.append((source, rows))
        else:
            claude_sources.append(
                (
                    "claude_code",
                    collect_source(
                        "claude_code",
                        lambda: collect_claude_usage(
                            claude_root, f"{source_prefix}:claude-code"
                        ),
                    ),
                )
            )
        opencode = collect_source(
            "opencode",
            lambda: collect_opencode_usage(opencode_dbs, f"{source_prefix}:opencode"),
        )
        usage_batches = [
            persist_source("hermes", usage_ledger, hermes, fact_type="usage_event_v1"),
            *(
                persist_source(source, usage_ledger, rows, fact_type="usage_event_v1")
                for source, rows in claude_sources
            ),
            persist_source("opencode", usage_ledger, opencode, fact_type="usage_event_v1"),
        ]
        result["usage"] = _aggregate_append_results(usage_batches)
        result["generated_corrections"] = generated_corrections
        paths["usage_ledger"] = str(Path(usage_ledger).expanduser())

    if collect_quota_scope:
        quota_sources: list[tuple[str, list[dict[str, Any]]]] = []
        if codex_quota_history:
            codex_history = codex_quota_observations(
                codex_ledger,
                f"{source_prefix}:codex-quota",
                include_history=True,
                history_since=codex_quota_history_since,
            )
            sources["codex_quota"] = _source_result(len(codex_history))
            quota_sources.append(("codex_quota", codex_history))
        else:
            if opencode is None:
                opencode = collect_source(
                    "opencode",
                    lambda: collect_opencode_usage(opencode_dbs, f"{source_prefix}:opencode"),
                )
            quota_sources.append(
                (
                    "codex_quota",
                    collect_source(
                        "codex_quota",
                        lambda: codex_quota_observations(
                            codex_ledger,
                            f"{source_prefix}:codex-quota",
                        ),
                    ),
                )
            )
            opencode_go_workspace = env.get("OPENCODE_GO_WORKSPACE_ID")
            opencode_go_cookie = env.get("OPENCODE_GO_AUTH_COOKIE")
            if live_quota and opencode_go_workspace and opencode_go_cookie:
                opencode_go_rows = collect_source(
                    "opencode_go_quota",
                    lambda: collect_exact_opencode_go_quota(
                        opencode_go_workspace,
                        opencode_go_cookie,
                        f"{source_prefix}:opencode-go",
                    ),
                    require_rows=True,
                )
                if opencode_go_rows:
                    quota_sources.append(("opencode_go_quota", opencode_go_rows))
                else:
                    estimated_source = "opencode_go_quota_estimated"
                    estimated_rows = collect_source(
                        estimated_source,
                        lambda: derive_opencode_go_quotas(
                            opencode or [],
                            f"{source_prefix}:opencode-go",
                        ),
                    )
                    quota_sources.append((estimated_source, estimated_rows))
            else:
                opencode_go_rows = collect_source(
                    "opencode_go_quota",
                    lambda: derive_opencode_go_quotas(
                        opencode or [],
                        f"{source_prefix}:opencode-go",
                    ),
                )
                quota_sources.append(("opencode_go_quota", opencode_go_rows))

            if live_quota:
                if claude_quota_command and not dry_run:
                    if configured_claude_instances:
                        for instance in configured_claude_instances:
                            source = instance.quota_source_key
                            quota_sources.append(
                                (
                                    source,
                                    collect_source(
                                        source,
                                        lambda instance=instance: collect_claude_code_quota(
                                            instance.quota_namespace(source_prefix),
                                            claude_command=claude_quota_command,
                                            probe_dir=instance.probe_dir(claude_probe_dir),
                                            timeout=claude_quota_timeout,
                                            claude_config_dir=instance.quota_config_dir,
                                            account_ref=instance.account_ref,
                                        ),
                                    ),
                                )
                            )
                    else:
                        quota_sources.append(
                            (
                                "claude_code_quota",
                                collect_source(
                                    "claude_code_quota",
                                    lambda: collect_claude_code_quota(
                                        f"{source_prefix}:claude-code-quota",
                                        claude_command=claude_quota_command,
                                        probe_dir=claude_probe_dir,
                                        timeout=claude_quota_timeout,
                                    ),
                                ),
                            )
                        )
                elif claude_quota_command:
                    # The Claude `/usage` capture creates a probe directory, lock,
                    # and tmux session; dry-run remains process/filesystem safe.
                    if configured_claude_instances:
                        for instance in configured_claude_instances:
                            sources[instance.quota_source_key] = _source_result(0)
                    else:
                        sources["claude_code_quota"] = _source_result(0)
                openrouter_key = env.get("OPENROUTER_API_KEY")
                if openrouter_key:
                    quota_sources.append(
                        (
                            "openrouter_quota",
                            collect_source(
                                "openrouter_quota",
                                lambda: collect_openrouter_quota(
                                    openrouter_key,
                                    f"{source_prefix}:openrouter-quota",
                                ),
                            ),
                        )
                    )
                deepseek_key = env.get("DEEPSEEK_API_KEY")
                if deepseek_key:
                    quota_sources.append(
                        (
                            "deepseek_quota",
                            collect_source(
                                "deepseek_quota",
                                lambda: collect_deepseek_quota(
                                    deepseek_key,
                                    f"{source_prefix}:deepseek-quota",
                                ),
                            ),
                        )
                    )

                kimi_key = env.get("KIMI_CODING_API_KEY")
                if kimi_key:
                    quota_sources.append(
                        (
                            "kimi_code_quota",
                            collect_source(
                                "kimi_code_quota",
                                lambda: collect_kimi_code_quota(
                                    kimi_key,
                                    f"{source_prefix}:kimi-code-quota",
                                ),
                            ),
                        )
                    )

                z_ai_key = env.get("Z_AI_API_KEY") or env.get("ZAI_API_KEY")
                if z_ai_key:
                    quota_sources.append(
                        (
                            "z_ai_quota",
                            collect_source(
                                "z_ai_quota",
                                lambda: collect_z_ai_quota(
                                    z_ai_key,
                                    f"{source_prefix}:z-ai-quota",
                                ),
                            ),
                        )
                    )

        quota_batches = [
            persist_source(
                source,
                quota_ledger,
                rows,
                fact_type="quota_observation_v1",
            )
            for source, rows in quota_sources
        ]
        result["quotas"] = _aggregate_append_results(quota_batches)
        paths["quota_ledger"] = str(Path(quota_ledger).expanduser())

    if scope == "all" and billing_ledger is not None:
        billing_result = _append_canonical_ledger(
            billing_ledger,
            [],
            fact_type="billing_fact_v1",
            dry_run=dry_run,
        )
        paths["billing_ledger"] = str(Path(billing_ledger).expanduser())
        result["billing"] = asdict(billing_result)
    if strict_sources and (warnings or conflicts):
        raise RuntimeError("collection incomplete under strict source policy")
    return result
