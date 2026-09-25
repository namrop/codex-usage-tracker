"""Serialized fast ticks with an atomic, success-only replay checkpoint.

This bounds ordinary Hermes/Claude reads; it does not replace full sweeps.
Backdated imports and restored old-mtime transcripts are recovered by the
independent daily full sweep. No canonical fact is edited or pruned.
"""
from __future__ import annotations

from contextlib import nullcontext
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable

from .claude_instances import normalize_claude_instances

OVERLAP_SECONDS = 600  # exceeds Claude's 300-second transcript settle period


def _binding(kwargs: dict[str, Any]) -> tuple[str, bool, str]:
    """Fingerprint configuration and file identities, never append-time mtimes."""
    instances = normalize_claude_instances(kwargs.get('claude_instances'))
    sources = [Path(kwargs['state_db']).expanduser()]
    sources += [Path(i.config_dir) / 'projects' for i in instances] if instances else [
        Path(kwargs.get('claude_root', '~/.claude/projects')).expanduser()]
    sources += [Path(p).expanduser() for p in kwargs['opencode_dbs']]
    if kwargs.get('openclaw_home'):
        sources.append(Path(kwargs['openclaw_home']).expanduser())
    def identity(path: Path) -> list[Any]:
        try:
            stat = path.stat()
            return [str(path.resolve()), stat.st_dev, stat.st_ino]
        except FileNotFoundError:
            return [str(path.resolve()), None, None]
    entries = [identity(p) for p in sources]
    complete = all(e[1] is not None for e in entries)
    source_payload = [entries, kwargs.get('source_prefix', 'sol'),
                      [(i.source_suffix, str(i.config_dir)) for i in instances]]
    source_digest = hashlib.sha256(json.dumps(source_payload, sort_keys=True).encode()).hexdigest()
    payload = [source_payload, identity(Path(kwargs['usage_ledger']).expanduser())]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(), complete, source_digest


def _load(path: Path, binding: str, started: float) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text())
        if not isinstance(state, dict) or state.get('version') != 1 or state.get('binding') != binding:
            return {}
        previous = state['last_success_started_at']
        if isinstance(previous, bool) or not isinstance(previous, (int, float)):
            return {}
        if not math.isfinite(previous) or not 0 <= previous <= started:
            return {}
        retries = state.get('retry_files', [])
        if not isinstance(retries, list) or not all(isinstance(p, str) for p in retries):
            return {}
        return state
    except (OSError, ValueError, KeyError):
        return {}


def _save(path: Path, state: dict[str, Any]) -> None:
    fd, name = tempfile.mkstemp(prefix=path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(state, handle, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def collect_incrementally(
    collect: Callable[..., dict[str, Any]], state_path: str | Path, *,
    full_sweep: bool = False, now: float | None = None, **kwargs: Any,
) -> dict[str, Any]:
    if kwargs.get('scope') != 'usage' or kwargs.get('usage_since') is not None:
        raise ValueError('incremental state requires usage scope and no explicit cutoff')
    state_path = Path(state_path).expanduser()
    protected = [kwargs['state_db'], kwargs['usage_ledger'], *kwargs['opencode_dbs']]
    protected += [kwargs[k] for k in ('quota_ledger','billing_ledger','codex_ledger','search_ledger') if kwargs.get(k)]
    if any(state_path.resolve() == Path(p).expanduser().resolve() for p in protected):
        raise ValueError('incremental checkpoint must be separate from sources and ledgers')
    dry_run = bool(kwargs.get("dry_run"))
    if not dry_run:
        state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Fast/deep timers share this lock across the entire read/append/checkpoint
    # cycle. Kernel releases it on process death; neither can overwrite the
    # other's cursor from an earlier snapshot. Canonical writer locking remains.
    with (nullcontext(None) if dry_run else open(str(state_path)+'.lock', 'a')) as lock:
        if lock is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        started = time.time() if now is None else now
        ledger_existed = Path(kwargs['usage_ledger']).expanduser().exists()
        binding, present, source_binding = _binding(kwargs)
        state = _load(state_path, binding, started)
        cutoff = None if full_sweep or not state else max(0, state['last_success_started_at']-OVERLAP_SECONDS)
        kwargs['usage_since'] = cutoff
        retry_files: list[str] = []
        kwargs['claude_retry_files'] = state.get('retry_files', [])
        kwargs['retry_files_out'] = retry_files
        result = collect(**kwargs)
        lanes = result.get('lanes') or {}
        after_binding, after_present, after_sources = _binding(kwargs)
        success = bool(lanes) and all(
            lane.get('succeeded') and not any(lane.get(k) for k in
                ('ceded', 'quarantined_count')) for lane in lanes.values())
        # Bad historical transcripts must be retried without pinning every
        # healthy source to a full scan forever. Their paths remain private in
        # this checkpoint, never in the collector's public diagnostics.
        skipped = sum(lane.get('skipped_files', 0) for lane in lanes.values())
        success = success and skipped == len(retry_files)
        advanced = bool(success and present and after_present and after_sources == source_binding
            and (not ledger_existed or after_binding == binding)
            and not kwargs.get('dry_run') and not result.get('conflicts')
            and not (result.get('counts') or {}).get('conflicts'))
        if advanced:
            _save(state_path, dict(version=1, binding=after_binding,
                last_success_started_at=started, retry_files=sorted(set(retry_files)),
                last_full_success_started_at=started if cutoff is None else state.get('last_full_success_started_at')))
        result['incremental'] = dict(mode='full' if cutoff is None else 'incremental',
            since=cutoff, overlap_seconds=OVERLAP_SECONDS, checkpoint_advanced=advanced, pending_transcripts=len(set(retry_files)))
        return result
