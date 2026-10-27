"""Exercise the hourly loop with a virtual clock and real temporary ledger."""

import argparse
import json
import signal
from datetime import datetime, timedelta
from typing import Callable

import pytest

from codex_usage_tracker import cli


PAYLOAD = {"plan_type": "pro", "rate_limit": None}


class Clock:
    def __init__(self):
        self.start = datetime(2026, 10, 27, 9, 59)
        self.elapsed = 0.0
        self.waits = []
        self.stopped = False
        self.on_wait: Callable[[Clock], None] | None = None

    @property
    def now(self):
        return self.start + timedelta(seconds=self.elapsed)

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waits.append(seconds)
        self.elapsed += seconds
        if self.on_wait:
            self.on_wait(self)
        return self.stopped


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    clock = Clock()
    handlers = {}

    class ClockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now

    monkeypatch.setattr(cli, "datetime", ClockDateTime)
    monkeypatch.setattr(cli.time_module, "monotonic", lambda: clock.elapsed)
    monkeypatch.setattr(cli.threading, "Event", lambda: clock)
    monkeypatch.setattr(cli.signal, "signal", lambda sig, fn: handlers.update({sig: fn}))
    ledger = tmp_path / "usage.jsonl"
    args = argparse.Namespace(atrium_root=str(tmp_path), ledger=str(ledger))
    # Stop at 11:01; a healthy 11:00 fetch is allowed before the next sleep.
    end = datetime(2026, 10, 27, 11, 1)
    clock.on_wait = lambda c: c.set() if c.now >= end else None
    return clock, handlers, ledger, args


def test_daemon_recovers_within_hour_and_appends_once(daemon, monkeypatch):
    clock, _, ledger, args = daemon
    attempts = []

    def fetch():
        attempts.append(clock.now)
        return None if len(attempts) == 1 else PAYLOAD

    monkeypatch.setattr(cli, "fetch_usage", fetch)
    assert cli.cmd_daemon(args) == 0
    assert attempts == [datetime(2026, 10, 27, 10), datetime(2026, 10, 27, 10, 1), datetime(2026, 10, 27, 11)]
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(rows) == 2  # one recovered sample, then the next regular hour
    assert len({row["id"] for row in rows}) == 2
    assert clock.waits == [60, 60, 3540, 3600]


def test_daemon_backs_off_then_caps_at_ten_minutes_until_boundary(daemon, monkeypatch):
    clock, _, ledger, args = daemon
    attempts = []

    def fetch():
        attempts.append(clock.now)
        return PAYLOAD if clock.now.hour == 11 else None

    monkeypatch.setattr(cli, "fetch_usage", fetch)
    assert cli.cmd_daemon(args) == 0
    assert [t.strftime("%H:%M") for t in attempts] == [
        "10:00", "10:01", "10:03", "10:08", "10:18", "10:28", "10:38", "10:48", "10:58", "11:00"
    ]
    assert clock.waits == [60, 60, 120, 300, 600, 600, 600, 600, 600, 120, 3600]
    assert len(ledger.read_text().splitlines()) == 1


def test_daemon_resets_retry_backoff_for_new_hour(daemon, monkeypatch):
    clock, _, ledger, args = daemon
    clock.on_wait = lambda c: c.set() if c.now >= datetime(2026, 10, 27, 11, 2) else None
    attempts = []

    def fetch():
        attempts.append(clock.now)
        return None if clock.now.minute == 0 else PAYLOAD

    monkeypatch.setattr(cli, "fetch_usage", fetch)
    assert cli.cmd_daemon(args) == 0
    assert [t.strftime("%H:%M") for t in attempts] == ["10:00", "10:01", "11:00", "11:01"]
    assert len(ledger.read_text().splitlines()) == 2


def test_daemon_accounts_for_fetch_time_and_does_not_retry_across_hour(daemon, monkeypatch):
    clock, _, ledger, args = daemon
    attempts = []

    def fetch():
        attempts.append(clock.now)
        if len(attempts) == 1:
            clock.elapsed += 59 * 60
            return None
        return PAYLOAD

    monkeypatch.setattr(cli, "fetch_usage", fetch)
    assert cli.cmd_daemon(args) == 0
    assert [t.strftime("%H:%M") for t in attempts] == ["10:00", "11:00"]
    assert clock.waits == [60, 60, 3600]
    assert len(ledger.read_text().splitlines()) == 1


def test_daemon_shutdown_interrupts_retry_wait(daemon, monkeypatch):
    clock, handlers, ledger, args = daemon
    attempts = []
    monkeypatch.setattr(cli, "fetch_usage", lambda: attempts.append(clock.now))

    def stop_during_retry(c):
        if len(c.waits) == 2:
            handlers[signal.SIGTERM](signal.SIGTERM, None)

    clock.on_wait = stop_during_retry
    assert cli.cmd_daemon(args) == 0
    assert clock.waits == [60, 60]
    assert len(attempts) == 1
    assert not ledger.exists()


@pytest.mark.parametrize("phase", ["projection", "summary"])
def test_daemon_does_not_refetch_after_post_append_failure(daemon, monkeypatch, phase):
    clock, _, ledger, args = daemon
    clock.on_wait = lambda c: c.set() if c.now.hour >= 11 else None
    attempts = []
    monkeypatch.setattr(cli, "fetch_usage", lambda: attempts.append(clock.now) or PAYLOAD)

    def fail(*_):
        raise RuntimeError("post-append failure")

    monkeypatch.setattr(cli, "_write_public_projection_for_args" if phase == "projection" else "_print_summary", fail)
    assert cli.cmd_daemon(args) == 0
    assert len(attempts) == 1
    assert len(ledger.read_text().splitlines()) == 1


def test_daemon_shutdown_during_fetch_preserves_successful_observation(daemon, monkeypatch):
    clock, handlers, ledger, args = daemon

    def fetch():
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        return PAYLOAD

    monkeypatch.setattr(cli, "fetch_usage", fetch)
    assert cli.cmd_daemon(args) == 0
    assert len(ledger.read_text().splitlines()) == 1


def test_daemon_slow_failed_retry_crossing_hour_does_not_skip_new_hour(daemon, monkeypatch):
    clock, _, ledger, args = daemon
    clock.on_wait = lambda c: c.set() if c.now >= datetime(2026, 10, 27, 11, 2) else None
    attempts = []

    def fetch():
        attempts.append(clock.now)
        if clock.now.hour == 10:
            if clock.now.minute == 58:
                clock.elapsed += 150  # getaddrinfo can outlast httpx's phase timeout
            return None
        return PAYLOAD

    monkeypatch.setattr(cli, "fetch_usage", fetch)
    assert cli.cmd_daemon(args) == 0
    assert attempts[-1] == datetime(2026, 10, 27, 11, 0, 30)
    assert len(ledger.read_text().splitlines()) == 1
