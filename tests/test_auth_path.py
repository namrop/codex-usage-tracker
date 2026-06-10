import importlib
import json


def _reload_auth(monkeypatch, *, home, hermes_home=None, auth_file=None):
    monkeypatch.setenv("HOME", str(home))
    if hermes_home is None:
        monkeypatch.delenv("HERMES_HOME", raising=False)
    else:
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    if auth_file is None:
        monkeypatch.delenv("CODEX_USAGE_AUTH_FILE", raising=False)
    else:
        monkeypatch.setenv("CODEX_USAGE_AUTH_FILE", str(auth_file))

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


def test_defaults_to_legacy_home_hermes_auth_json_without_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    legacy_dir = home / ".hermes"
    legacy_dir.mkdir(parents=True)
    payload = {"providers": {"openai-codex": {"tokens": {"refresh_token": "legacy"}}}}
    (legacy_dir / "auth.json").write_text(json.dumps(payload), encoding="utf-8")

    auth = _reload_auth(monkeypatch, home=home)

    assert auth._read_auth_file() == payload
