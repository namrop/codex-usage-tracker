from __future__ import annotations

import json
import subprocess

import pytest

from codex_usage_tracker.git_autocommit import commit_ledger, validate_jsonl


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test Runner")
    ledger = repo / "12_runtime" / "ledgers" / "codex_usage" / "codex_usage_ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"fetched_at": "2026-06-08T00:00:00+00:00"}) + "\n", encoding="utf-8")
    _git(repo, "add", str(ledger.relative_to(repo)))
    _git(repo, "commit", "-m", "Initial ledger")
    return repo, ledger


def test_validate_jsonl_counts_rows(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text('{"a": 1}\n\n{"b": 2}\n', encoding="utf-8")

    assert validate_jsonl(ledger) == 2


def test_validate_jsonl_rejects_bad_rows(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text('{"a": 1}\nnot-json\n', encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid JSONL"):
        validate_jsonl(ledger)


def test_commit_ledger_commits_only_ledger_path(tmp_path):
    repo, ledger = _init_repo(tmp_path)
    ledger.write_text(
        ledger.read_text(encoding="utf-8")
        + json.dumps({"fetched_at": "2026-06-08T01:00:00+00:00"})
        + "\n",
        encoding="utf-8",
    )
    other = repo / "unrelated.txt"
    other.write_text("do not commit\n", encoding="utf-8")

    result = commit_ledger(repo_root=repo, ledger_path=ledger, message="Update Codex usage ledger")

    assert result.committed is True
    assert result.commit_sha
    committed_paths = _git(repo, "show", "--name-only", "--format=", "HEAD").stdout.splitlines()
    assert committed_paths == ["12_runtime/ledgers/codex_usage/codex_usage_ledger.jsonl"]
    assert _git(repo, "status", "--porcelain", "--", "unrelated.txt").stdout.startswith("??")


def test_commit_ledger_refuses_unrelated_staged_paths(tmp_path):
    repo, ledger = _init_repo(tmp_path)
    ledger.write_text(
        ledger.read_text(encoding="utf-8")
        + json.dumps({"fetched_at": "2026-06-08T01:00:00+00:00"})
        + "\n",
        encoding="utf-8",
    )
    other = repo / "unrelated.txt"
    other.write_text("already staged\n", encoding="utf-8")
    _git(repo, "add", "unrelated.txt")

    with pytest.raises(RuntimeError, match="unrelated paths are staged"):
        commit_ledger(repo_root=repo, ledger_path=ledger)


def test_commit_ledger_pathspec_commit_does_not_sweep_paths_staged_by_another_lane(tmp_path, monkeypatch):
    """A concurrent lane stages a file after our pre-check; the pathspec commit must
    neither include it nor disturb its staged state."""
    import codex_usage_tracker.git_autocommit as mod

    repo, ledger = _init_repo(tmp_path)
    ledger.write_text(
        ledger.read_text(encoding="utf-8")
        + json.dumps({"fetched_at": "2026-06-08T01:00:00+00:00"})
        + "\n",
        encoding="utf-8",
    )
    other = repo / "other_lane.txt"
    other.write_text("staged by another lane\n", encoding="utf-8")

    real_run_git = mod._run_git

    def racing_run_git(root, args, **kwargs):
        # Inject the race right at the commit call, after every staged-path check.
        if args and args[0] == "commit":
            _git(repo, "add", "other_lane.txt")
        return real_run_git(root, args, **kwargs)

    monkeypatch.setattr(mod, "_run_git", racing_run_git)

    result = commit_ledger(repo_root=repo, ledger_path=ledger, message="Update Codex usage ledger")

    assert result.committed is True
    committed_paths = _git(repo, "show", "--name-only", "--format=", "HEAD").stdout.splitlines()
    assert committed_paths == ["12_runtime/ledgers/codex_usage/codex_usage_ledger.jsonl"]
    # The other lane's file is still staged and untouched, not committed.
    staged = _git(repo, "diff", "--cached", "--name-only").stdout.split()
    assert staged == ["other_lane.txt"]


def test_commit_ledger_invokes_git_commit_with_pathspec(tmp_path, monkeypatch):
    import codex_usage_tracker.git_autocommit as mod

    repo, ledger = _init_repo(tmp_path)
    ledger.write_text(
        ledger.read_text(encoding="utf-8")
        + json.dumps({"fetched_at": "2026-06-08T01:00:00+00:00"})
        + "\n",
        encoding="utf-8",
    )
    calls = []
    real_run_git = mod._run_git

    def recording_run_git(root, args, **kwargs):
        calls.append(list(args))
        return real_run_git(root, args, **kwargs)

    monkeypatch.setattr(mod, "_run_git", recording_run_git)

    commit_ledger(repo_root=repo, ledger_path=ledger, message="msg")

    commit_calls = [c for c in calls if c and c[0] == "commit"]
    assert commit_calls == [["commit", "-m", "msg", "--", "12_runtime/ledgers/codex_usage/codex_usage_ledger.jsonl"]]
