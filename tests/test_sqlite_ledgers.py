from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

import codex_usage_tracker.canonical_ledger as ledger
from codex_usage_tracker.canonical_ledger import (
    IdentityConflictError,
    MalformedLedgerError,
    ValidationError,
    append_sqlite_facts,
    audit_sqlite_ledger,
    canonical_json,
    export_sqlite_to_jsonl,
    migrate_jsonl_to_sqlite,
    normalize_fact,
    query_sqlite_facts,
    read_facts,
    read_sqlite_facts,
)


def usage(source_event_id: str = "u-1", **updates):
    row = {
        "fact_type": "usage_event_v1",
        "schema_version": 1,
        "source_namespace": "test:usage",
        "source_event_id": source_event_id,
        "harness": "test",
        "purpose": "main",
        "record_kind": "api_attempt",
        "occurred_at": "2026-07-12T12:00:00Z",
        "recorded_at": "2026-07-12T12:00:01Z",
        "usage_source": "provider_reported",
        "usage_completeness": "complete",
        "measurement_confidence": "exact",
        "cost_status": "included",
        "provider": "example",
        "model_requested": "model-a",
    }
    row.update(updates)
    return row


def quota(source_observation_id: str = "q-1", **updates):
    row = {
        "fact_type": "quota_observation_v1",
        "schema_version": 1,
        "source_namespace": "test:quota",
        "source_observation_id": source_observation_id,
        "harness": "test",
        "observed_at": "2026-07-12T12:00:00Z",
        "provider": "example",
        "quota_name": "week",
        "quota_scope": "account",
        "window_kind": "rolling",
        "unit": "percent",
        "measurement_confidence": "exact",
    }
    row.update(updates)
    return row


def billing(source_billing_fact_id: str = "b-1", **updates):
    row = {
        "fact_type": "billing_fact_v1",
        "schema_version": 1,
        "source_namespace": "test:billing",
        "source_billing_fact_id": source_billing_fact_id,
        "provider": "example",
        "occurred_at": "2026-07-12T02:00:00+02:00",
        "transaction_kind": "charge",
        "status": "posted",
        "amount": "+0012.3400",
        "currency": "USD",
    }
    row.update(updates)
    return row


def test_billing_fact_validation_materialization_and_canonicalization():
    row = normalize_fact(
        billing(
            billing_period_start="2026-07-01T02:00:00+02:00",
            usage_event_refs=[
                {"source_namespace": "z", "source_event_id": "2"},
                {"source_namespace": "a", "source_event_id": "1"},
            ],
        )
    )
    assert row["amount"] == "12.34"
    assert row["occurred_at"] == "2026-07-12T00:00:00Z"
    assert row["billing_period_start"] == "2026-07-01T00:00:00Z"
    assert row["billing_period_end"] is None
    assert row["account_ref"] is None and row["provider_receipt_id"] is None
    assert [(ref["source_namespace"], ref["source_event_id"]) for ref in row["usage_event_refs"]] == [
        ("a", "1"),
        ("z", "2"),
    ]


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"currency": "usd"}, "currency"),
        ({"currency": "US"}, "currency"),
        ({"transaction_kind": "fee"}, "transaction_kind"),
        ({"status": "paid"}, "status"),
        ({"amount": "-1"}, "sign"),
        ({"transaction_kind": "refund", "amount": "1"}, "sign"),
        ({"transaction_kind": "payment", "amount": "1"}, "sign"),
        ({"amount": "nan"}, "amount"),
        ({"source_billing_fact_id": ""}, "source_billing_fact_id"),
    ],
)
def test_billing_fact_rejects_contract_violations(updates, message):
    with pytest.raises(ValidationError, match=message):
        normalize_fact(billing(**updates))


def test_billing_adjustment_and_real_zero_are_valid_but_refs_are_unique():
    assert normalize_fact(billing(transaction_kind="adjustment", amount="-0.50"))["amount"] == "-0.5"
    assert normalize_fact(billing(amount="0.00"))["amount"] == "0"
    duplicate = {"source_namespace": "n", "source_event_id": "e"}
    with pytest.raises(ValidationError, match="unique"):
        normalize_fact(billing(usage_event_refs=[duplicate, dict(duplicate)]))
    with pytest.raises(ValidationError, match="source_event_id"):
        normalize_fact(billing(usage_event_refs=[{"source_namespace": "n", "source_event_id": ""}]))


@pytest.mark.parametrize("fact", [usage(), quota(), billing()])
def test_each_fact_type_has_a_bound_hardened_sqlite_ledger(tmp_path, fact):
    path = tmp_path / f"{fact['fact_type']}.sqlite3"
    result = append_sqlite_facts(path, [fact])
    assert (result.discovered, result.appended, result.replayed) == (1, 1, 0)
    assert read_sqlite_facts(path) == [normalize_fact(fact)]
    assert os.stat(path).st_mode & 0o777 == 0o600

    con = sqlite3.connect(path)
    assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert con.execute("PRAGMA synchronous").fetchone()[0] >= 2
    metadata = dict(con.execute("SELECT key, value FROM ledger_metadata"))
    assert metadata["fact_type"] == fact["fact_type"]
    stored = con.execute(
        "SELECT canonical_json, canonical_sha256, ingestion_sequence, ingested_at FROM facts"
    ).fetchone()
    assert stored[1] == hashlib.sha256(stored[0].encode("utf-8")).hexdigest()
    assert stored[2] == 1 and stored[3].endswith("Z")
    triggers = {row[0] for row in con.execute("SELECT name FROM sqlite_schema WHERE type='trigger'")}
    assert {"facts_no_update", "facts_no_delete"} <= triggers
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("UPDATE facts SET provider = 'changed'")
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("UPDATE ledger_metadata SET value = 'quota_observation_v1' WHERE key = 'fact_type'")
    with pytest.raises(sqlite3.DatabaseError):
        con.execute(
            """
            INSERT INTO facts(
                ingested_at, fact_type, source_namespace, source_identity, canonical_json,
                canonical_sha256, occurred_or_observed_at, provider, harness, purpose, model,
                quota_name, account_ref, transaction_kind, transaction_status, invoice_id, line_item_id
            )
            SELECT ingested_at,
                   CASE fact_type WHEN 'quota_observation_v1' THEN 'usage_event_v1' ELSE 'quota_observation_v1' END,
                   source_namespace, 'mixed', canonical_json,
                   canonical_sha256, occurred_or_observed_at, provider, harness, purpose, model,
                   quota_name, account_ref, transaction_kind, transaction_status, invoice_id, line_item_id
            FROM facts LIMIT 1
            """
        )
    con.close()



def test_sqlite_replay_conflict_and_batch_rollback(tmp_path):
    path = tmp_path / "usage.sqlite3"
    first = append_sqlite_facts(path, [usage()])
    replay = append_sqlite_facts(path, [usage(occurred_at="2026-07-12T14:00:00+02:00")])
    assert (first.appended, replay.replayed) == (1, 1)

    with pytest.raises(IdentityConflictError):
        append_sqlite_facts(path, [usage("u-2"), usage(input_tokens=99)])
    assert [row["source_event_id"] for row in read_sqlite_facts(path)] == ["u-1"]

    with pytest.raises(ValidationError, match="fact_type"):
        append_sqlite_facts(path, [quota()])
    assert len(read_sqlite_facts(path)) == 1



def test_dry_run_absent_database_has_no_filesystem_side_effect(tmp_path):
    parent = tmp_path / "absent" / "private"
    path = parent / "usage.sqlite3"
    result = append_sqlite_facts(path, [usage()], dry_run=True)
    assert (result.appended, result.replayed) == (1, 0)
    assert not parent.exists()
    assert not path.exists()
    assert not (path.parent / f"{path.name}-wal").exists()
    assert not (path.parent / f"{path.name}-shm").exists()


def test_migration_dry_run_does_not_lock_or_chmod_source(tmp_path):
    source = tmp_path / "usage.jsonl"
    source.write_text(canonical_json(usage()) + "\n", encoding="utf-8")
    os.chmod(source, 0o644)
    before = source.stat()
    destination = tmp_path / "usage.sqlite3"
    result = migrate_jsonl_to_sqlite(source, destination, fact_type="usage_event_v1", dry_run=True)
    after = source.stat()
    assert result.appended == 1
    assert (after.st_mode, after.st_mtime_ns, after.st_size) == (before.st_mode, before.st_mtime_ns, before.st_size)
    assert not Path(f"{source}.lock").exists()
    assert not destination.exists()


def test_schema_mismatch_and_content_corruption_fail_closed(tmp_path):
    wrong = tmp_path / "wrong.sqlite3"
    sqlite3.connect(wrong).close()
    with pytest.raises(MalformedLedgerError):
        append_sqlite_facts(wrong, [usage()])

    path = tmp_path / "usage.sqlite3"
    append_sqlite_facts(path, [usage()])
    con = sqlite3.connect(path)
    con.execute("DROP TRIGGER facts_no_update")
    con.execute("UPDATE facts SET canonical_sha256 = ?", ("0" * 64,))
    con.commit()
    con.close()
    with pytest.raises(MalformedLedgerError):
        read_sqlite_facts(path)



def test_jsonl_migration_export_parity_and_replay(tmp_path):
    source = tmp_path / "billing.jsonl"
    source.write_text(
        "\n".join(canonical_json(row) for row in [billing("b-1"), billing("b-2", amount="2")]) + "\n",
        encoding="utf-8",
    )
    os.chmod(source, 0o600)
    database = tmp_path / "billing.sqlite3"
    first = migrate_jsonl_to_sqlite(source, database, fact_type="billing_fact_v1")
    replay = migrate_jsonl_to_sqlite(source, database, fact_type="billing_fact_v1")
    assert (first.appended, replay.replayed) == (2, 2)

    exported = tmp_path / "archive" / "billing.jsonl"
    count = export_sqlite_to_jsonl(database, exported, fact_type="billing_fact_v1")
    assert count == 2
    assert os.stat(exported).st_mode & 0o777 == 0o600
    assert read_facts(exported) == read_sqlite_facts(database)
    assert [hashlib.sha256(canonical_json(row).encode()).hexdigest() for row in read_facts(exported)] == [
        hashlib.sha256(canonical_json(row).encode()).hexdigest() for row in read_sqlite_facts(database)
    ]



def test_failed_migration_does_not_accept_a_partial_import(tmp_path):
    source = tmp_path / "bad.jsonl"
    source.write_text(canonical_json(usage()) + "\nnot-json\n", encoding="utf-8")
    database = tmp_path / "usage.sqlite3"
    with pytest.raises(MalformedLedgerError):
        migrate_jsonl_to_sqlite(source, database, fact_type="usage_event_v1")
    assert not database.exists()

    mixed = tmp_path / "mixed.jsonl"
    mixed.write_text(canonical_json(usage()) + "\n" + canonical_json(quota()) + "\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="fact_type"):
        migrate_jsonl_to_sqlite(mixed, database, fact_type="usage_event_v1")
    assert not database.exists()


def test_rfc8785_vectors_utf16_strings_and_ieee754_numbers():
    row = usage(
        x_object={
            "\u20ac": "Euro Sign", "\r": "Carriage Return",
            "\ufb33": "Hebrew Letter Dalet With Dagesh", "1": "One",
            "\U0001f600": "Emoji: Grinning Face", "\u0080": "Control",
            "\u00f6": "Latin Small Letter O With Diaeresis",
        },
        x_numbers=[-0.0, 333333333.33333329, 1e30, 4.50, 2e-3, 1e-27],
        x_string='\b\t\n\f\r"\\/\u000f',
    )
    payload = canonical_json(row)
    assert '"x_numbers":[0,333333333.3333333,1e+30,4.5,0.002,1e-27]' in payload
    assert '"x_string":"\\b\\t\\n\\f\\r\\"\\\\/\\u000f"' in payload
    ordered_keys = ['\r', '1', '\u0080', '\u00f6', '\u20ac', '\U0001f600', '\ufb33']
    object_start = payload.index('"x_object":{')
    object_end = payload.index('},"x_string"', object_start)
    object_payload = payload[object_start:object_end]
    positions = [object_payload.index(json.dumps(key, ensure_ascii=False)[1:-1]) for key in ordered_keys]
    assert positions == sorted(positions)


@pytest.mark.parametrize("value", ["\ud800", "ok\udfff"])
def test_jcs_rejects_unpaired_surrogates(value):
    with pytest.raises(ValidationError, match="surrogate"):
        canonical_json(usage(x_bad=value))
    with pytest.raises(ValidationError, match="surrogate"):
        canonical_json(usage(x_bad={value: "key"}))


def test_jcs_rejects_unsafe_integers_and_nonfinite_numbers():
    for value in (2**53, -(2**53), float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            canonical_json(usage(x_number=value))


@pytest.mark.parametrize("value", [9007199254740992.0, -9007199254740992.0, 1000000.0])
def test_jcs_integral_binary64_values_do_not_keep_decimal_suffix(value):
    payload = canonical_json(usage(x_number=value))
    assert f'"x_number":{int(value)}' in payload
    assert f'"x_number":{int(value)}.0' not in payload


@pytest.mark.parametrize("timestamp", [
    "2026-07-12 12:00:00Z", "2026-07-12X12:00:00Z", "2026-W28-7T12:00:00Z",
    "2026-07-12T12:00:00", "2026-07-12T12:00Z",
    "2026-07-12T12:00:00+2400", "2026-07-12T12:00:00+24:00",
])
def test_timestamp_parser_rejects_non_rfc3339_spellings(timestamp):
    with pytest.raises(ValidationError, match="RFC 3339"):
        normalize_fact(usage(occurred_at=timestamp))


def test_timestamp_parser_normalizes_offset_and_arbitrary_fraction_precision():
    row = normalize_fact(usage(occurred_at="2026-07-12T00:30:00.123400000-01:30"))
    assert row["occurred_at"] == "2026-07-12T02:00:00.1234Z"


@pytest.mark.parametrize("value", ["1_000", "１２", "1e3", "NaN", "Infinity", " 1", "1 ", ".5", "1."])
def test_contract_decimals_reject_non_ascii_or_non_plain_spellings(value):
    with pytest.raises(ValidationError, match="amount"):
        normalize_fact(billing(amount=value))


def test_currency_membership_optional_empty_strings_and_complete_sign_matrix():
    assert normalize_fact(billing(currency="USD"))["currency"] == "USD"
    assert normalize_fact(billing(currency="EUR"))["currency"] == "EUR"
    with pytest.raises(ValidationError, match="currency"):
        normalize_fact(billing(currency="ABC"))
    row = normalize_fact(
        billing(account_ref="", invoice_id="", line_item_id="", description_code="", provider_receipt_id="")
    )
    assert all(row[field] == "" for field in (
        "account_ref", "invoice_id", "line_item_id", "description_code", "provider_receipt_id"
    ))
    for kind in ("charge", "tax"):
        normalize_fact(billing(transaction_kind=kind, amount="1"))
        with pytest.raises(ValidationError, match="sign"):
            normalize_fact(billing(transaction_kind=kind, amount="-1"))
    for kind in ("credit", "refund", "payment"):
        normalize_fact(billing(transaction_kind=kind, amount="-1"))
        with pytest.raises(ValidationError, match="sign"):
            normalize_fact(billing(transaction_kind=kind, amount="1"))
    for kind in ("charge", "tax", "credit", "refund", "payment", "adjustment"):
        assert normalize_fact(billing(transaction_kind=kind, amount="-0.00"))["amount"] == "0"


@pytest.mark.parametrize("updates", [
    {"request_status": "gibberish"},
    {"missing_fields": ["provider"]},
    {"attribution_gaps": ["input_tokens"]},
    {"record_kind": "historical_aggregate", "latency_ms": 1, "request_status": None},
    {"record_kind": "correction", "latency_ms": 1, "request_status": None},
])
def test_usage_contract_rejects_invalid_status_gap_and_latency_semantics(updates):
    with pytest.raises(ValidationError):
        normalize_fact(usage(**updates))


def test_append_is_structurally_incremental_and_does_not_audit_history(tmp_path, monkeypatch):
    path = tmp_path / "usage.sqlite3"
    append_sqlite_facts(path, [usage(str(index)) for index in range(40)])
    calls = 0
    original = ledger.normalize_fact

    def counted(fact):
        nonlocal calls
        calls += 1
        return original(fact)

    monkeypatch.setattr(ledger, "normalize_fact", counted)
    monkeypatch.setattr(ledger, "audit_sqlite_ledger", lambda *args, **kwargs: pytest.fail("append audited ledger"))
    result = append_sqlite_facts(path, [usage("new")])
    assert result.appended == 1
    assert calls <= 2


def test_existing_schema_is_validated_inside_write_transaction(tmp_path, monkeypatch):
    path = tmp_path / "usage.sqlite3"
    append_sqlite_facts(path, [usage()])
    original = ledger._validate_sqlite_schema
    observations = []

    def checked(connection, *args, **kwargs):
        observations.append(connection.in_transaction)
        return original(connection, *args, **kwargs)

    monkeypatch.setattr(ledger, "_validate_sqlite_schema", checked)
    append_sqlite_facts(path, [usage("new")])
    assert observations and all(observations)


@pytest.mark.parametrize("tamper", ["trigger", "index", "constraint", "metadata", "wal"])
def test_exact_schema_validation_fails_closed_on_drift(tmp_path, tamper):
    path = tmp_path / f"{tamper}.sqlite3"
    append_sqlite_facts(path, [usage()])
    con = sqlite3.connect(path)
    if tamper == "trigger":
        con.executescript("DROP TRIGGER facts_no_update; CREATE TRIGGER facts_no_update BEFORE UPDATE ON facts BEGIN SELECT 1; END;")
    elif tamper == "index":
        con.executescript("DROP INDEX facts_provider_idx; CREATE INDEX facts_provider_idx ON facts(harness);")
    elif tamper == "constraint":
        con.execute("PRAGMA writable_schema=ON")
        sql = con.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name='facts'").fetchone()[0]
        con.execute("UPDATE sqlite_schema SET sql=? WHERE type='table' AND name='facts'", (
            sql.replace("UNIQUE(source_namespace, source_identity)", "UNIQUE(source_identity, source_namespace)"),
        ))
        con.execute("PRAGMA writable_schema=OFF")
    elif tamper == "metadata":
        con.executescript("DROP TRIGGER metadata_no_update; UPDATE ledger_metadata SET value='2' WHERE key='schema_version';")
    else:
        con.execute("PRAGMA journal_mode=DELETE")
    con.commit()
    con.close()
    with pytest.raises(MalformedLedgerError):
        append_sqlite_facts(path, [usage("new")])


def test_index_validation_includes_collation_and_sort_direction(tmp_path):
    path = tmp_path / "index-shape.sqlite3"
    append_sqlite_facts(path, [usage()])
    con = sqlite3.connect(path)
    con.executescript(
        "DROP INDEX facts_provider_idx; "
        "CREATE INDEX facts_provider_idx ON facts(provider COLLATE NOCASE DESC);"
    )
    con.close()
    with pytest.raises(MalformedLedgerError, match="index"):
        append_sqlite_facts(path, [usage("new")])


def test_explicit_audit_detects_historical_payload_corruption(tmp_path):
    path = tmp_path / "usage.sqlite3"
    append_sqlite_facts(path, [usage()])
    con = sqlite3.connect(path)
    stored_hash = con.execute("SELECT canonical_sha256 FROM facts").fetchone()[0]
    con.close()
    database_bytes = path.read_bytes()
    assert stored_hash.encode("ascii") in database_bytes
    path.write_bytes(database_bytes.replace(stored_hash.encode("ascii"), b"0" * 64, 1))
    with pytest.raises(MalformedLedgerError, match="hash"):
        audit_sqlite_ledger(path)


def test_existing_database_dry_run_has_no_file_or_mode_side_effects(tmp_path):
    path = tmp_path / "usage.sqlite3"
    append_sqlite_facts(path, [usage()])
    os.chmod(path, 0o640)
    before = path.stat()
    assert not Path(f"{path}-wal").exists() and not Path(f"{path}-shm").exists()
    result = append_sqlite_facts(path, [usage("u-2")], dry_run=True)
    after = path.stat()
    assert result.appended == 1
    assert (after.st_mode, after.st_mtime_ns, after.st_size) == (before.st_mode, before.st_mtime_ns, before.st_size)
    assert not Path(f"{path}-wal").exists() and not Path(f"{path}-shm").exists()


def test_dry_run_fails_closed_when_current_state_may_live_in_wal(tmp_path):
    path = tmp_path / "usage.sqlite3"
    append_sqlite_facts(path, [usage()])
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("CREATE TABLE wal_probe(value TEXT)")
    writer.execute("INSERT INTO wal_probe VALUES ('committed-in-wal')")
    writer.commit()
    assert Path(f"{path}-wal").exists()
    with pytest.raises(MalformedLedgerError, match="WAL"):
        append_sqlite_facts(path, [usage("u-2")], dry_run=True)
    writer.close()


def test_jsonl_lock_symlink_is_rejected(tmp_path):
    database = tmp_path / "usage.sqlite3"
    append_sqlite_facts(database, [usage()])
    destination = tmp_path / "out.jsonl"
    victim = tmp_path / "victim"
    victim.write_text("unchanged", encoding="utf-8")
    os.chmod(victim, 0o644)
    Path(f"{destination}.lock").symlink_to(victim)
    with pytest.raises((MalformedLedgerError, OSError)):
        export_sqlite_to_jsonl(database, destination)
    assert victim.read_text(encoding="utf-8") == "unchanged"
    assert os.stat(victim).st_mode & 0o777 == 0o644


def test_migration_and_export_reject_missing_aliases_and_duplicate_sources(tmp_path):
    missing_jsonl = tmp_path / "missing.jsonl"
    database = tmp_path / "usage.sqlite3"
    with pytest.raises((FileNotFoundError, MalformedLedgerError)):
        migrate_jsonl_to_sqlite(missing_jsonl, database, fact_type="usage_event_v1")
    assert not database.exists()
    with pytest.raises((FileNotFoundError, MalformedLedgerError)):
        export_sqlite_to_jsonl(database, tmp_path / "out.jsonl")

    source = tmp_path / "usage.jsonl"
    source.write_text(canonical_json(usage()) + "\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="same file"):
        migrate_jsonl_to_sqlite(source, source, fact_type="usage_event_v1")
    append_sqlite_facts(database, [usage()])
    with pytest.raises(ValidationError, match="same file"):
        export_sqlite_to_jsonl(database, database)

    duplicate = tmp_path / "duplicate.jsonl"
    duplicate.write_text(canonical_json(usage()) + "\n" + canonical_json(usage()) + "\n", encoding="utf-8")
    fresh = tmp_path / "fresh.sqlite3"
    with pytest.raises(IdentityConflictError, match="duplicate"):
        migrate_jsonl_to_sqlite(duplicate, fresh, fact_type="usage_event_v1")
    assert not fresh.exists() and not Path(f"{fresh}-wal").exists() and not Path(f"{fresh}-shm").exists()


def test_failed_initial_migration_cleans_all_sqlite_artifacts(tmp_path, monkeypatch):
    source = tmp_path / "usage.jsonl"
    source.write_text(canonical_json(usage()) + "\n", encoding="utf-8")
    database = tmp_path / "usage.sqlite3"
    monkeypatch.setattr(ledger, "_typed_values", lambda row: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        migrate_jsonl_to_sqlite(source, database, fact_type="usage_event_v1")
    assert not database.exists() and not Path(f"{database}-wal").exists() and not Path(f"{database}-shm").exists()


def test_failed_initial_append_removes_created_database(tmp_path):
    database = tmp_path / "usage.sqlite3"
    with pytest.raises(IdentityConflictError):
        append_sqlite_facts(database, [usage(), usage(input_tokens=99)])
    assert not database.exists()
    assert not Path(f"{database}-wal").exists() and not Path(f"{database}-shm").exists()


def test_absent_database_rejects_and_preserves_preexisting_sidecars(tmp_path):
    database = tmp_path / "usage.sqlite3"
    wal = Path(f"{database}-wal")
    shm = Path(f"{database}-shm")
    wal.write_bytes(b"preexisting-wal")
    shm.write_bytes(b"preexisting-shm")
    with pytest.raises(MalformedLedgerError, match="sidecar"):
        append_sqlite_facts(database, [usage()])
    assert not database.exists()
    assert wal.read_bytes() == b"preexisting-wal"
    assert shm.read_bytes() == b"preexisting-shm"


def test_migration_does_not_delete_destination_it_did_not_create(tmp_path, monkeypatch):
    source = tmp_path / "usage.jsonl"
    source.write_text(canonical_json(usage()) + "\n", encoding="utf-8")
    database = tmp_path / "usage.sqlite3"

    def concurrent_creator(*args, **kwargs):
        database.write_bytes(b"created-by-another-writer")
        raise IdentityConflictError("simulated concurrent conflict")

    monkeypatch.setattr(ledger, "append_sqlite_facts", concurrent_creator)
    with pytest.raises(IdentityConflictError):
        migrate_jsonl_to_sqlite(source, database, fact_type="usage_event_v1")
    assert database.read_bytes() == b"created-by-another-writer"


def test_sqlite_database_and_sidecar_symlinks_are_rejected(tmp_path):
    victim = tmp_path / "victim.sqlite3"
    append_sqlite_facts(victim, [usage()])
    os.chmod(victim, 0o644)
    alias = tmp_path / "alias.sqlite3"
    alias.symlink_to(victim)
    with pytest.raises(MalformedLedgerError):
        append_sqlite_facts(alias, [usage("new")])
    assert len(read_sqlite_facts(victim)) == 1
    assert os.stat(victim).st_mode & 0o777 == 0o644

    sidecar_victim = tmp_path / "sidecar-victim"
    sidecar_victim.write_text("unchanged", encoding="utf-8")
    Path(f"{victim}-wal").unlink(missing_ok=True)
    Path(f"{victim}-shm").unlink(missing_ok=True)
    Path(f"{victim}-wal").symlink_to(sidecar_victim)
    with pytest.raises(MalformedLedgerError):
        append_sqlite_facts(victim, [usage("new")])
    assert sidecar_victim.read_text(encoding="utf-8") == "unchanged"


def test_concurrent_first_append_serializes_initialization(tmp_path):
    database = tmp_path / "usage.sqlite3"

    def append_one(index):
        return append_sqlite_facts(database, [usage(str(index))])

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(append_one, range(16)))
    assert sum(result.appended for result in results) == 16
    assert len(read_sqlite_facts(database)) == 16


def test_bounded_sqlite_query_filters_orders_and_validates_only_returned_rows(tmp_path, monkeypatch):
    database = tmp_path / "usage.sqlite3"
    append_sqlite_facts(database, [
        usage("old", provider="other", occurred_at="2026-07-10T12:00:00Z"),
        usage("first", provider="example", occurred_at="2026-07-12T12:00:00Z"),
        usage("second", provider="example", occurred_at="2026-07-13T12:00:00Z"),
    ])
    calls = 0
    original = ledger.normalize_fact

    def counted(fact):
        nonlocal calls
        calls += 1
        return original(fact)

    monkeypatch.setattr(ledger, "normalize_fact", counted)
    rows = query_sqlite_facts(
        database,
        fact_type="usage_event_v1",
        filters={"provider": "example"},
        occurred_or_observed_at_gte="2026-07-12T00:00:00Z",
        order="desc",
        limit=1,
        offset=1,
    )
    assert [row["source_event_id"] for row in rows] == ["first"]
    # Canonical comparison normalizes the one returned row a second time; no
    # non-returned historical payload is touched.
    assert calls == 2


def test_bounded_sqlite_query_integrity_mode_skips_contract_renormalization(tmp_path, monkeypatch):
    database = tmp_path / "usage.sqlite3"
    append_sqlite_facts(database, [usage("one"), usage("two")])
    calls = 0
    original = ledger.normalize_fact

    def counted(fact):
        nonlocal calls
        calls += 1
        return original(fact)

    monkeypatch.setattr(ledger, "normalize_fact", counted)
    rows = query_sqlite_facts(
        database,
        fact_type="usage_event_v1",
        contract_validation=False,
    )

    assert [row["source_event_id"] for row in rows] == ["one", "two"]
    assert calls == 0


def test_bounded_sqlite_query_supports_exclusive_upper_timestamp_bound(tmp_path):
    database = tmp_path / "usage.sqlite3"
    append_sqlite_facts(database, [
        usage("before", occurred_at="2026-07-13T03:59:59.9Z"),
        usage("at-bound", occurred_at="2026-07-13T04:00:00Z"),
        usage("after", occurred_at="2026-07-13T05:00:00Z"),
    ])

    rows = query_sqlite_facts(
        database,
        fact_type="usage_event_v1",
        occurred_or_observed_at_lt="2026-07-13T04:00:00Z",
    )

    assert [row["source_event_id"] for row in rows] == ["before"]


def test_sqlite_timestamp_bounds_order_fractional_instants_chronologically(tmp_path):
    database = tmp_path / "usage.sqlite3"
    append_sqlite_facts(database, [
        usage("before-lower", occurred_at="2026-07-13T02:59:59.9Z"),
        usage("after-lower", occurred_at="2026-07-13T03:00:00.1Z"),
        usage("before-upper", occurred_at="2026-07-13T03:59:59.9Z"),
        usage("at-upper", occurred_at="2026-07-13T04:00:00Z"),
        usage("after-upper", occurred_at="2026-07-13T04:00:00.1Z"),
    ])

    rows = query_sqlite_facts(
        database,
        fact_type="usage_event_v1",
        occurred_or_observed_at_gte="2026-07-13T03:00:00Z",
        occurred_or_observed_at_lt="2026-07-13T04:00:00Z",
    )

    assert [row["source_event_id"] for row in rows] == ["after-lower", "before-upper"]


@pytest.mark.parametrize("kwargs", [
    {"filters": {"not_a_column": "x"}},
    {"filters": {"provider": 1}},
    {"order": "sideways"},
    {"limit": -1},
    {"limit": 100_002},
    {"offset": -1},
    {"occurred_or_observed_at_gte": "not-a-time"},
    {"occurred_or_observed_at_lt": "not-a-time"},
    {"contract_validation": "no"},
])
def test_sqlite_query_rejects_unbounded_or_malformed_arguments(tmp_path, kwargs):
    database = tmp_path / "usage.sqlite3"
    append_sqlite_facts(database, [usage()])
    with pytest.raises(ValidationError):
        query_sqlite_facts(database, fact_type="usage_event_v1", **kwargs)


@pytest.mark.parametrize(("fact_factory", "fact_type", "filters", "index_name"), [
    (usage, "usage_event_v1", {"provider": "example"}, "facts_provider_idx"),
    (usage, "usage_event_v1", {"harness": "test"}, "facts_harness_idx"),
    (usage, "usage_event_v1", {"purpose": "main"}, "facts_purpose_idx"),
    (quota, "quota_observation_v1", {"quota_name": "week"}, "facts_quota_name_idx"),
    (billing, "billing_fact_v1", {"transaction_kind": "charge", "status": "posted"}, "facts_transaction_idx"),
    (billing, "billing_fact_v1", {"status": "posted"}, "facts_status_idx"),
    (lambda: billing(line_item_id="line-1"), "billing_fact_v1", {"line_item_id": "line-1"}, "facts_line_item_idx"),
])
def test_sqlite_filtered_query_plan_uses_relevant_index(
    tmp_path, fact_factory, fact_type, filters, index_name, monkeypatch
):
    database = tmp_path / f"{fact_type}.sqlite3"
    append_sqlite_facts(database, [fact_factory()])
    plans = []
    original = ledger._execute_bounded_facts_query

    def capture(connection, sql, parameters):
        plans.extend(row[3] for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", parameters))
        return original(connection, sql, parameters)

    monkeypatch.setattr(ledger, "_execute_bounded_facts_query", capture)
    assert query_sqlite_facts(database, fact_type=fact_type, filters=filters)
    assert any("SEARCH" in detail and index_name in detail for detail in plans), plans


def test_sqlite_timestamp_lower_bound_uses_timestamp_index(tmp_path, monkeypatch):
    database = tmp_path / "usage.sqlite3"
    append_sqlite_facts(database, [usage()])
    plans = []
    original = ledger._execute_bounded_facts_query

    def capture(connection, sql, parameters):
        plans.extend(row[3] for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", parameters))
        return original(connection, sql, parameters)

    monkeypatch.setattr(ledger, "_execute_bounded_facts_query", capture)
    assert query_sqlite_facts(
        database,
        fact_type="usage_event_v1",
        occurred_or_observed_at_gte="2026-07-01T00:00:00Z",
    )
    assert any("SEARCH" in detail and "facts_timestamp_idx" in detail for detail in plans), plans


def test_sqlite_timestamp_upper_bound_uses_timestamp_index(tmp_path, monkeypatch):
    database = tmp_path / "usage.sqlite3"
    append_sqlite_facts(database, [usage()])
    plans = []
    original = ledger._execute_bounded_facts_query

    def capture(connection, sql, parameters):
        plans.extend(row[3] for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", parameters))
        return original(connection, sql, parameters)

    monkeypatch.setattr(ledger, "_execute_bounded_facts_query", capture)
    assert query_sqlite_facts(
        database,
        fact_type="usage_event_v1",
        occurred_or_observed_at_lt="2026-08-01T00:00:00Z",
    )
    assert any("SEARCH" in detail and "facts_timestamp_idx" in detail for detail in plans), plans


def test_writable_open_migrates_only_exact_legacy_timestamp_index_idempotently(tmp_path):
    database = tmp_path / "usage.sqlite3"
    append_sqlite_facts(database, [usage()])
    connection = sqlite3.connect(database)
    connection.executescript(
        "DROP INDEX facts_timestamp_idx; "
        "CREATE INDEX facts_timestamp_idx ON facts(occurred_or_observed_at);"
    )
    connection.close()

    append_sqlite_facts(database, [usage("after-migration")])
    append_sqlite_facts(database, [usage("after-migration")])

    connection = sqlite3.connect(database)
    index_sql = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='index' AND name='facts_timestamp_idx'"
    ).fetchone()[0]
    connection.close()
    assert index_sql == "CREATE INDEX facts_timestamp_idx ON facts(rtrim(occurred_or_observed_at, 'Z'))"


def test_legacy_timestamp_migration_audits_other_schema_before_ddl(tmp_path):
    database = tmp_path / "usage.sqlite3"
    append_sqlite_facts(database, [usage()])
    connection = sqlite3.connect(database)
    connection.executescript(
        "DROP INDEX facts_timestamp_idx; "
        "CREATE INDEX facts_timestamp_idx ON facts(occurred_or_observed_at); "
        "DROP INDEX facts_provider_idx; "
        "CREATE INDEX facts_provider_idx ON facts(harness);"
    )
    connection.close()

    with pytest.raises(MalformedLedgerError, match="index"):
        append_sqlite_facts(database, [usage("must-not-append")])

    connection = sqlite3.connect(database)
    assert connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='index' AND name='facts_timestamp_idx'"
    ).fetchone()[0] == "CREATE INDEX facts_timestamp_idx ON facts(occurred_or_observed_at)"
    connection.close()


def test_writable_open_does_not_repair_unrecognized_timestamp_index_sql(tmp_path):
    database = tmp_path / "usage.sqlite3"
    append_sqlite_facts(database, [usage()])
    connection = sqlite3.connect(database)
    connection.executescript(
        "DROP INDEX facts_timestamp_idx; "
        "CREATE INDEX facts_timestamp_idx ON facts(rtrim(occurred_or_observed_at, 'z'));"
    )
    wrong_sql = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='index' AND name='facts_timestamp_idx'"
    ).fetchone()[0]
    connection.close()

    with pytest.raises(MalformedLedgerError, match="index"):
        append_sqlite_facts(database, [usage("must-not-append")])

    connection = sqlite3.connect(database)
    assert connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='index' AND name='facts_timestamp_idx'"
    ).fetchone()[0] == wrong_sql
    connection.close()


def test_read_only_open_rejects_legacy_timestamp_index_without_migrating(tmp_path):
    database = tmp_path / "usage.sqlite3"
    append_sqlite_facts(database, [usage()])
    connection = sqlite3.connect(database)
    connection.executescript(
        "DROP INDEX facts_timestamp_idx; "
        "CREATE INDEX facts_timestamp_idx ON facts(occurred_or_observed_at);"
    )
    legacy_sql = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='index' AND name='facts_timestamp_idx'"
    ).fetchone()[0]
    connection.close()

    with pytest.raises(MalformedLedgerError, match="index"):
        query_sqlite_facts(database, fact_type="usage_event_v1")

    connection = sqlite3.connect(database)
    assert connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='index' AND name='facts_timestamp_idx'"
    ).fetchone()[0] == legacy_sql
    connection.close()


def test_sqlite_query_detects_returned_row_hash_or_typed_column_tampering(tmp_path):
    database = tmp_path / "usage.sqlite3"
    append_sqlite_facts(database, [usage()])
    connection = sqlite3.connect(database)
    trigger_sql = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name='facts_no_update'"
    ).fetchone()[0]
    connection.executescript("DROP TRIGGER facts_no_update; UPDATE facts SET provider='tampered';")
    connection.execute(trigger_sql)
    connection.close()
    with pytest.raises(MalformedLedgerError, match="extracted columns"):
        query_sqlite_facts(
            database, fact_type="usage_event_v1", filters={"provider": "tampered"}
        )
