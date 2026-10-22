"""Antigravity CLI (agy) model calls (Paperclip SIL-78, 2026-10-22: "ensure agy
usage is accounted for in the ai-usage tracker").

The fixture stores keep the shape of the live ones
(~/.gemini/antigravity-cli/conversations/<id>.db: protobuf blobs in
gen_metadata, steps.metadata and trajectory_metadata_blob), with made-up ids
and counts. Field numbers are the ones validated on Sol against Hermes'
provider-reported rows for the same bridged calls.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from codex_usage_tracker.antigravity_usage import antigravity_stores, collect_antigravity_usage
from codex_usage_tracker.collector import collect_all

T0 = 1792651791  # 2026-10-22T06:49:51Z


# --- minimal protobuf encoder -------------------------------------------------
def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field(number: int, value) -> bytes:
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    if isinstance(value, str):
        value = value.encode()
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def _msg(*fields) -> bytes:
    return b"".join(_field(n, v) for n, v in fields)


def _stamp(seconds: int, nanos: int = 0) -> bytes:
    return _msg((1, seconds), (2, nanos))


def _usage(inp, out, cache_read=0, thinking=0, response_id="resp-1", extra=()):
    fields = [(1, 1318), (2, inp), (3, out), (6, 24), (7, "bot-x"), (9, thinking), (10, out - thinking),
              (11, response_id), *extra]
    if cache_read:
        fields.append((5, cache_read))
    return _msg(*fields)


def _generation(usage: bytes, model="gemini-3.8-flash-n", request_id="traj-0", used_claude=False) -> bytes:
    pairs = [("request_id", request_id), ("trajectory_id", "traj"), ("model_enum", "MODEL_PLACEHOLDER_M318"),
             ("used_claude", "true" if used_claude else "false")]
    inner = _msg((4, usage), (19, model), *((20, _msg((1, k), (2, v))) for k, v in pairs))
    return _msg((1, inner), (4, "gen-uuid"))


def _store(home: Path, conversation_id: str, workspace: str, generations, steps) -> Path:
    path = home / "conversations" / f"{conversation_id}.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE gen_metadata (idx integer PRIMARY KEY, data blob, size integer NOT NULL DEFAULT 0)")
    con.execute("CREATE TABLE steps (idx integer PRIMARY KEY, step_type integer NOT NULL DEFAULT 0, metadata blob)")
    con.execute("CREATE TABLE trajectory_metadata_blob (id text DEFAULT 'main' PRIMARY KEY, data blob)")
    con.execute("INSERT INTO trajectory_metadata_blob VALUES ('main', ?)",
                (_msg((1, _msg((1, f"file://{workspace}")))),))
    for idx, data in enumerate(generations):
        con.execute("INSERT INTO gen_metadata VALUES (?, ?, ?)", (idx, data, len(data)))
    for idx, meta in enumerate(steps):
        con.execute("INSERT INTO steps VALUES (?, 15, ?)", (idx, meta))
    con.commit()
    con.close()
    return path


def _step(usage: bytes | None, completed: int | None, created: int = T0 - 10) -> bytes:
    fields = [(1, _stamp(created))]
    if completed is not None:
        fields.append((7, _stamp(completed, 500_000_000)))
    if usage is not None:
        fields.append((9, usage))
    return _msg(*fields)


def _summaries(home: Path, rows) -> None:
    con = sqlite3.connect(home / "conversation_summaries.db")
    con.execute("CREATE TABLE conversation_summaries (conversation_id text PRIMARY KEY, workspace_uris text NOT NULL,"
                " parent_conversation_id text NOT NULL DEFAULT '')")
    con.executemany("INSERT INTO conversation_summaries VALUES (?, ?, ?)",
                    [(cid, json.dumps([f"file://{ws}"]), parent) for cid, ws, parent in rows])
    con.commit()
    con.close()


PAPERCLIP_WS = "/home/u/.local/share/paperclip-learning/instance/projects/p/_default"
RELAY_WS = "/var/lib/svc/meridian-agy/antigravity-workspaces-a527d8d1b75c/conversation-RYBItz"


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "antigravity-cli"
    u1 = _usage(36710, 1780, thinking=1644, response_id="resp-a")
    u2 = _usage(2578, 201, cache_read=36588, thinking=76, response_id="resp-b")
    u_pending = _usage(500, 50, response_id="resp-pending")
    u_unmapped = _usage(100, 10, response_id="resp-c", extra=[(4, 77)])
    _store(home, "direct-1", PAPERCLIP_WS,
           [_generation(u1, request_id="traj-0"), _generation(u2, request_id="traj-1"),
            # live conversation: generation written, its step not yet completed
            _generation(u_pending, request_id="traj-2"),
            # a generation with no tokens is not a model call
            _generation(_msg((1, 1318)), model="", request_id="traj-3")],
           [_step(None, None), _step(u1, T0), _step(u2, T0 + 30), _step(u2, T0 + 31), _step(u_pending, None)])
    _store(home, "direct-2", "/home/u/scratch",
           [_generation(u_unmapped, model="claude-opus-4-6-thinking", request_id="t2-0", used_claude=True)],
           [_step(u_unmapped, T0 + 60)])
    # Meridian bridge conversation: the calling harness records it.
    u_relay = _usage(2629, 760, cache_read=49826, response_id="resp-relay")
    _store(home, "relay-1", RELAY_WS, [_generation(u_relay)], [_step(u_relay, T0 + 90)])
    _summaries(home, [("direct-1", PAPERCLIP_WS, ""), ("direct-2", "/home/u/scratch", "direct-1"),
                      ("relay-1", RELAY_WS, "")])
    return home


def _drain(generator):
    rows = []
    try:
        while True:
            rows.append(next(generator))
    except StopIteration as stop:
        return rows, stop.value


def test_direct_generations_become_exact_partial_facts(tmp_path):
    rows, diagnostics = _drain(collect_antigravity_usage(_home(tmp_path), "sol:antigravity"))
    assert [r["source_event_id"] for r in rows] == ["direct-1:traj-0", "direct-1:traj-1", "direct-2:t2-0"]
    first, second, third = rows
    assert (first["input_tokens"], first["output_tokens"], first["cache_read_tokens"]) == (36710, 1780, 0)
    assert (second["input_tokens"], second["output_tokens"], second["cache_read_tokens"]) == (2578, 201, 36588)
    assert first["reasoning_tokens"] == 1644
    # Completion time of the earliest completed step carrying the response.
    assert first["occurred_at"] == "2026-10-22T06:49:51.5Z"
    assert second["occurred_at"] == "2026-10-22T06:50:21.5Z"
    for row in rows:
        assert row["provider"] == "antigravity" and row["harness"] == "antigravity"
        assert row["source_namespace"] == "sol:antigravity"
        assert row["record_kind"] == "api_attempt" and row["measurement_confidence"] == "exact"
        assert row["cache_write_tokens"] is None and row["missing_fields"] == ["cache_write_tokens"]
        assert row["usage_completeness"] == "partial"
        assert (row["billing_mode"], row["cost_status"], row["cost_source"]) == (
            "subscription_included", "included", "subscription")
        assert row["model_requested"] is None and "model_requested" in row["attribution_gaps"]
    assert first["model_reported"] == "gemini-3.8-flash-n"
    assert first["surface"] == "paperclip" and first["purpose"] == "main"
    assert first["x_antigravity_response_id"] == "resp-a"
    assert third["model_reported"] == "claude-opus-4-6-thinking" and third["x_antigravity_used_claude"] is True
    assert third["surface"] == "cli" and third["purpose"] == "subagent"
    # An unrecognised usage field is surfaced, never guessed into a column.
    assert third["x_antigravity_unmapped_usage_fields"] == {"4": 77}
    assert "x_antigravity_unmapped_usage_fields" not in first
    assert diagnostics == {
        "skipped": {"relay_conversation": 1, "zero_usage": 1, "unparsable": 0, "unreadable_store": 0,
                    "deferred_no_completed_step": 1},
        "stores": 3,
        "skipped_files": [],
    }


def test_bridge_detection_reads_the_store_without_summaries(tmp_path):
    home = _home(tmp_path)
    (home / "conversation_summaries.db").unlink()
    rows, diagnostics = _drain(collect_antigravity_usage(home))
    assert {r["session_id"] for r in rows} == {"direct-1", "direct-2"}
    assert diagnostics["skipped"]["relay_conversation"] == 1


def test_missing_home_is_an_empty_lane(tmp_path):
    assert antigravity_stores(tmp_path / "absent") == []
    assert _drain(collect_antigravity_usage(tmp_path / "absent")) == ([], {"skipped": {
        "relay_conversation": 0, "zero_usage": 0, "unparsable": 0, "unreadable_store": 0,
        "deferred_no_completed_step": 0}, "stores": 0, "skipped_files": []})


def test_unreadable_store_is_a_skipped_file_not_fatal(tmp_path):
    home = _home(tmp_path)
    broken = home / "conversations" / "broken.db"
    broken.write_bytes(b"not a database at all" * 10)
    rows, diagnostics = _drain(collect_antigravity_usage(home))
    assert len(rows) == 3 and diagnostics["skipped"]["unreadable_store"] == 1
    assert diagnostics["skipped_files"] == [{"path": str(broken)}]


def test_header_only_wal_with_unopenable_store_reads_as_immutable(tmp_path, monkeypatch):
    """Sol 2026-10-22: a closed agy store kept a 32-byte WAL and a stale -shm;
    the read-only collector got "locking protocol". A frame-less WAL adds no
    pages, so the store is read as immutable instead of skipped."""
    import codex_usage_tracker.antigravity_usage as module

    home = _home(tmp_path)
    store = home / "conversations" / "direct-1.db"
    Path(str(store) + "-wal").write_bytes(b"\x37\x7f\x06\x82" + b"\0" * 28)
    real_connect = sqlite3.connect

    def connect(database, *args, **kwargs):
        if "direct-1.db" in str(database) and "immutable=1" not in str(database):
            raise sqlite3.OperationalError("locking protocol")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", connect)
    rows, diagnostics = _drain(collect_antigravity_usage(home))
    assert {r["source_event_id"] for r in rows} >= {"direct-1:traj-0", "direct-1:traj-1"}
    assert diagnostics["skipped"]["unreadable_store"] == 0
    # A WAL with frames is not ignorable: the store is skipped, not misread.
    Path(str(store) + "-wal").write_bytes(b"\0" * 4096)
    rows, diagnostics = _drain(collect_antigravity_usage(home))
    assert "direct-1" not in {r["session_id"] for r in rows}
    assert diagnostics["skipped"]["unreadable_store"] == 1


def _collect(tmp_path, home):
    return collect_all(
        state_db=tmp_path / "state.db", claude_root=tmp_path / "claude", opencode_dbs=[],
        codex_ledger=tmp_path / "codex.jsonl", usage_ledger=tmp_path / "usage.sqlite3",
        quota_ledger=tmp_path / "quota.sqlite3", search_ledger=tmp_path / "search.sqlite3",
        search_audit=tmp_path / "no-audit.jsonl", live_quota=False, scope="usage", strict_sources=True,
        antigravity_home=home,
    )


def test_collector_lane_appends_once_and_replays(tmp_path):
    home = _home(tmp_path)
    first = _collect(tmp_path, home)
    assert first["lanes"]["antigravity"]["succeeded"] is True
    assert first["usage"]["appended"] == 3
    again = _collect(tmp_path, home)
    assert (again["usage"]["appended"], again["usage"]["replayed"]) == (0, 3)
    con = sqlite3.connect(tmp_path / "usage.sqlite3")
    stored = con.execute("SELECT provider, harness, source_namespace, count(*) FROM facts GROUP BY 1, 2, 3").fetchall()
    assert stored == [("antigravity", "antigravity", "sol:antigravity", 3)]


def _cli_run(tmp_path, monkeypatch, ledger: Path, extra: list[str]) -> None:
    from codex_usage_tracker.cli import main
    from test_fast_incremental_collection import _sidecar

    sidecar = tmp_path / "usage_events.db"
    if not sidecar.exists():
        _sidecar(sidecar, [])
    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    opencode = tmp_path / "opencode.db"
    with sqlite3.connect(opencode) as c:
        c.execute("CREATE TABLE IF NOT EXISTS session(id TEXT)")
    argv = ["tracker", "collect-all", "--scope", "usage", "--no-live-quota", "--state-db", str(sidecar),
            "--claude-root", str(projects), "--opencode-db", str(opencode), "--openclaw-home", "",
            "--usage-ledger", str(ledger), *extra]
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 0


def _antigravity_rows(ledger: Path) -> int:
    with sqlite3.connect(ledger) as con:
        return con.execute("SELECT count(*) FROM facts WHERE harness = 'antigravity'").fetchone()[0]


def test_cli_reads_the_agy_home_by_default_and_empty_disables(tmp_path, monkeypatch, capsys):
    # The live timer passes no --antigravity-home: the lane runs on the default.
    monkeypatch.setenv("ANTIGRAVITY_HOME", str(_home(tmp_path)))
    _cli_run(tmp_path, monkeypatch, tmp_path / "on.sqlite3", [])
    assert _antigravity_rows(tmp_path / "on.sqlite3") == 3
    _cli_run(tmp_path, monkeypatch, tmp_path / "off.sqlite3", ["--antigravity-home", ""])
    assert _antigravity_rows(tmp_path / "off.sqlite3") == 0
