"""Finance-owned receipt inputs remain usable by both tracker consumers."""
from __future__ import annotations

import runpy
import subprocess
from pathlib import Path

import pytest

from codex_usage_tracker import dashboard

JOB = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "receipts_monthly.py"))
FINANCE_MANIFEST = "22_life_and_logistics/finances/ai_subscriptions/ai_subscription_receipts.jsonl"


def test_monthly_collector_reads_finance_owned_receipts():
    assert JOB["MANIFEST_REL"] == FINANCE_MANIFEST


def test_dashboard_reads_finance_owned_receipts():
    assert dashboard.SUBSCRIPTION_RECEIPTS_MANIFEST == FINANCE_MANIFEST


@pytest.mark.parametrize("tracked", [True, False])
def test_monthly_commit_is_scoped_and_stages_only_new_files(tmp_path, monkeypatch, tracked):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, text=True, capture_output=True, check=True)

    git("init", "-q")
    git("config", "user.name", "Receipt test")
    git("config", "user.email", "receipt-test@example.invalid")
    other = tmp_path / "other.txt"
    other.write_text("initial\n")
    git("add", "--", "other.txt")
    manifest = tmp_path / FINANCE_MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"receipt_id":"initial"}\n')
    if tracked:
        git("add", "--", FINANCE_MANIFEST)
    git("commit", "-qm", "test baseline")
    other.write_text("unrelated staged work\n")
    git("add", "--", "other.txt")
    manifest.write_text('{"receipt_id":"initial"}\n{"receipt_id":"next"}\n')

    commit_manifest = JOB["_commit_manifest"]
    monkeypatch.setitem(commit_manifest.__globals__, "CANON", tmp_path)
    monkeypatch.setitem(commit_manifest.__globals__, "MANIFEST_REL", FINANCE_MANIFEST)
    original_run = subprocess.run
    commands = []

    def traced_run(command, **kwargs):
        commands.append(command)
        return original_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", traced_run)
    assert commit_manifest(1) is not None
    assert git("show", "--format=", "--name-only", "HEAD").stdout.strip() == FINANCE_MANIFEST
    assert git("diff", "--cached", "--name-only").stdout.strip() == "other.txt"
    prefix = JOB["GIT"]
    adds = [c for c in commands if c[:len(prefix)] == prefix and c[len(prefix)] == "add"]
    assert bool(adds) is not tracked
