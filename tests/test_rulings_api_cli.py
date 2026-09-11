"""``/api/rulings``, ``/api/rulings-per-dollar``, and the CLI that prints them.

The surfaces are private and read-only: both endpoints scan an Atrium root for
``ruling_ledger: v1`` files and read the canonical usage ledger the dashboard
already prices. These tests pin the response contract, the argument bounds,
the "no usage ledger configured" behaviour, and that the CLI table and its
``--json`` form agree.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sys

import pytest

from codex_usage_tracker import cli
import codex_usage_tracker.dashboard as dashboard_module
from codex_usage_tracker.dashboard import create_app


UID_A = "019938a5-1b2c-7def-8000-00000000000a"
UID_B = "019938a5-1b2c-7def-8000-00000000000b"
UID_C = "019938a5-1b2c-7def-8000-00000000000c"


@pytest.fixture(autouse=True)
def _no_scan_cache():
    """The dashboard memoizes canon scans for a minute; tests want live reads."""
    dashboard_module._rulings_scan_cache.clear()
    yield
    dashboard_module._rulings_scan_cache.clear()


def _write_ruling_ledger(root, *blocks):
    path = root / "09_authors_notes" / "ruling_ledger.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n\n".join(f"```ruling\n{block.strip()}\n```" for block in blocks)
    path.write_text(
        "---\nuid: 019938a5-1b2c-7def-8000-0000000000ff\nkind: note\n"
        f"ruling_ledger: v1\n---\n\n# Rulings\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _block(uid, ruled_at, ruling_class="adoption", **extra):
    lines = [f"uid: {uid}", f"ruled_at: {ruled_at}", f"class: {ruling_class}"]
    lines.extend(f"{key}: {value}" for key, value in extra.items())
    return "\n".join(lines)


def _usage_fact(source_event_id, occurred_at, **updates):
    row = {
        "fact_type": "usage_event_v1",
        "schema_version": 1,
        "source_namespace": "test:rulings-api",
        "source_event_id": source_event_id,
        "harness": "claude_code",
        "surface": None,
        "purpose": "main",
        "record_kind": "api_attempt",
        "occurred_at": occurred_at,
        "recorded_at": occurred_at,
        "session_id": None,
        "logical_call_id": None,
        "attempt_no": None,
        "provider_request_id": None,
        "upstream_provider": None,
        "provider": "anthropic",
        "model_requested": "claude-opus-5",
        "model_reported": None,
        "api_mode": None,
        "billing_mode": None,
        "request_status": "ok",
        "error_class": None,
        "latency_ms": None,
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "usage_source": "provider_reported",
        "usage_completeness": "complete",
        "measurement_confidence": "exact",
        "missing_fields": [],
        "attribution_gaps": [],
        "estimated_cost_usd": None,
        "actual_cost_usd": None,
        "cost_status": "included",
    }
    row.update(updates)
    return row


def _write_usage_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _recent(weeks_ago=0, hour=14):
    """An instant inside the ISO week ``weeks_ago`` weeks back, in UTC.

    The endpoints anchor on wall-clock now, so fixtures are placed relative to
    it rather than on a fixed date that would age out of the window.
    """
    from codex_usage_tracker.rulings import recent_week_start_dates, week_bounds

    start_days = recent_week_start_dates(weeks_ago + 1)
    start, _end = week_bounds(start_days[0])
    # Wednesday of that week, safely inside it in either time zone.
    return start.replace(hour=hour) + __import__("datetime").timedelta(days=2)


# ── /api/rulings ───────────────────────────────────────────────────────────


def test_api_rulings_counts_weeks_without_any_usage_ledger(tmp_path):
    atrium = tmp_path / "atrium"
    _write_ruling_ledger(
        atrium,
        _block(UID_A, _recent(0).isoformat(), "adoption", grade="A"),
        _block(UID_B, _recent(0, hour=16).isoformat(), "gate", grade="A"),
        _block(UID_C, _recent(1).isoformat(), "correction", grade="B"),
    )

    client = create_app(atrium_root=str(atrium), ledger=str(tmp_path / "codex.jsonl")).test_client()
    response = client.get("/api/rulings?weeks=2")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["weeks"] == 2
    assert [entry["rulings_total"] for entry in payload["series"]] == [1, 2]
    assert payload["series"][-1]["rulings_by_class"]["gate"] == 1
    assert payload["series"][-1]["rulings_by_grade"] == {"A": 2}
    assert payload["coverage"]["ledgers_scanned"] == 1
    assert payload["coverage"]["parse_errors"] == 0
    # Counts-only payload carries no price fields at all.
    assert "list_price_usd" not in payload["series"][0]


def test_api_rulings_reports_parse_errors_as_coverage(tmp_path):
    atrium = tmp_path / "atrium"
    _write_ruling_ledger(
        atrium,
        _block(UID_A, _recent(0).isoformat()),
        _block(UID_B, _recent(0).isoformat(), "ratification"),
    )

    client = create_app(atrium_root=str(atrium), ledger=str(tmp_path / "codex.jsonl")).test_client()
    coverage = client.get("/api/rulings?weeks=1").get_json()["coverage"]

    assert coverage["entries_parsed"] == 1
    assert coverage["parse_errors"] == 1
    assert "ratification" in coverage["parse_error_details"][0]["reason"]


@pytest.mark.parametrize("query", ["weeks=0", "weeks=105", "weeks=many", "source=guess"])
def test_ruling_endpoints_reject_out_of_range_arguments(tmp_path, query):
    client = create_app(
        atrium_root=str(tmp_path / "atrium"), ledger=str(tmp_path / "codex.jsonl")
    ).test_client()
    assert client.get(f"/api/rulings?{query}").status_code == 400


# ── /api/rulings-per-dollar ────────────────────────────────────────────────


def test_api_rulings_per_dollar_joins_counts_to_the_list_price_estimate(tmp_path):
    atrium = tmp_path / "atrium"
    _write_ruling_ledger(
        atrium,
        _block(UID_A, _recent(0).isoformat(), "adoption"),
        _block(UID_B, _recent(0, hour=16).isoformat(), "gate"),
        _block(UID_C, _recent(0, hour=18).isoformat(), "routing"),
    )
    unified = _write_usage_jsonl(
        tmp_path / "unified.jsonl",
        [
            _usage_fact(
                "this-week",
                _recent(0, hour=15).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                input_tokens=1_000_000,
                output_tokens=100_000,
            )
        ],
    )

    client = create_app(
        atrium_root=str(atrium),
        ledger=str(tmp_path / "codex.jsonl"),
        unified_usage_ledger=str(unified),
    ).test_client()
    response = client.get("/api/rulings-per-dollar?weeks=1")

    assert response.status_code == 200
    week = response.get_json()["series"][0]
    assert week["rulings_total"] == 3
    # claude-opus-5: $5 input + $2.50 output = $7.50 → 400 rulings per $1k.
    assert week["list_price_usd"] == "7.50"
    assert week["rulings_per_1k_usd"] == 400.0
    assert week["output_tokens"] == 100_000
    assert week["unpriced_token_share"] == 0.0
    assert week["partial"] is True


def test_api_rulings_per_dollar_requires_a_configured_usage_ledger(tmp_path):
    client = create_app(
        atrium_root=str(tmp_path / "atrium"), ledger=str(tmp_path / "codex.jsonl")
    ).test_client()
    response = client.get("/api/rulings-per-dollar")
    assert response.status_code == 404
    assert "unified usage ledger" in response.get_json()["error"]


def test_api_rulings_per_dollar_is_empty_but_healthy_with_no_ledger_in_canon(tmp_path):
    """Until the canon lane lands a ledger, the series is honestly all zeros."""
    unified = _write_usage_jsonl(
        tmp_path / "unified.jsonl",
        [
            _usage_fact(
                "this-week",
                _recent(0, hour=15).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                input_tokens=1_000_000,
            )
        ],
    )
    client = create_app(
        atrium_root=str(tmp_path / "atrium"),
        ledger=str(tmp_path / "codex.jsonl"),
        unified_usage_ledger=str(unified),
    ).test_client()

    payload = client.get("/api/rulings-per-dollar?weeks=2").get_json()

    assert payload["coverage"]["ledgers_scanned"] == 0
    assert [entry["rulings_total"] for entry in payload["series"]] == [0, 0]
    assert payload["series"][-1]["list_price_usd"] == "5.00"
    # Zero rulings over real spend is a real ratio of zero, not a null.
    assert payload["series"][-1]["rulings_per_1k_usd"] == 0.0


def test_api_rulings_service_source_unavailable_is_a_503(tmp_path, monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = create_app(
        atrium_root=str(tmp_path / "atrium"), ledger=str(tmp_path / "codex.jsonl")
    ).test_client()

    assert client.get("/api/rulings?source=service").status_code == 503


def test_canon_scan_is_memoized_between_requests(tmp_path, monkeypatch):
    calls = []
    real_load = dashboard_module.load_rulings

    def counting_load(**kwargs):
        calls.append(kwargs)
        return real_load(**kwargs)

    monkeypatch.setattr(dashboard_module, "load_rulings", counting_load)
    client = create_app(
        atrium_root=str(tmp_path / "atrium"), ledger=str(tmp_path / "codex.jsonl")
    ).test_client()

    client.get("/api/rulings")
    client.get("/api/rulings?weeks=4")

    assert len(calls) == 1


# ── CLI ────────────────────────────────────────────────────────────────────


def _run_cli(monkeypatch, *args) -> int:
    monkeypatch.setattr(sys, "argv", ["codex-usage-tracker", "rulings-per-dollar", *args])
    return cli.main()


def test_cli_prints_a_table_with_the_join_and_its_caveats(tmp_path, monkeypatch, capsys):
    atrium = tmp_path / "atrium"
    _write_ruling_ledger(
        atrium,
        _block(UID_A, _recent(0).isoformat(), "adoption"),
        _block(UID_B, _recent(0, hour=16).isoformat(), "gate"),
    )
    unified = _write_usage_jsonl(
        tmp_path / "unified.jsonl",
        [
            _usage_fact(
                "this-week",
                _recent(0, hour=15).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                input_tokens=1_000_000,
            )
        ],
    )

    assert _run_cli(
        monkeypatch,
        "--weeks",
        "1",
        "--atrium-root",
        str(atrium),
        "--usage-ledger",
        str(unified),
    ) == 0

    out = capsys.readouterr().out
    assert "rulings" in out and "per $1k" in out
    # 2 rulings over $5.00 → 400 per $1,000.
    assert "$5.00" in out and "400.00" in out
    assert "adoption:1 gate:1" in out
    assert "ESTIMATE" in out
    assert "RECORDED rulings" in out


def test_cli_json_matches_the_endpoint_shape(tmp_path, monkeypatch, capsys):
    atrium = tmp_path / "atrium"
    _write_ruling_ledger(atrium, _block(UID_A, _recent(0).isoformat(), "adoption"))
    unified = _write_usage_jsonl(tmp_path / "unified.jsonl", [])

    assert _run_cli(
        monkeypatch,
        "--json",
        "--weeks",
        "2",
        "--atrium-root",
        str(atrium),
        "--usage-ledger",
        str(unified),
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["timezone"] == "America/New_York"
    assert len(payload["series"]) == 2
    assert payload["series"][-1]["rulings_total"] == 1
    assert payload["coverage"]["source"] == "canon"
    assert payload["coverage"]["ledgers_scanned"] == 1


def test_cli_reports_parse_errors_on_stderr_and_still_prints(tmp_path, monkeypatch, capsys):
    atrium = tmp_path / "atrium"
    _write_ruling_ledger(
        atrium,
        _block(UID_A, _recent(0).isoformat()),
        _block(UID_B, _recent(0).isoformat(), "ratification"),
    )
    unified = _write_usage_jsonl(tmp_path / "unified.jsonl", [])

    assert _run_cli(
        monkeypatch, "--atrium-root", str(atrium), "--usage-ledger", str(unified), "--weeks", "1"
    ) == 0

    captured = capsys.readouterr()
    assert "parse_errors=1" in captured.out
    assert "ratification" in captured.err


def test_cli_service_source_failure_exits_nonzero(tmp_path, monkeypatch, capsys):
    def fake_urlopen(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert _run_cli(monkeypatch, "--rulings-source", "service") == 1
    assert "rulings-per-dollar failed" in capsys.readouterr().err


def test_cli_without_a_usage_ledger_prints_counts_and_null_prices(tmp_path, monkeypatch, capsys):
    """A host with no usage ledger still gets the numerator, with no dollars.

    Printing $0.00 there would read as free judgment; the column is dashed and
    the missing ledger is named on stderr.
    """
    atrium = tmp_path / "atrium"
    _write_ruling_ledger(atrium, _block(UID_A, _recent(0).isoformat()))

    assert _run_cli(
        monkeypatch,
        "--weeks",
        "1",
        "--atrium-root",
        str(atrium),
        "--usage-ledger",
        str(tmp_path / "missing.sqlite3"),
    ) == 0

    captured = capsys.readouterr()
    assert "no usage ledger at" in captured.err
    # Both the price and the ratio columns are dashed, and the count survives.
    assert captured.out.count("—") >= 2
    assert "adoption:1" in captured.out
