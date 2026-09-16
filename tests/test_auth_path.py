import importlib
import json
from pathlib import Path


def _reload_auth(monkeypatch, *, home, hermes_home=None, auth_file=None, sol_primary=False):
    monkeypatch.setenv("HOME", str(home))
    if hermes_home is None:
        monkeypatch.delenv("HERMES_HOME", raising=False)
    else:
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    if auth_file is None:
        monkeypatch.delenv("CODEX_USAGE_AUTH_FILE", raising=False)
    else:
        monkeypatch.setenv("CODEX_USAGE_AUTH_FILE", str(auth_file))
    # Sol-root presence is mocked; the tests never stat the live store.
    monkeypatch.setattr(
        "codex_usage_tracker.hermes_home._sol_primary_present",
        lambda: sol_primary,
    )

    import codex_usage_tracker.auth as auth

    return importlib.reload(auth)


def test_reads_auth_json_from_hermes_home_when_set(tmp_path, monkeypatch):
    home = tmp_path / "home"
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    payload = {"providers": {"openai-codex": {"tokens": {"refresh_token": "from-hermes-home"}}}}
    (hermes_home / "auth.json").write_text(json.dumps(payload), encoding="utf-8")

    auth = _reload_auth(monkeypatch, home=home, hermes_home=hermes_home)

    assert auth._read_auth_file() == payload


def test_hermes_home_still_wins_on_a_sol_host(tmp_path, monkeypatch):
    home = tmp_path / "home"
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    payload = {"providers": {"openai-codex": {"tokens": {"refresh_token": "from-hermes-home"}}}}
    (hermes_home / "auth.json").write_text(json.dumps(payload), encoding="utf-8")

    auth = _reload_auth(monkeypatch, home=home, hermes_home=hermes_home, sol_primary=True)

    assert auth._read_auth_file() == payload


def test_explicit_codex_usage_auth_file_overrides_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    hermes_home = tmp_path / "hermes-home"
    explicit = tmp_path / "explicit-auth.json"
    hermes_home.mkdir()
    (hermes_home / "auth.json").write_text(
        json.dumps({"providers": {"openai-codex": {"tokens": {"refresh_token": "wrong"}}}}),
        encoding="utf-8",
    )
    payload = {"providers": {"openai-codex": {"tokens": {"refresh_token": "explicit"}}}}
    explicit.write_text(json.dumps(payload), encoding="utf-8")

    auth = _reload_auth(monkeypatch, home=home, hermes_home=hermes_home, auth_file=explicit)

    assert auth._read_auth_file() == payload


def test_explicit_codex_usage_auth_file_overrides_sol_primary(tmp_path, monkeypatch):
    home = tmp_path / "home"
    explicit = tmp_path / "explicit-auth.json"
    payload = {"providers": {"openai-codex": {"tokens": {"refresh_token": "explicit"}}}}
    explicit.write_text(json.dumps(payload), encoding="utf-8")

    auth = _reload_auth(monkeypatch, home=home, auth_file=explicit, sol_primary=True)

    assert auth._resolve_auth_file() == explicit
    assert auth._read_auth_file() == payload


def test_without_env_a_sol_host_routes_to_the_canonical_primary_store(tmp_path, monkeypatch):
    # The stale ``~/.hermes`` copy on Sol must not be the no-env route. Only the
    # resolved path is asserted — the live store is never opened.
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)

    auth = _reload_auth(monkeypatch, home=home, sol_primary=True)

    assert auth._resolve_auth_file() == Path("/var/lib/hermes/primary/auth.json")


def test_defaults_to_legacy_home_hermes_auth_json_on_a_generic_host(tmp_path, monkeypatch):
    home = tmp_path / "home"
    legacy_dir = home / ".hermes"
    legacy_dir.mkdir(parents=True)
    payload = {"providers": {"openai-codex": {"tokens": {"refresh_token": "legacy"}}}}
    (legacy_dir / "auth.json").write_text(json.dumps(payload), encoding="utf-8")

    auth = _reload_auth(monkeypatch, home=home, sol_primary=False)

    assert auth._read_auth_file() == payload
