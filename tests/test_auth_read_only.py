"""The tracker must never write auth.json.

Hermes owns the Codex OAuth token lifecycle. These tests pin the read-only
contract — see the auth module docstring and the 2026-08-15 incident.
"""

import base64
import json
import time

import codex_usage_tracker.auth as auth


def _jwt(exp: float) -> str:
    def seg(obj):
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{seg({'alg': 'RS256'})}.{seg({'exp': int(exp)})}.sig"


def _write_auth(tmp_path, monkeypatch, tokens):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps({"providers": {"openai-codex": {"tokens": tokens}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_USAGE_AUTH_FILE", str(auth_file))
    return auth_file


def test_module_exposes_no_write_or_refresh_helpers():
    for removed in ("_write_auth_file", "_persist_tokens", "_refresh_access_token"):
        assert not hasattr(auth, removed), f"{removed} must stay removed — Hermes owns refresh"


def test_returns_valid_token(tmp_path, monkeypatch):
    token = _jwt(time.time() + 86400)
    _write_auth(tmp_path, monkeypatch, {"access_token": token, "refresh_token": "rt"})

    creds = auth.get_credentials()

    assert creds is not None
    assert creds["api_key"] == token
    assert creds["base_url"] == "https://chatgpt.com"


def test_passes_through_account_id(tmp_path, monkeypatch):
    token = _jwt(time.time() + 86400)
    _write_auth(
        tmp_path, monkeypatch, {"access_token": token, "account_id": "acct-1"}
    )

    assert auth.get_credentials()["account_id"] == "acct-1"


def test_expired_token_returns_none_and_does_not_touch_the_file(tmp_path, monkeypatch):
    auth_file = _write_auth(
        tmp_path,
        monkeypatch,
        {"access_token": _jwt(time.time() - 60), "refresh_token": "rt"},
    )
    before = auth_file.read_bytes()

    assert auth.get_credentials() is None
    assert auth_file.read_bytes() == before, "auth.json must not be rewritten"


def test_token_inside_skew_window_is_treated_as_expired(tmp_path, monkeypatch):
    # Still technically valid, but dies mid-request — decline it.
    _write_auth(
        tmp_path,
        monkeypatch,
        {"access_token": _jwt(time.time() + 5), "refresh_token": "rt"},
    )

    assert auth.get_credentials() is None


def test_undecodable_token_is_treated_as_expired(tmp_path, monkeypatch):
    _write_auth(tmp_path, monkeypatch, {"access_token": "not-a-jwt", "refresh_token": "rt"})

    assert auth.get_credentials() is None


def test_missing_access_token_returns_none(tmp_path, monkeypatch):
    _write_auth(tmp_path, monkeypatch, {"refresh_token": "rt"})

    assert auth.get_credentials() is None


def test_expired_token_makes_no_network_call(tmp_path, monkeypatch):
    """The old code POSTed to auth.openai.com here. It must not any more."""
    import httpx

    def explode(*args, **kwargs):
        raise AssertionError("tracker must not make OAuth network calls")

    monkeypatch.setattr(httpx, "post", explode)
    _write_auth(
        tmp_path,
        monkeypatch,
        {"access_token": _jwt(time.time() - 60), "refresh_token": "rt"},
    )

    assert auth.get_credentials() is None


def _write_pool_only_auth(tmp_path, monkeypatch, entries):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps({"version": 1, "providers": {}, "credential_pool": {"openai-codex": entries}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_USAGE_AUTH_FILE", str(auth_file))
    return auth_file


def test_falls_back_to_the_newest_live_pool_entry_when_the_singleton_is_gone(tmp_path, monkeypatch):
    """2026-09-11: the live store lost providers.openai-codex; `hermes auth add
    openai-codex` restores a manual:device_code POOL entry only. A singleton-only
    reader would keep the Codex quota lane dead after the re-auth."""
    older = _jwt(time.time() + 3600)
    newer = _jwt(time.time() + 7200)
    auth_file = _write_pool_only_auth(tmp_path, monkeypatch, [
        {"id": "a", "source": "manual:device_code", "access_token": older,
         "account_id": "acct-old", "last_refresh": 100.0},
        {"id": "b", "source": "manual:device_code", "access_token": newer,
         "account_id": "acct-new", "last_refresh": 200.0},
        {"id": "c", "source": "manual:device_code", "access_token": _jwt(time.time() + 9000),
         "account_id": "acct-dead", "last_refresh": 300.0, "last_status": "dead"},
    ])
    before = auth_file.read_bytes()
    creds = auth.get_credentials()
    assert creds is not None
    assert creds["api_key"] == newer
    assert creds["account_id"] == "acct-new"
    assert auth_file.read_bytes() == before, "auth.json must not be rewritten"


def test_singleton_still_wins_over_the_pool(tmp_path, monkeypatch):
    singleton = _jwt(time.time() + 3600)
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({
        "version": 1,
        "providers": {"openai-codex": {"tokens": {"access_token": singleton, "account_id": "acct-s"}}},
        "credential_pool": {"openai-codex": [
            {"id": "p", "access_token": _jwt(time.time() + 7200), "account_id": "acct-p", "last_refresh": 5.0},
        ]},
    }), encoding="utf-8")
    monkeypatch.setenv("CODEX_USAGE_AUTH_FILE", str(auth_file))
    assert auth.get_credentials()["account_id"] == "acct-s"


def test_pool_without_usable_entries_returns_none(tmp_path, monkeypatch):
    _write_pool_only_auth(tmp_path, monkeypatch, [
        {"id": "d", "access_token": _jwt(time.time() + 3600), "last_status": "dead"},
        {"id": "e", "refresh_token": "only-a-refresh-token"},
    ])
    assert auth.get_credentials() is None
