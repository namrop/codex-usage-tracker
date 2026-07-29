"""Cross-harness collection orchestration.

Collectors are read-only with respect to harness stores. The only writes are the
canonical usage, quota, and optional billing ledgers supplied by the caller.
"""
from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from .canonical_ledger import (
    AppendResult,
    ConflictSummary,
    ValidationError,
    _read_facts_without_side_effects,
    append_facts,
    append_facts_quarantined,
    append_sqlite_facts,
    append_sqlite_facts_quarantined,
    query_sqlite_facts,
)
from .quota import (
    codex_quota_observations,
    collect_claude_code_quota,
    collect_deepseek_quota,
    collect_openrouter_quota,
    derive_opencode_go_quotas,
)
from .usage_adapters import collect_claude_usage, collect_hermes_usage, collect_opencode_usage

ALLOWED_DOTENV_KEYS = frozenset(
    {
        "OPENROUTER_API_KEY",
        "DEEPSEEK_API_KEY",
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


def collect_all(
    *,
    state_db: str | Path,
    claude_root: str | Path,
    opencode_dbs: Iterable[str | Path],
    codex_ledger: str | Path,
    usage_ledger: str | Path,
    quota_ledger: str | Path,
    billing_ledger: str | Path | None = None,
    dotenv: str | Path | None = None,
    claude_quota_command: str | Path | None = None,
    claude_probe_dir: str | Path = "~/.local/state/codex-usage-tracker/claude-probe",
    claude_quota_timeout: float = 25.0,
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

    def collect_source(source: str, collect: Any) -> list[dict[str, Any]]:
        try:
            rows = list(collect())
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
        append_result, summaries = _append_canonical_ledger_quarantined(
            path,
            rows,
            fact_type=fact_type,
            dry_run=dry_run,
            equivalent_changed_fields=(
                frozenset({"occurred_at", "recorded_at"})
                if source == "claude_code" and fact_type == "usage_event_v1"
                else frozenset()
            ),
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
        claude = collect_source(
            "claude_code",
            lambda: collect_claude_usage(claude_root, f"{source_prefix}:claude-code"),
        )
        opencode = collect_source(
            "opencode",
            lambda: collect_opencode_usage(opencode_dbs, f"{source_prefix}:opencode"),
        )
        usage_batches = [
            persist_source("hermes", usage_ledger, hermes, fact_type="usage_event_v1"),
            persist_source("claude_code", usage_ledger, claude, fact_type="usage_event_v1"),
            persist_source("opencode", usage_ledger, opencode, fact_type="usage_event_v1"),
        ]
        result["usage"] = _aggregate_append_results(usage_batches)
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
            quota_sources.append(
                (
                    "opencode_go_quota",
                    collect_source(
                        "opencode_go_quota",
                        lambda: derive_opencode_go_quotas(
                            opencode or [],
                            f"{source_prefix}:opencode-go",
                        ),
                    ),
                )
            )

            if live_quota:
                if claude_quota_command and not dry_run:
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
