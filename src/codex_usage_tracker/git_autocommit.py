"""Scoped git auto-commit helpers for Codex usage ledgers."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


@dataclass(frozen=True)
class CommitResult:
    """Result from a scoped ledger commit attempt."""

    committed: bool
    message: str
    commit_sha: str | None = None


def _run_git(repo_root: Path, args: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def _repo_relative(repo_root: Path, path: Path) -> str:
    resolved_repo = repo_root.resolve()
    resolved_path = path.resolve()
    try:
        return str(resolved_path.relative_to(resolved_repo))
    except ValueError as exc:
        raise ValueError(f"Path is outside repo root: {resolved_path}") from exc


def _staged_paths(repo_root: Path) -> set[str]:
    result = _run_git(repo_root, ["diff", "--cached", "--name-only"])
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _changed_paths(repo_root: Path, paths: Iterable[str]) -> set[str]:
    result = _run_git(repo_root, ["status", "--porcelain", "--", *paths])
    changed: set[str] = set()
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        # Porcelain v1 path begins at column 4. Rename records include "old -> new";
        # use the new side because that is what an explicit git add would stage.
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        changed.add(path)
    return changed


def validate_jsonl(path: Path) -> int:
    """Validate a JSONL file and return the number of non-empty rows."""

    rows = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
            rows += 1
    return rows


def commit_ledger(
    *,
    repo_root: str | Path,
    ledger_path: str | Path,
    message: str = "Update Codex usage ledger",
    dry_run: bool = False,
) -> CommitResult:
    """Commit only the configured ledger path, refusing unrelated staged changes."""

    root = Path(repo_root).expanduser().resolve()
    ledger = Path(ledger_path).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"Repo root does not exist: {root}")
    if not ledger.exists():
        raise FileNotFoundError(f"Ledger does not exist: {ledger}")

    relative_ledger = _repo_relative(root, ledger)
    allowed = {relative_ledger}
    rows = validate_jsonl(ledger)

    pre_staged = _staged_paths(root)
    unrelated_staged = pre_staged - allowed
    if unrelated_staged:
        raise RuntimeError(
            "Refusing to auto-commit while unrelated paths are staged: "
            + ", ".join(sorted(unrelated_staged))
        )

    changed = _changed_paths(root, [relative_ledger])
    if not changed and relative_ledger not in pre_staged:
        return CommitResult(False, f"No ledger changes to commit ({relative_ledger}; rows={rows}).")

    if dry_run:
        return CommitResult(False, f"Would commit {relative_ledger} (rows={rows}).")

    _run_git(root, ["add", "--", relative_ledger])
    staged_after = _staged_paths(root)
    unrelated_after = staged_after - allowed
    if unrelated_after:
        raise RuntimeError(
            "Refusing to commit because unrelated staged paths appeared: "
            + ", ".join(sorted(unrelated_after))
        )
    if relative_ledger not in staged_after:
        return CommitResult(False, f"No staged ledger changes after add ({relative_ledger}; rows={rows}).")

    _run_git(root, ["diff", "--cached", "--check"])
    _run_git(root, ["commit", "-m", message])
    sha = _run_git(root, ["rev-parse", "--short", "HEAD"]).stdout.strip()
    return CommitResult(True, f"Committed {relative_ledger} (rows={rows}).", sha)
