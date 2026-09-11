"""Ruling ledger reader and the rulings-per-dollar join.

Keeper ruling 2026-09-10: ratification is the keeper's unit of produced
value, so rulings per ISO week set against the list-price inference estimate
is the honest judgment-per-dollar number. These tests pin the parts that can
silently lie:

- a malformed entry must be COUNTED as a parse error, never dropped (a
  dropped entry understates the numerator and nobody can see it happen);
- an Atrium with no ruling ledger must read as an empty series with honest
  coverage, not as an error and not as a fabricated zero;
- the join arithmetic must be the same rate table the dashboard prices with;
- ISO weeks are America/New_York, so a Sunday-evening ruling stays in its own
  week instead of being pushed forward by the UTC date.

Every test builds its own fixture tree. Nothing here reads the live Atrium.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from codex_usage_tracker.rulings import (
    RULINGS_TIMEZONE_NAME,
    RulingParseError,
    RulingsSourceUnavailable,
    build_rulings_per_dollar,
    build_weekly_rulings,
    fetch_service_rulings,
    load_rulings,
    parse_ruling_yaml,
    ruling_from_mapping,
    rulings_per_1k_usd,
    scan_canon_rulings,
    weekly_ruling_counts,
)


# A fixed "now" inside 2026-W37 (Thursday 2026-09-10, 18:00 UTC = 14:00 EDT).
NOW = datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc)

UID_A = "019938a5-1b2c-7def-8000-00000000000a"
UID_B = "019938a5-1b2c-7def-8000-00000000000b"
UID_C = "019938a5-1b2c-7def-8000-00000000000c"
UID_D = "019938a5-1b2c-7def-8000-00000000000d"


def _ledger(path, *blocks, frontmatter_value="v1"):
    """Write one ``ruling_ledger`` markdown file holding the given blocks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n\n".join(f"```ruling\n{block.strip()}\n```" for block in blocks)
    path.write_text(
        "---\n"
        "uid: 019938a5-1b2c-7def-8000-0000000000ff\n"
        "kind: note\n"
        "note_role: ruling_ledger\n"
        f"ruling_ledger: {frontmatter_value}\n"
        "---\n\n"
        "# Rulings\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return path


def _block(uid, ruled_at, ruling_class="adoption", **extra):
    lines = [f"uid: {uid}", f"ruled_at: {ruled_at}", f"class: {ruling_class}"]
    lines.extend(f"{key}: {value}" for key, value in extra.items())
    return "\n".join(lines)


def _usage_row(source_event_id, occurred_at, **updates):
    row = {
        "fact_type": "usage_event_v1",
        "schema_version": 1,
        "source_namespace": "test:rulings",
        "source_event_id": source_event_id,
        "harness": "claude_code",
        "purpose": "main",
        "record_kind": "api_attempt",
        "occurred_at": occurred_at,
        "recorded_at": occurred_at,
        "provider": "anthropic",
        "model_requested": "claude-opus-5",
        "model_reported": None,
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "usage_source": "provider_reported",
        "usage_completeness": "complete",
        "measurement_confidence": "exact",
        "cost_status": "included",
    }
    row.update(updates)
    return row


# ── Reader ─────────────────────────────────────────────────────────────────


def test_fixture_ledger_parses_entries_and_counts_the_malformed_one(tmp_path):
    """A bad entry is coverage, not a silent drop."""
    _ledger(
        tmp_path / "09_authors_notes" / "ruling_ledger.md",
        _block(UID_A, "2026-09-08T10:00:00-04:00", "adoption", grade="A", who="luis"),
        _block(UID_B, "2026-09-09T11:30:00-04:00", "correction", grade="B"),
        # Malformed: 'ratification' is not in the fixed class vocabulary.
        _block(UID_C, "2026-09-09T12:00:00-04:00", "ratification"),
        _block(UID_D, "2026-09-02T09:15:00-04:00", "gate", grade="A"),
    )

    scan = scan_canon_rulings(tmp_path)

    assert scan.ledgers_scanned == 1
    assert scan.entries_parsed == 3
    assert scan.parse_error_count == 1
    error = scan.parse_errors[0]
    assert "ratification" in error["reason"]
    assert error["path"].endswith("ruling_ledger.md")
    # The reader names the line the bad fence opened on, so the entry is
    # findable rather than merely counted.
    assert error["line"] > 0
    assert [ruling.uid for ruling in scan.rulings] == [UID_D, UID_A, UID_B]


def test_empty_canon_reads_as_an_empty_series_not_an_error(tmp_path):
    scan = scan_canon_rulings(tmp_path)
    assert scan.ledgers_scanned == 0
    assert scan.rulings == ()
    assert scan.parse_errors == ()

    payload = build_rulings_per_dollar(scan, [], weeks=3, now=NOW)
    assert [entry["rulings_total"] for entry in payload["series"]] == [0, 0, 0]
    # No traffic prices honestly at $0.00 with no ratio — "no denominator" is
    # not the same statement as "a ratio of zero".
    assert [entry["list_price_usd"] for entry in payload["series"]] == ["0.00"] * 3
    assert [entry["rulings_per_1k_usd"] for entry in payload["series"]] == [None] * 3
    assert payload["coverage"]["ledgers_scanned"] == 0
    assert payload["coverage"]["usage_ledger_available"] is True
    assert payload["timezone"] == RULINGS_TIMEZONE_NAME


def test_missing_atrium_root_is_empty_rather_than_raising(tmp_path):
    scan = scan_canon_rulings(tmp_path / "no-such-atrium")
    assert scan.ledgers_scanned == 0 and scan.rulings == ()


def test_scan_skips_archive_and_git_trees(tmp_path):
    _ledger(tmp_path / "09_authors_notes" / "live.md", _block(UID_A, "2026-09-08T10:00:00-04:00"))
    for excluded in ("99_archive", "08_archives", ".git", "50_drive"):
        _ledger(tmp_path / excluded / "old.md", _block(UID_B, "2026-09-08T10:00:00-04:00"))

    scan = scan_canon_rulings(tmp_path)

    assert scan.ledgers_scanned == 1
    assert [ruling.uid for ruling in scan.rulings] == [UID_A]


def test_files_without_the_frontmatter_key_are_not_ledgers(tmp_path):
    _ledger(
        tmp_path / "notes" / "not_a_ledger.md",
        _block(UID_A, "2026-09-08T10:00:00-04:00"),
        frontmatter_value="v0",
    )
    plain = tmp_path / "notes" / "plain.md"
    plain.write_text("```ruling\nuid: x\n```\n", encoding="utf-8")

    assert scan_canon_rulings(tmp_path).ledgers_scanned == 0


def test_duplicate_uid_across_ledgers_counts_once(tmp_path):
    _ledger(tmp_path / "a" / "one.md", _block(UID_A, "2026-09-08T10:00:00-04:00"))
    _ledger(tmp_path / "b" / "two.md", _block(UID_A, "2026-09-08T10:00:00-04:00"))

    scan = scan_canon_rulings(tmp_path)

    assert scan.ledgers_scanned == 2
    assert len(scan.rulings) == 1
    assert scan.duplicate_uids == 1
    assert scan.entries_parsed == 2


def test_block_scalars_lists_and_quotes_survive_the_subset_parser():
    parsed = parse_ruling_yaml(
        "uid: %s\n"
        "ruled_at: 2026-09-10T14:05:00-04:00\n"
        "class: adoption\n"
        "grade: A\n"
        "who: luis\n"
        'state: "ratified"\n'
        "verbatim: |\n"
        "  yeah we need to make ratification a first class thing.\n"
        "  maybe through the ADR\n"
        "rationale: >\n"
        "  tokens proxy cost,\n"
        "  cost proxies value\n"
        "applies_to:\n"
        "  - 20_digital_architecture\n"
        "  - 07_systems\n"
        "supersedes: []\n"
        "recorded_by: claude-code\n" % UID_A
    )

    assert parsed["verbatim"].splitlines()[1] == "maybe through the ADR"
    assert parsed["rationale"] == "tokens proxy cost, cost proxies value"
    assert parsed["applies_to"] == ["20_digital_architecture", "07_systems"]
    assert parsed["supersedes"] == []
    assert parsed["state"] == "ratified"

    ruling = ruling_from_mapping(parsed, source_path="fixture.md")
    assert ruling.ruling_class == "adoption"
    assert ruling.applies_to == ("20_digital_architecture", "07_systems")


@pytest.mark.parametrize(
    "mapping, fragment",
    [
        ({"ruled_at": "2026-09-10T14:05:00-04:00", "class": "gate"}, "uid is missing"),
        ({"uid": "nope", "ruled_at": "2026-09-10T14:05:00-04:00", "class": "gate"}, "not a UUID"),
        # UUIDv4 where the grammar says v7: a copy-paste, not a ruling.
        (
            {
                "uid": "9a2b1c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d",
                "ruled_at": "2026-09-10T14:05:00-04:00",
                "class": "gate",
            },
            "not a UUIDv7",
        ),
        ({"uid": UID_A, "class": "gate"}, "ruled_at is missing"),
        # Naive timestamp: the offset is what decides the local ISO week.
        ({"uid": UID_A, "ruled_at": "2026-09-10T14:05:00", "class": "gate"}, "no UTC offset"),
        ({"uid": UID_A, "ruled_at": "2026-09-10T14:05:00-04:00"}, "class is missing"),
    ],
)
def test_malformed_entries_raise_with_a_stated_reason(mapping, fragment):
    with pytest.raises(RulingParseError) as excinfo:
        ruling_from_mapping(mapping)
    assert fragment in str(excinfo.value)


def test_unterminated_fence_is_a_counted_error_for_the_whole_file(tmp_path):
    path = tmp_path / "broken.md"
    path.write_text(
        "---\nruling_ledger: v1\n---\n\n```ruling\nuid: %s\nclass: gate\n" % UID_A,
        encoding="utf-8",
    )

    scan = scan_canon_rulings(tmp_path)

    assert scan.ledgers_scanned == 1
    assert scan.rulings == ()
    assert "unterminated" in scan.parse_errors[0]["reason"]


# ── ISO weeks ──────────────────────────────────────────────────────────────


def test_iso_weeks_bucket_on_local_midnight_not_utc(tmp_path):
    """A Sunday-evening ruling stays in the week it was spoken in.

    2026-09-07T01:30:00Z is Sunday 2026-09-06 21:30 EDT — the last hours of
    ISO week 36. Bucketing on the UTC date would move it into week 37 and
    make Monday look busier than it was.
    """
    _ledger(
        tmp_path / "ledger.md",
        _block(UID_A, "2026-09-06T21:30:00-04:00"),
        _block(UID_B, "2026-09-07T00:30:00-04:00", "gate"),
    )
    scan = scan_canon_rulings(tmp_path)

    series = weekly_ruling_counts(scan.rulings, weeks=2, now=NOW)

    assert [entry["week"] for entry in series] == ["2026-W36", "2026-W37"]
    assert [entry["rulings_total"] for entry in series] == [1, 1]
    assert series[0]["rulings_by_class"]["adoption"] == 1
    assert series[1]["rulings_by_class"]["gate"] == 1
    # Both timestamps share a UTC calendar date (2026-09-07); only the local
    # offset separates them.
    assert UID_A[:8] == UID_B[:8]


def test_usage_rows_cross_the_same_local_week_boundary(tmp_path):
    """Cost and rulings must not use different week edges, or the ratio lies."""
    scan = scan_canon_rulings(tmp_path)
    rows = [
        # 21:30 EDT Sunday — week 36 locally, 2026-09-07 in UTC.
        _usage_row("late-sunday", "2026-09-07T01:30:00Z", input_tokens=1_000_000),
        # 00:30 EDT Monday — week 37.
        _usage_row("early-monday", "2026-09-07T04:30:00Z", input_tokens=2_000_000),
    ]

    payload = build_rulings_per_dollar(scan, rows, weeks=2, now=NOW)

    # claude-opus-5 input is $5/Mtok.
    assert payload["series"][0]["list_price_usd"] == "5.00"
    assert payload["series"][1]["list_price_usd"] == "10.00"


def test_partial_flag_marks_only_the_week_containing_now(tmp_path):
    payload = build_rulings_per_dollar(scan_canon_rulings(tmp_path), [], weeks=4, now=NOW)
    assert [entry["partial"] for entry in payload["series"]] == [False, False, False, True]
    assert payload["series"][-1]["week"] == "2026-W37"


# ── The join ───────────────────────────────────────────────────────────────


def test_join_divides_recorded_rulings_by_the_list_price_estimate(tmp_path):
    _ledger(
        tmp_path / "ledger.md",
        _block(UID_A, "2026-09-08T10:00:00-04:00", "adoption", grade="A"),
        _block(UID_B, "2026-09-09T10:00:00-04:00", "gate", grade="A"),
        _block(UID_C, "2026-09-09T18:00:00-04:00", "correction", grade="B"),
    )
    rows = [
        # $5.00 of input + $2.50 of output = $7.50 at claude-opus-5 list.
        _usage_row("week37", "2026-09-09T14:00:00Z", input_tokens=1_000_000, output_tokens=100_000),
    ]

    payload = build_rulings_per_dollar(scan_canon_rulings(tmp_path), rows, weeks=1, now=NOW)
    week = payload["series"][0]

    assert week["week"] == "2026-W37"
    assert week["rulings_total"] == 3
    assert week["rulings_by_class"] == {
        "correction": 1,
        "scope": 0,
        "routing": 0,
        "replan": 0,
        "adoption": 1,
        "gate": 1,
        "assent": 0,
        "convention": 0,
    }
    assert week["rulings_by_grade"] == {"A": 2, "B": 1}
    assert week["list_price_usd"] == "7.50"
    # 3 rulings / $7.50 → 400 rulings per $1,000 of estimated inference.
    assert week["rulings_per_1k_usd"] == 400.0
    assert week["output_tokens"] == 100_000
    assert week["total_tokens"] == 1_100_000
    assert week["unpriced_token_share"] == 0.0
    assert week["rate_basis"] == "standard"


def test_unpriced_traffic_surfaces_as_coverage_in_the_week(tmp_path):
    rows = [
        _usage_row("priced", "2026-09-09T14:00:00Z", input_tokens=1_000_000),
        _usage_row(
            "unpriced",
            "2026-09-09T15:00:00Z",
            provider="openai",
            model_requested="gpt-7-unreleased",
            input_tokens=1_000_000,
        ),
    ]

    week = build_rulings_per_dollar(scan_canon_rulings(tmp_path), rows, weeks=1, now=NOW)["series"][0]

    assert week["list_price_usd"] == "5.00"
    assert week["unpriced_token_share"] == 0.5
    assert week["unpriced_providers"] == ["openai"]


def test_no_usage_ledger_yields_null_prices_rather_than_a_zero_denominator(tmp_path):
    _ledger(tmp_path / "ledger.md", _block(UID_A, "2026-09-08T10:00:00-04:00"))

    payload = build_rulings_per_dollar(scan_canon_rulings(tmp_path), None, weeks=1, now=NOW)
    week = payload["series"][0]

    assert week["rulings_total"] == 1
    assert week["list_price_usd"] is None
    assert week["rulings_per_1k_usd"] is None
    assert payload["coverage"]["usage_ledger_available"] is False


def test_rulings_per_1k_is_none_when_there_is_no_denominator():
    assert rulings_per_1k_usd(4, "0.00") is None
    assert rulings_per_1k_usd(0, "10.00") == 0.0
    assert rulings_per_1k_usd(1, "2000.00") == 0.5


def test_weekly_counts_payload_drops_the_price_fields(tmp_path):
    _ledger(tmp_path / "ledger.md", _block(UID_A, "2026-09-08T10:00:00-04:00"))

    payload = build_weekly_rulings(scan_canon_rulings(tmp_path), weeks=1, now=NOW)
    week = payload["series"][0]

    assert week["rulings_total"] == 1
    assert "list_price_usd" not in week
    assert "rulings_per_1k_usd" not in week
    assert payload["coverage"]["entries_parsed"] == 1


def test_coverage_carries_both_standing_caveats(tmp_path):
    coverage = build_rulings_per_dollar(scan_canon_rulings(tmp_path), [], weeks=1, now=NOW)["coverage"]
    assert "estimate" in coverage["price_is_estimate"].lower()
    assert "recorded" in coverage["counts_recorded_rulings_only"].lower()


# ── Service source ─────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_service_source_validates_entries_exactly_like_the_canon_scan(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        return _FakeResponse(
            {
                "ledgers_scanned": 2,
                "rulings": [
                    {"uid": UID_A, "ruled_at": "2026-09-08T10:00:00-04:00", "class": "adoption"},
                    {"uid": "not-a-uuid", "ruled_at": "2026-09-08T11:00:00-04:00", "class": "gate"},
                ],
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    scan = fetch_service_rulings(base_url="http://127.0.0.1:8766", repo="/srv/pharos/atrium/canon")

    assert scan.source == "service"
    assert scan.ledgers_scanned == 2
    assert [ruling.uid for ruling in scan.rulings] == [UID_A]
    assert scan.parse_error_count == 1
    assert "/rulings?" in captured["url"]


def test_service_source_unavailable_raises_rather_than_reporting_zero(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(RulingsSourceUnavailable):
        load_rulings(atrium_root="/srv/pharos/atrium/canon", source="service")


def test_unknown_source_is_rejected():
    with pytest.raises(ValueError):
        load_rulings(atrium_root="/tmp", source="guess")


# ── Grammar as it actually landed in canon (standard v2.9, 2026-09-10) ──────


def test_documentation_example_inside_a_wider_fence_is_not_an_entry(tmp_path):
    """The proposal that defines this grammar shows a placeholder entry.

    ``ratification_ledger_first_class_ruling_entries_proposal_2026-09-10.md``
    carries its own first live ruling AND, twenty lines above it, an example
    block with ``uid: <uuidv7, minted per entry>`` wrapped in a four-backtick
    documentation fence. A reader that grepped for the tag would report that
    example as a malformed entry in every scan, forever.
    """
    path = tmp_path / "proposal.md"
    path.write_text(
        "---\nruling_ledger: v1\n---\n\n"
        "Entries are fenced blocks tagged ` ```ruling `:\n\n"
        "````\n"
        "```ruling\n"
        "uid: <uuidv7, minted per entry>\n"
        "class: correction | scope | routing\n"
        "```\n"
        "````\n\n"
        "And the real one:\n\n"
        "```ruling\n"
        f"uid: {UID_A}\n"
        "ruled_at: 2026-09-10T19:53:00-04:00\n"
        "class: convention\n"
        "```\n",
        encoding="utf-8",
    )

    scan = scan_canon_rulings(tmp_path)

    assert scan.parse_errors == ()
    assert [ruling.uid for ruling in scan.rulings] == [UID_A]


def test_real_entry_shape_parses_whole(tmp_path):
    """The landed grammar: nested ``recorded_by``, ``supersedes: null``, and
    the Valarin grade/state vocabularies, which this reader does not police."""
    _ledger(
        tmp_path / "RULINGS.md",
        "\n".join(
            [
                f"uid: {UID_A}",
                "ruled_at: 2026-09-10T19:53:00-04:00",
                "class: convention",
                "grade: ratified",
                "state: ruled",
                "who: keeper",
                'verbatim: "agreed. delegate it to subagents to implement."',
                'rationale: "Said on the six-point design as presented."',
                "applies_to:",
                "  - 01a08dc2-27e1-77e9-99f2-edd305619d2e",
                "supersedes: null",
                "recorded_by:",
                "  name: Claude Fable 5.1",
                "  harness: claude_code",
                "  model: claude-opus-5",
                "  provider: anthropic",
                "  at: 2026-09-10T20:05:00-04:00",
            ]
        ),
    )

    scan = scan_canon_rulings(tmp_path)

    assert scan.parse_errors == ()
    ruling = scan.rulings[0]
    assert ruling.state == "ruled"
    assert ruling.grade == "ratified"
    # `supersedes: null` is the absence of a superseded ruling, not the
    # literal string "null".
    assert ruling.supersedes == ()
    assert ruling.applies_to == ("01a08dc2-27e1-77e9-99f2-edd305619d2e",)
    assert ruling.recorded_by["model"] == "claude-opus-5"


def test_unknown_grade_and_state_are_counted_verbatim_not_rejected(tmp_path):
    """Keeper direction 2026-09-10: an unknown grade or state must still
    count. Those vocabularies evolve in the Valarin standard, and a reader
    that hard-failed on a new word would silently shrink the numerator."""
    _ledger(
        tmp_path / "RULINGS.md",
        _block(UID_A, "2026-09-09T10:00:00-04:00", "adoption", grade="provisional", state="mulled"),
    )

    scan = scan_canon_rulings(tmp_path)

    assert scan.parse_errors == ()
    assert scan.rulings[0].state == "mulled"
    week = build_weekly_rulings(scan, weeks=1, now=NOW)["series"][0]
    assert week["rulings_by_grade"] == {"provisional": 1}


def test_ungraded_entries_are_bucketed_as_ungraded(tmp_path):
    _ledger(tmp_path / "RULINGS.md", _block(UID_A, "2026-09-09T10:00:00-04:00"))
    week = build_weekly_rulings(scan_canon_rulings(tmp_path), weeks=1, now=NOW)["series"][0]
    assert week["rulings_by_grade"] == {"ungraded": 1}


def test_inline_recorded_by_mapping_is_accepted(tmp_path):
    _ledger(
        tmp_path / "RULINGS.md",
        "\n".join(
            [
                f"uid: {UID_A}",
                "ruled_at: 2026-09-09T10:00:00-04:00",
                "class: gate",
                "recorded_by: {name: Claude Fable 5.1, harness: claude_code}",
            ]
        ),
    )
    ruling = scan_canon_rulings(tmp_path).rulings[0]
    assert ruling.recorded_by == {"name": "Claude Fable 5.1", "harness": "claude_code"}
