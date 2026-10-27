"""Fetch failures must retain their real origin without logging credentials."""

import errno
import socket

import pytest

from codex_usage_tracker import auth, fetcher


@pytest.fixture
def credentials(monkeypatch):
    creds = {"base_url": "https://chatgpt.com", "api_key": "test-secret-not-for-logs", "account_id": "test-private-account"}
    monkeypatch.setattr(fetcher, "get_credentials", lambda: creds)
    return creds


def test_dns_ebusy_logs_full_real_transport_traceback(credentials, monkeypatch, caplog):
    # Exercise httpx/httpcore/socket, not a mocked httpx.get. No packets leave:
    # fail at name resolution, the same boundary seen in Sol's outage logs.
    def unavailable(*args, **kwargs):
        raise OSError(errno.EBUSY, "Device or resource busy")

    monkeypatch.setattr(socket, "getaddrinfo", unavailable)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    assert fetcher.fetch_usage() is None
    record = next(r for r in caplog.records if "Usage fetch failed" in r.message)
    assert record.exc_info is not None
    assert "Traceback (most recent call last)" in caplog.text
    assert "unavailable" in caplog.text
    assert "httpcore" in caplog.text
    assert credentials["api_key"] not in caplog.text
    assert credentials["account_id"] not in caplog.text


def test_credential_loading_exception_is_a_retryable_fetch_failure(monkeypatch, caplog):
    def fail():
        raise OSError(errno.EBUSY, "Device or resource busy")

    monkeypatch.setattr(fetcher, "get_credentials", fail)
    assert fetcher.fetch_usage() is None
    assert next(r for r in caplog.records if "Usage fetch failed" in r.message).exc_info is not None


def test_auth_read_failure_logs_traceback_without_writing(tmp_path, monkeypatch, caplog):
    auth_file = tmp_path / "auth.json"
    original = b'{"synthetic": true}'
    auth_file.write_bytes(original)
    monkeypatch.setenv("CODEX_USAGE_AUTH_FILE", str(auth_file))
    real_open = type(auth_file).open
    modes = []

    def busy(path, mode="r", *args, **kwargs):
        if path == auth_file:
            modes.append(mode)
            raise OSError(errno.EBUSY, "Device or resource busy")
        return real_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(type(auth_file), "open", busy)
        assert auth._read_auth_file() is None
    assert modes == ["r"]
    assert auth_file.read_bytes() == original
    assert next(r for r in caplog.records if "Failed to read auth file" in r.message).exc_info is not None


def test_fetch_rereads_credentials_on_each_attempt(monkeypatch):
    attempts = []
    borrowed = []

    def borrow():
        borrowed.append(1)
        return {"base_url": "https://chatgpt.com", "api_key": f"synthetic-{len(borrowed)}"}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"plan_type": "pro"}

    def get(url, *, headers, timeout):
        attempts.append(headers["Authorization"])
        if len(attempts) == 1:
            raise OSError(errno.EBUSY, "Device or resource busy")
        return Response()

    monkeypatch.setattr(fetcher, "get_credentials", borrow)
    monkeypatch.setattr(fetcher.httpx, "get", get)
    assert fetcher.fetch_usage() is None
    assert fetcher.fetch_usage() == {"plan_type": "pro"}
    assert attempts == ["Bearer synthetic-1", "Bearer synthetic-2"]


@pytest.mark.parametrize("field", ["api_key", "account_id"])
def test_malformed_credential_headers_are_rejected_without_network_or_value_logs(
    credentials, monkeypatch, caplog, field
):
    secret = "synthetic-private-value\n"
    credentials[field] = secret
    calls = []

    def send(*args, **kwargs):
        calls.append(1)
        # Mirrors h11's actual illegal-header error without making a connection.
        raise ValueError(f"Illegal header value {secret!r}")

    monkeypatch.setattr(fetcher.httpx, "get", send)
    assert fetcher.fetch_usage() is None
    assert calls == []
    assert secret.strip() not in caplog.text
