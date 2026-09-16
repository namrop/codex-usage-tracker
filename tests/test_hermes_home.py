"""Hermes home resolution: ``HERMES_HOME`` → real Sol primary root → ``~/.hermes``.

Presence of the Sol root is mocked here — these tests never touch the live
credential store.
"""

from __future__ import annotations

from pathlib import Path

from codex_usage_tracker import hermes_home as hermes_home_module


def test_hermes_home_env_wins_even_when_the_sol_root_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes_home_module, "_sol_primary_present", lambda: True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))

    assert hermes_home_module.resolve_hermes_home() == tmp_path / "profile"


def test_sol_primary_is_the_no_env_route_when_that_root_exists(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    # A stale ``~/.hermes`` copy must not win on Sol.
    stale = tmp_path / "home" / ".hermes"
    stale.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(hermes_home_module, "_sol_primary_present", lambda: True)

    assert hermes_home_module.resolve_hermes_home() == Path("/var/lib/hermes/primary")


def test_generic_host_keeps_the_portable_home_hermes_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(hermes_home_module, "_sol_primary_present", lambda: False)

    assert hermes_home_module.resolve_hermes_home() == tmp_path / "home" / ".hermes"


def test_sol_root_is_an_absolute_path_not_derived_from_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert hermes_home_module.SOL_PRIMARY_HERMES_HOME == Path("/var/lib/hermes/primary")


def test_resolution_reads_the_environment_mapping_it_is_given(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes_home_module, "_sol_primary_present", lambda: True)

    resolved = hermes_home_module.resolve_hermes_home({"HERMES_HOME": str(tmp_path / "explicit")})

    assert resolved == tmp_path / "explicit"
