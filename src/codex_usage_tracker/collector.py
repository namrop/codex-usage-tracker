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
    ValidationError,
    _read_facts_without_side_effects,
    append_facts,
    append_sqlite_facts,
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
) -> dict[str, Any]:
    """Collect all local harness history and optional live quota snapshots."""
    # Reject aliasing and wrong pre-existing type bindings before reading any
    # source or writing any destination.
    bindings = [
        (usage_ledger, "usage_event_v1"),
        (quota_ledger, "quota_observation_v1"),
    ]
    if billing_ledger is not None:
        bindings.append((billing_ledger, "billing_fact_v1"))
    _validate_distinct_ledger_paths(bindings)
    for ledger_path, ledger_fact_type in bindings:
        _ledger_backend(ledger_path)
        _preflight_ledger_binding(ledger_path, ledger_fact_type, dry_run=dry_run)

    env = load_allowed_dotenv(dotenv, environ=environment)
    sources: dict[str, dict[str, int]] = {}
    warnings: list[dict[str, str]] = []

    def collect_source(source: str, collect: Any) -> list[dict[str, Any]]:
        try:
            rows = list(collect())
        except Exception as exc:
            warnings.append({"source": source, "error": type(exc).__name__})
            sources[source] = _source_result(0)
            return []
        sources[source] = _source_result(len(rows))
        return rows

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

    usage = [*hermes, *claude, *opencode]
    usage_result = _append_canonical_ledger(
        usage_ledger, usage, fact_type="usage_event_v1", dry_run=dry_run
    )

    quotas = collect_source(
        "codex_quota",
        lambda: codex_quota_observations(codex_ledger, f"{source_prefix}:codex-quota"),
    )
    go = derive_opencode_go_quotas(opencode, f"{source_prefix}:opencode-go")
    sources["opencode_go_quota"] = _source_result(len(go))
    quotas.extend(go)

    if live_quota:
        def collect_live(source: str, collect: Any) -> None:
            try:
                rows = list(collect())
            except Exception as exc:
                # Summaries must never copy provider payloads, credentials, URLs,
                # or exception text. The exception class is enough to route the
                # operator to logs while allowing other collectors to finish.
                warnings.append({"source": source, "error": type(exc).__name__})
                sources[source] = _source_result(0)
                return
            quotas.extend(rows)
            sources[source] = _source_result(len(rows))

        if claude_quota_command and not dry_run:
            collect_live(
                "claude_code_quota",
                lambda: collect_claude_code_quota(
                    f"{source_prefix}:claude-code-quota",
                    claude_command=claude_quota_command,
                    probe_dir=claude_probe_dir,
                    timeout=claude_quota_timeout,
                ),
            )
        elif claude_quota_command:
            # The Claude `/usage` capture creates a probe directory, lock, and
            # tmux session; a dry run must remain filesystem/process side-effect free.
            sources["claude_code_quota"] = _source_result(0)
        openrouter_key = env.get("OPENROUTER_API_KEY")
        if openrouter_key:
            collect_live(
                "openrouter_quota",
                lambda: collect_openrouter_quota(openrouter_key, f"{source_prefix}:openrouter-quota"),
            )
        deepseek_key = env.get("DEEPSEEK_API_KEY")
        if deepseek_key:
            collect_live(
                "deepseek_quota",
                lambda: collect_deepseek_quota(deepseek_key, f"{source_prefix}:deepseek-quota"),
            )

    quota_result = _append_canonical_ledger(
        quota_ledger, quotas, fact_type="quota_observation_v1", dry_run=dry_run
    )
    paths = {
        "usage_ledger": str(Path(usage_ledger).expanduser()),
        "quota_ledger": str(Path(quota_ledger).expanduser()),
    }
    result = {
        "dry_run": dry_run,
        "paths": paths,
        "sources": sources,
        "warnings": warnings,
        "usage": asdict(usage_result),
        "quotas": asdict(quota_result),
    }
    if billing_ledger is not None:
        billing_result = _append_canonical_ledger(
            billing_ledger, [], fact_type="billing_fact_v1", dry_run=dry_run
        )
        paths["billing_ledger"] = str(Path(billing_ledger).expanduser())
        result["billing"] = asdict(billing_result)
    return result
