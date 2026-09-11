"""Ruling ledger reader and the rulings-per-dollar join.

Keeper ruling 2026-09-10 ("yeah we need to make ratification a first class
thing … delegate it to subagents to implement"): tokens are a lossy proxy for
cost, cost for value used, value used for value produced. The keeper's unit of
PRODUCED value is a ratification — a keeper ruling — so the honest
denominator-over-numerator is *rulings per dollar of inference*, computed here
rather than mined by hand.

Two halves:

1. A reader for the ruling ledger grammar the Atrium canon lane is landing:
   markdown files whose frontmatter carries ``ruling_ledger: v1`` and whose
   body holds fenced ```` ```ruling ```` blocks of YAML. The canon scan is the
   default source; ``source="service"`` calls Atrium Service's ``/rulings``
   endpoint instead, for after that service is deployed. Neither is required
   to exist — an Atrium with no ruling ledger at all yields an empty series
   with honest coverage fields, never an error and never a fabricated zero
   that reads like "no judgment happened".

2. The weekly join against the existing list-price estimate. Weeks are ISO
   weeks in America/New_York, because the keeper rules in local wall time and
   a UTC week would cut Sunday evenings into the following week. The dollars
   are :mod:`codex_usage_tracker.list_pricing`'s estimate over the same
   canonical usage rows the dashboard prices — same rate table, same
   per-request long-context basis, same unpriced-token coverage.

What the ratio is NOT: it measures *recorded* rulings against *estimated*
list price at the standard service tier. It is not keeper activity (canon
captured a small fraction of adjudication traffic before this ledger existed)
and it is not billed cost (most of this traffic is subscription-included and
bills $0 actual). Both caveats ride in the payload rather than in a footnote
someone can drop.

The YAML parsed here is a deliberately small subset (flat scalars, block
scalars, and lists) implemented inline: this package depends on httpx and
flask only, and a ruling block does not need a YAML engine. Anything the
subset cannot parse is a counted ``parse_error`` with a stated reason — never
a silently dropped entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .list_pricing import (
    RATE_BASIS,
    accumulate_price_bucket,
    estimate_list_price,
    finalize_price_buckets,
)

# The keeper rules in local wall time; ISO weeks are anchored there.
RULINGS_TIMEZONE = ZoneInfo("America/New_York")
RULINGS_TIMEZONE_NAME = "America/New_York"

# Frontmatter marker that makes a markdown file a ruling ledger.
RULING_LEDGER_KEY = "ruling_ledger"
RULING_LEDGER_VALUE = "v1"

# Fixed grammar (canon lane, 2026-09-10). An entry whose class is not one of
# these is malformed, not a new class: inventing classes at the reader would
# let a typo become a category.
RULING_CLASSES: tuple[str, ...] = (
    "correction",
    "scope",
    "routing",
    "replan",
    "adoption",
    "gate",
    "assent",
    "convention",
)

# Directories the scan never descends into. `.git` is object storage,
# `50_drive` is a blocked symlink tree, and the two archive beams hold
# superseded material whose rulings must not re-enter a live count.
EXCLUDED_DIRECTORY_NAMES = frozenset({".git", "50_drive", "99_archive", "08_archives"})

DEFAULT_WEEKS = 8
MAX_WEEKS = 104
DEFAULT_SERVICE_URL = "http://127.0.0.1:8766"
RULING_SOURCES = ("canon", "service")

# Bounds on the scan. A ruling ledger is a hand-kept markdown file; these
# ceilings keep a pathological tree from turning a dashboard request into an
# unbounded read, and each one is reported when it bites.
_FRONTMATTER_PROBE_BYTES = 8192
_MAX_LEDGER_BYTES = 4 * 1024 * 1024
_MAX_LEDGERS = 512
_MAX_PARSE_ERROR_DETAILS = 50

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-(?P<version>[0-9a-f])[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_KEY_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_.-]*):(?P<rest>.*)$")
_FRONTMATTER_KEY_RE = re.compile(rf"^{RULING_LEDGER_KEY}:\s*(?P<value>\S+)\s*$")
_FENCE_RE = re.compile(r"^(?P<fence>`{3,}|~{3,})(?P<info>[^`~]*)$")


class RulingParseError(ValueError):
    """One ruling block could not be read. Counted, never swallowed."""


class RulingsSourceUnavailable(RuntimeError):
    """The requested ruling source could not be reached or understood."""


@dataclass(frozen=True)
class Ruling:
    """One ratification: a uid-bearing keeper ruling at a point in time."""

    uid: str
    ruled_at: datetime
    ruling_class: str
    grade: str | None = None
    state: str | None = None
    who: str | None = None
    verbatim: str | None = None
    rationale: str | None = None
    applies_to: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()
    # The writer identity, kept whole when the entry gives the author/
    # last_touched key set (standard §§8.4-8.5) rather than a bare name.
    recorded_by: dict[str, Any] | str | None = None
    source_path: str | None = None


@dataclass(frozen=True)
class RulingScan:
    """Everything one read of the ruling corpus produced, coverage included."""

    rulings: tuple[Ruling, ...] = ()
    ledgers_scanned: int = 0
    entries_parsed: int = 0
    parse_errors: tuple[dict[str, Any], ...] = ()
    duplicate_uids: int = 0
    source: str = "canon"
    atrium_root: str | None = None
    truncated: bool = False

    @property
    def parse_error_count(self) -> int:
        return len(self.parse_errors)


# ── YAML subset ────────────────────────────────────────────────────────────


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _split_inline_list(raw: str) -> list[str]:
    """Split ``[a, "b, c"]`` honoring quotes; no nesting, by design."""
    items: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in raw:
        if quote is not None:
            if char == quote:
                quote = None
            else:
                current.append(char)
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char == ",":
            items.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        items.append(tail)
    return [item for item in items if item]


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _scalar(value: str) -> Any:
    """One scalar value: YAML nulls become ``None``, quotes are stripped."""
    if value in ("null", "Null", "NULL", "~"):
        return None
    return _strip_quotes(value)


def _parse_mapping(lines: list[str], index: int, indent: int) -> tuple[dict[str, Any], int]:
    """Parse one mapping level, returning it and the line index after it."""
    result: dict[str, Any] = {}
    while index < len(lines):
        raw_line = lines[index]
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        if stripped in ("---", "..."):
            raise RulingParseError("multi-document ruling blocks are not supported")
        line_indent = _indent_of(raw_line)
        if line_indent < indent:
            break
        if line_indent > indent:
            raise RulingParseError(f"unexpected indentation at line {index + 1}")
        match = _KEY_RE.match(stripped)
        if match is None:
            raise RulingParseError(f"not a key: value line at line {index + 1}")
        key = match.group("key")
        if key in result:
            raise RulingParseError(f"duplicate key: {key}")
        rest = match.group("rest")
        if rest and not rest.startswith(" "):
            raise RulingParseError(f"missing space after '{key}:' at line {index + 1}")
        value = rest.strip()
        index += 1

        if value.startswith("|") or value.startswith(">"):
            if value.rstrip("+-") not in ("|", ">"):
                raise RulingParseError(f"unsupported block scalar '{value}' for {key}")
            folded = value.startswith(">")
            block: list[str] = []
            block_indent: int | None = None
            while index < len(lines):
                candidate = lines[index]
                if candidate.strip():
                    candidate_indent = _indent_of(candidate)
                    if candidate_indent <= indent:
                        break
                    if block_indent is None:
                        block_indent = candidate_indent
                    elif candidate_indent < block_indent:
                        break
                    block.append(candidate[block_indent:])
                else:
                    block.append("")
                index += 1
            while block and not block[-1].strip():
                block.pop()
            # Trailing blank lines were dropped above, so the three chomping
            # indicators collapse to one result here; the reader keeps
            # accepting all three so a ledger author's `|-` is not an error.
            if folded:
                result[key] = " ".join(line.strip() for line in block if line.strip())
            else:
                result[key] = "\n".join(block)
            continue

        if value.startswith("[") and value.endswith("]"):
            result[key] = _split_inline_list(value[1:-1])
            continue

        if value.startswith("{") and value.endswith("}"):
            # Inline mapping, e.g. recorded_by: {name: ..., harness: ...}.
            inline: dict[str, Any] = {}
            for item in _split_inline_list(value[1:-1]):
                inner_key, separator, inner_value = item.partition(":")
                if not separator:
                    raise RulingParseError(f"inline mapping item without a key under {key}")
                inline[inner_key.strip()] = _scalar(inner_value.strip())
            result[key] = inline
            continue

        if value:
            result[key] = _scalar(value)
            continue

        # Empty value: a block list, a nested mapping, or an explicit null.
        probe = index
        while probe < len(lines) and not lines[probe].strip():
            probe += 1
        if probe >= len(lines) or _indent_of(lines[probe]) <= indent:
            result[key] = None
            continue
        child_indent = _indent_of(lines[probe])
        if lines[probe].strip().startswith("- "):
            items: list[Any] = []
            while index < len(lines):
                candidate = lines[index]
                if not candidate.strip():
                    index += 1
                    continue
                if _indent_of(candidate) <= indent:
                    break
                item = candidate.strip()
                if not item.startswith("- "):
                    raise RulingParseError(f"unexpected line in the list under {key}")
                items.append(_scalar(item[2:].strip()))
                index += 1
            result[key] = items
            continue
        # Nested mapping — `recorded_by` is one (the author/last_touched key
        # set from standard §§8.4-8.5), so the reader keeps it whole instead
        # of flattening the writer's identity.
        child, index = _parse_mapping(lines, index, child_indent)
        result[key] = child
    return result, index


def parse_ruling_yaml(text: str) -> dict[str, Any]:
    """Parse one ruling block's YAML mapping.

    Supported: ``key: scalar`` (with YAML nulls and quoted strings), block
    scalars (``|``, ``|-``, ``>``, ``>-`` and their ``+`` forms — what
    ``verbatim`` and ``rationale`` use), block and inline lists, and nested
    mappings such as ``recorded_by``. Anchors, tags, and multi-document
    streams are NOT supported and raise, because a reader that quietly
    ignores structure it does not understand reports a ruling it did not
    read.
    """
    lines = text.splitlines()
    base_indent = 0
    for line in lines:
        if line.strip():
            base_indent = _indent_of(line)
            break
    mapping, index = _parse_mapping(lines, 0, base_indent)
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index < len(lines):
        raise RulingParseError(f"unexpected content at line {index + 1}")
    return mapping


# ── Entry normalization ────────────────────────────────────────────────────


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float, bool)):
        return str(value)
    raise RulingParseError(f"expected a scalar, got {type(value).__name__}")


def _as_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    raise RulingParseError(f"expected a list, got {type(value).__name__}")


def _as_recorded_by(value: Any) -> dict[str, Any] | str | None:
    """``recorded_by`` is either a name or the writer mapping; keep both."""
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return _as_text(value)


def parse_timestamp(raw: Any) -> datetime:
    """Parse an offset-bearing ISO timestamp, or raise.

    A naive timestamp is rejected rather than assumed UTC: the whole point of
    local ISO weeks is that the offset decides which week a Sunday-evening
    ruling lands in, and guessing it would silently move rulings between
    buckets.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise RulingParseError("ruled_at is missing")
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise RulingParseError(f"ruled_at is not an ISO timestamp: {raw!r}") from exc
    if parsed.tzinfo is None:
        raise RulingParseError(f"ruled_at has no UTC offset: {raw!r}")
    return parsed


def ruling_from_mapping(mapping: Mapping[str, Any], *, source_path: str | None = None) -> Ruling:
    """Validate one parsed block into a :class:`Ruling`, or raise."""
    uid = _as_text(mapping.get("uid"))
    if not uid:
        raise RulingParseError("uid is missing")
    match = _UUID_RE.match(uid.lower())
    if match is None:
        raise RulingParseError(f"uid is not a UUID: {uid!r}")
    if match.group("version") != "7":
        raise RulingParseError(f"uid is not a UUIDv7: {uid!r}")
    ruled_at = parse_timestamp(mapping.get("ruled_at"))
    ruling_class = _as_text(mapping.get("class"))
    if not ruling_class:
        raise RulingParseError("class is missing")
    if ruling_class not in RULING_CLASSES:
        raise RulingParseError(f"unknown ruling class: {ruling_class!r}")
    # `grade` and `state` are deliberately NOT validated. They carry the
    # Valarin vocabularies (grade: ratified | approved | offered | floated;
    # state: ruled | built | adopted | retired | repaired) which evolve on
    # their own schedule, and the coordinating ruling is explicit that an
    # unknown value must still be counted, recorded verbatim in the by-grade
    # breakdown, and never turned into a dropped entry.
    return Ruling(
        uid=uid.lower(),
        ruled_at=ruled_at,
        ruling_class=ruling_class,
        grade=_as_text(mapping.get("grade")),
        state=_as_text(mapping.get("state")),
        who=_as_text(mapping.get("who")),
        verbatim=_as_text(mapping.get("verbatim")),
        rationale=_as_text(mapping.get("rationale")),
        applies_to=_as_list(mapping.get("applies_to")),
        supersedes=_as_list(mapping.get("supersedes")),
        recorded_by=_as_recorded_by(mapping.get("recorded_by")),
        source_path=source_path,
    )


def ruling_as_dict(ruling: Ruling) -> dict[str, Any]:
    """Serialize one ruling for an API/CLI payload.

    ``verbatim`` is the keeper's own words and ``rationale`` can carry
    working context; both are private-surface only, exactly like every other
    canonical read in this package.
    """
    return {
        "uid": ruling.uid,
        "ruled_at": ruling.ruled_at.isoformat(),
        "class": ruling.ruling_class,
        "grade": ruling.grade,
        "state": ruling.state,
        "who": ruling.who,
        "verbatim": ruling.verbatim,
        "rationale": ruling.rationale,
        "applies_to": list(ruling.applies_to),
        "supersedes": list(ruling.supersedes),
        "recorded_by": (
            dict(ruling.recorded_by)
            if isinstance(ruling.recorded_by, Mapping)
            else ruling.recorded_by
        ),
        "source_path": ruling.source_path,
    }


# ── Canon scan ─────────────────────────────────────────────────────────────


def _is_ruling_ledger(path: Path) -> bool:
    """Cheap frontmatter probe: does this markdown file declare the ledger?"""
    try:
        with path.open("rb") as handle:
            head = handle.read(_FRONTMATTER_PROBE_BYTES)
    except OSError:
        return False
    text = head.decode("utf-8", "replace")
    if not text.startswith("---"):
        return False
    for line in text.splitlines()[1:]:
        if line.strip() in ("---", "..."):
            return False
        match = _FRONTMATTER_KEY_RE.match(line.strip())
        if match is not None:
            return _strip_quotes(match.group("value")) == RULING_LEDGER_VALUE
    return False


def iter_ruling_ledger_paths(atrium_root: str | Path) -> Iterator[Path]:
    """Yield every ``ruling_ledger: v1`` markdown file under the Atrium root.

    Walks with :meth:`Path.walk`-style pruning so the excluded beams are never
    descended into (the archives hold superseded rulings; `.git` holds none).
    """
    import os

    root = Path(atrium_root).expanduser()
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames if name not in EXCLUDED_DIRECTORY_NAMES
        )
        for filename in sorted(filenames):
            if not filename.endswith(".md"):
                continue
            candidate = Path(dirpath) / filename
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if _is_ruling_ledger(candidate):
                yield candidate


def iter_ruling_blocks(text: str) -> Iterator[tuple[int, str]]:
    """Yield ``(opening line number, block body)`` for each ```ruling fence.

    Fenced blocks are walked in document order and every fence is consumed,
    not just the ruling ones, so a ```ruling block shown as an EXAMPLE inside
    a wider ````…```` documentation fence is skipped rather than read as a
    live entry. The proposal note that defines this grammar contains exactly
    that: a placeholder entry with ``uid: <uuidv7, minted per entry>`` sitting
    a few lines above the real first ruling. A scanner that grepped for the
    tag would report the documentation as a malformed entry forever.
    """
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = _FENCE_RE.match(lines[index].strip())
        if match is None:
            index += 1
            continue
        fence = match.group("fence")
        info = match.group("info").strip()
        opened_at = index + 1
        index += 1
        body: list[str] = []
        closed = False
        while index < len(lines):
            candidate = lines[index].strip()
            # A closing fence is at least as long as its opener and carries
            # nothing but fence characters.
            if len(candidate) >= len(fence) and set(candidate) == {fence[0]}:
                closed = True
                index += 1
                break
            body.append(lines[index])
            index += 1
        if info != "ruling":
            continue
        if not closed:
            raise RulingParseError(f"unterminated ruling fence opened at line {opened_at}")
        yield opened_at, "\n".join(body)


def read_ruling_ledger(path: Path) -> tuple[list[Ruling], list[dict[str, Any]]]:
    """Read one ledger file into ``(rulings, parse_errors)``."""
    errors: list[dict[str, Any]] = []
    try:
        if path.stat().st_size > _MAX_LEDGER_BYTES:
            return [], [
                {
                    "path": str(path),
                    "line": None,
                    "reason": f"ledger exceeds {_MAX_LEDGER_BYTES} bytes",
                }
            ]
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [{"path": str(path), "line": None, "reason": f"unreadable: {exc.strerror}"}]
    rulings: list[Ruling] = []
    try:
        blocks = list(iter_ruling_blocks(text))
    except RulingParseError as exc:
        return [], [{"path": str(path), "line": None, "reason": str(exc)}]
    for line_number, body in blocks:
        try:
            mapping = parse_ruling_yaml(body)
            rulings.append(ruling_from_mapping(mapping, source_path=str(path)))
        except RulingParseError as exc:
            errors.append({"path": str(path), "line": line_number, "reason": str(exc)})
    return rulings, errors


def scan_canon_rulings(atrium_root: str | Path) -> RulingScan:
    """Scan the Atrium canon for ruling ledgers and read every entry.

    An Atrium with no ledger yields an empty scan with ``ledgers_scanned=0``:
    that is a true statement about the corpus, and it is the state until the
    canon lane lands the first ledger.
    """
    rulings: list[Ruling] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    ledgers = 0
    truncated = False
    for path in iter_ruling_ledger_paths(atrium_root):
        if ledgers >= _MAX_LEDGERS:
            truncated = True
            break
        ledgers += 1
        file_rulings, file_errors = read_ruling_ledger(path)
        errors.extend(file_errors)
        for ruling in file_rulings:
            if ruling.uid in seen:
                duplicates += 1
                continue
            seen.add(ruling.uid)
            rulings.append(ruling)
    rulings.sort(key=lambda ruling: (ruling.ruled_at, ruling.uid))
    return RulingScan(
        rulings=tuple(rulings),
        ledgers_scanned=ledgers,
        entries_parsed=len(rulings) + duplicates,
        parse_errors=tuple(errors),
        duplicate_uids=duplicates,
        source="canon",
        atrium_root=str(Path(atrium_root).expanduser()),
        truncated=truncated,
    )


# ── Atrium Service client (optional source) ────────────────────────────────


def fetch_service_rulings(
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    base_url: str = DEFAULT_SERVICE_URL,
    repo: str | None = None,
    timeout: float = 6.0,
) -> RulingScan:
    """Read rulings from Atrium Service's ``GET /rulings`` instead of the tree.

    This is the forward path: Atrium Service is gaining ``rulings --since
    --until --by week`` and the matching endpoint, but it is a Nix-built
    service and is not deployed yet, so the canon scan stays the default. The
    response envelope is read leniently (``rulings`` or ``entries``) and each
    entry goes through the same validation as a fenced block — a service that
    hands back a malformed entry gets it counted as a parse error, not
    trusted for being a service.
    """
    params: dict[str, str] = {}
    if since is not None:
        params["since"] = since.isoformat()
    if until is not None:
        params["until"] = until.isoformat()
    if repo is not None:
        params["repo"] = repo
    url = f"{base_url.rstrip('/')}/rulings"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "codex-usage-tracker/rulings"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise RulingsSourceUnavailable(
            f"atrium-service rulings endpoint unavailable: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict):
        raise RulingsSourceUnavailable("atrium-service rulings payload is not an object")
    entries = payload.get("rulings")
    if entries is None:
        entries = payload.get("entries")
    if not isinstance(entries, list):
        raise RulingsSourceUnavailable("atrium-service rulings payload has no entry list")
    rulings: list[Ruling] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    for position, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            errors.append({"path": url, "line": position, "reason": "entry is not an object"})
            continue
        try:
            ruling = ruling_from_mapping(entry, source_path=str(entry.get("source_path") or url))
        except RulingParseError as exc:
            errors.append({"path": url, "line": position, "reason": str(exc)})
            continue
        if ruling.uid in seen:
            duplicates += 1
            continue
        seen.add(ruling.uid)
        rulings.append(ruling)
    rulings.sort(key=lambda ruling: (ruling.ruled_at, ruling.uid))
    ledgers = payload.get("ledgers_scanned")
    return RulingScan(
        rulings=tuple(rulings),
        ledgers_scanned=int(ledgers) if isinstance(ledgers, int) else 0,
        entries_parsed=len(rulings) + duplicates,
        parse_errors=tuple(errors),
        duplicate_uids=duplicates,
        source="service",
        atrium_root=repo,
    )


def load_rulings(
    *,
    atrium_root: str | Path | None = None,
    source: str = "canon",
    service_url: str = DEFAULT_SERVICE_URL,
    since: datetime | None = None,
    until: datetime | None = None,
) -> RulingScan:
    """Read rulings from the requested source."""
    if source not in RULING_SOURCES:
        raise ValueError(f"unknown rulings source: {source!r}")
    if source == "service":
        return fetch_service_rulings(
            since=since,
            until=until,
            base_url=service_url,
            repo=str(atrium_root) if atrium_root is not None else None,
        )
    if atrium_root is None:
        raise ValueError("canon source requires an atrium root")
    return scan_canon_rulings(atrium_root)


# ── ISO weeks in America/New_York ──────────────────────────────────────────


def local_week_start_date(moment: datetime) -> date:
    """The Monday of the ISO week this instant falls in, in local time."""
    local = moment.astimezone(RULINGS_TIMEZONE)
    return local.date() - timedelta(days=local.weekday())


def week_key(day: date) -> str:
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def week_bounds(start_day: date) -> tuple[datetime, datetime]:
    """Local ``[Monday 00:00, next Monday 00:00)`` for a week-start date.

    The end is computed from the date and re-localized rather than by adding
    seven days to the start instant, so a DST week is 167 or 169 hours long
    and still ends at local midnight.
    """
    start = datetime.combine(start_day, time.min, tzinfo=RULINGS_TIMEZONE)
    end = datetime.combine(start_day + timedelta(days=7), time.min, tzinfo=RULINGS_TIMEZONE)
    return start, end


def recent_week_start_dates(weeks: int, now: datetime | None = None) -> list[date]:
    """The last ``weeks`` ISO week starts, oldest first, including this week."""
    if weeks < 1:
        raise ValueError("weeks must be at least 1")
    current = local_week_start_date(now or datetime.now(timezone.utc))
    return [current - timedelta(days=7 * offset) for offset in range(weeks - 1, -1, -1)]


# ── The join ───────────────────────────────────────────────────────────────


def _empty_class_counts() -> dict[str, int]:
    return {name: 0 for name in RULING_CLASSES}


def weekly_ruling_counts(
    rulings: Iterable[Ruling],
    *,
    weeks: int = DEFAULT_WEEKS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Bucket rulings into the last ``weeks`` local ISO weeks, oldest first.

    Every requested week is emitted, including weeks with no rulings: a gap in
    the record is a fact about the record, and dropping empty weeks would make
    a quiet fortnight look like a dense one.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start_days = recent_week_start_dates(weeks, now)
    buckets: dict[date, dict[str, Any]] = {}
    for start_day in start_days:
        start, end = week_bounds(start_day)
        buckets[start_day] = {
            "week": week_key(start_day),
            "week_start": start.isoformat(),
            "week_end": end.isoformat(),
            # The newest week is still being ruled in; say so rather than
            # letting a Monday-morning reading look like a collapse.
            "partial": start <= now < end,
            "rulings_total": 0,
            "rulings_by_class": _empty_class_counts(),
            "rulings_by_grade": {},
        }
    window_start = week_bounds(start_days[0])[0]
    window_end = week_bounds(start_days[-1])[1]
    for ruling in rulings:
        moment = ruling.ruled_at.astimezone(timezone.utc)
        if moment < window_start or moment >= window_end:
            continue
        bucket = buckets[local_week_start_date(moment)]
        bucket["rulings_total"] += 1
        bucket["rulings_by_class"][ruling.ruling_class] += 1
        grade = ruling.grade or "ungraded"
        bucket["rulings_by_grade"][grade] = bucket["rulings_by_grade"].get(grade, 0) + 1
    return [buckets[start_day] for start_day in start_days]


def weekly_list_price(
    usage_rows: Iterable[Mapping[str, Any]],
    start_days: Sequence[date],
) -> dict[date, dict[str, Any]]:
    """List-price estimate plus token totals per local ISO week.

    Reuses :mod:`list_pricing`'s bucketing verbatim — the same (provider,
    model, per-request context basis) grouping the dashboard's window and
    daily estimates use — so the weekly dollars cannot drift from the daily
    ones through a second copy of the arithmetic.
    """
    if not start_days:
        return {}
    index_by_day = {start_day: position for position, start_day in enumerate(start_days)}
    grouped: list[dict[tuple[str, str, str], dict[str, Any]]] = [{} for _ in start_days]
    window_start = week_bounds(start_days[0])[0]
    window_end = week_bounds(start_days[-1])[1]
    for row in usage_rows:
        raw = row.get("occurred_at") or row.get("recorded_at")
        try:
            moment = parse_timestamp(raw)
        except RulingParseError:
            continue
        moment = moment.astimezone(timezone.utc)
        if moment < window_start or moment >= window_end:
            continue
        position = index_by_day.get(local_week_start_date(moment))
        if position is None:
            continue
        accumulate_price_bucket(grouped[position], row)
    result: dict[date, dict[str, Any]] = {}
    for start_day, buckets in zip(start_days, grouped):
        finalized = finalize_price_buckets(buckets)
        estimate = estimate_list_price(finalized)
        result[start_day] = {
            "estimate": estimate,
            "output_tokens": sum(int(bucket["output_tokens"]) for bucket in finalized),
            "total_tokens": sum(int(bucket["total_tokens"]) for bucket in finalized),
            "events_priced_buckets": len(finalized),
        }
    return result


def rulings_per_1k_usd(rulings_total: int, list_price_usd: str) -> float | None:
    """Rulings per $1,000 of estimated inference, or ``None`` when undefined.

    A week with $0.00 of estimated spend has no ratio — not an infinite one
    and not a zero. Returning ``None`` keeps "no denominator" distinguishable
    from "no rulings", which matters most in exactly the weeks where the
    ledger is new and thin.
    """
    price = Decimal(list_price_usd)
    if price <= 0:
        return None
    ratio = (Decimal(rulings_total) * Decimal(1000)) / price
    return float(ratio.quantize(Decimal("0.0001")))


def build_rulings_per_dollar(
    scan: RulingScan,
    usage_rows: Iterable[Mapping[str, Any]] | None = None,
    *,
    weeks: int = DEFAULT_WEEKS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Join weekly ruling counts to the weekly list-price estimate.

    ``usage_rows`` is the canonical ``usage_event_v1`` slice covering the
    weeks asked for; ``None`` means no usage ledger was available, which
    yields counts with null prices rather than a zero-dollar denominator that
    would read as free judgment.
    """
    generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start_days = recent_week_start_dates(weeks, generated_at)
    counts = weekly_ruling_counts(scan.rulings, weeks=weeks, now=generated_at)
    priced = weekly_list_price(usage_rows, start_days) if usage_rows is not None else None
    series: list[dict[str, Any]] = []
    for start_day, bucket in zip(start_days, counts):
        entry = dict(bucket)
        if priced is None:
            entry.update(
                {
                    "list_price_usd": None,
                    "rulings_per_1k_usd": None,
                    "output_tokens": None,
                    "total_tokens": None,
                    "unpriced_token_share": None,
                    "rate_basis": RATE_BASIS,
                }
            )
        else:
            week_price = priced[start_day]
            estimate = week_price["estimate"]
            entry.update(
                {
                    "list_price_usd": estimate["estimate_usd"],
                    "rulings_per_1k_usd": rulings_per_1k_usd(
                        entry["rulings_total"], estimate["estimate_usd"]
                    ),
                    "output_tokens": week_price["output_tokens"],
                    "total_tokens": week_price["total_tokens"],
                    "unpriced_token_share": round(1.0 - estimate["priced_token_share"], 6),
                    "unpriced_providers": estimate["unpriced_providers"],
                    "rate_basis": estimate["rate_basis"],
                }
            )
        series.append(entry)
    return {
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "timezone": RULINGS_TIMEZONE_NAME,
        "weeks": weeks,
        "series": series,
        "coverage": {
            "source": scan.source,
            "atrium_root": scan.atrium_root,
            "ledgers_scanned": scan.ledgers_scanned,
            "entries_parsed": scan.entries_parsed,
            "parse_errors": scan.parse_error_count,
            "parse_error_details": [
                dict(detail) for detail in scan.parse_errors[:_MAX_PARSE_ERROR_DETAILS]
            ],
            "duplicate_uids": scan.duplicate_uids,
            "scan_truncated": scan.truncated,
            "usage_ledger_available": usage_rows is not None,
            "rate_basis": RATE_BASIS,
            # Two standing caveats, carried in the payload so no consumer can
            # read the ratio as either billed cost or keeper activity.
            "price_is_estimate": (
                "List price is an ESTIMATE at the standard service tier over "
                "canonical token aggregates; subscription traffic bills $0 actual."
            ),
            "counts_recorded_rulings_only": (
                "Counts ratifications recorded in the ruling ledger, not keeper "
                "adjudication traffic; canon captured roughly 2% of that traffic "
                "before this ledger existed (backfill scheduled ~2026-09-18)."
            ),
        },
    }


def build_weekly_rulings(
    scan: RulingScan,
    *,
    weeks: int = DEFAULT_WEEKS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Weekly ruling counts alone — the same buckets without the price join."""
    payload = build_rulings_per_dollar(scan, None, weeks=weeks, now=now)
    for entry in payload["series"]:
        for field in (
            "list_price_usd",
            "rulings_per_1k_usd",
            "output_tokens",
            "total_tokens",
            "unpriced_token_share",
            "rate_basis",
        ):
            entry.pop(field, None)
    payload["coverage"].pop("usage_ledger_available", None)
    return payload


# ── Usage rows for the join (CLI path) ─────────────────────────────────────

_SQLITE_SUFFIXES = frozenset({".sqlite3", ".sqlite", ".db"})
# The canonical SQLite reader caps a single query at 100_001 rows; ask for one
# under it so a full week window reads in one snapshot, and let a ledger that
# genuinely exceeds it raise rather than silently truncating the denominator.
_USAGE_ROW_LIMIT = 100_000


def read_usage_rows(
    ledger_path: str | Path,
    *,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Read canonical ``usage_event_v1`` rows covering ``[start, end)``.

    The dashboard has its own bounded reader with the private-API row caps;
    this one exists for the CLI, which reads the same type-bound ledgers
    directly. Both dispatch on the explicit suffix — a JSONL ledger stays a
    JSONL ledger and is never guessed at.
    """
    from .canonical_ledger import query_sqlite_facts, read_facts

    path = Path(ledger_path).expanduser()
    suffix = path.suffix.casefold()
    if suffix in _SQLITE_SUFFIXES:
        return query_sqlite_facts(
            path,
            fact_type="usage_event_v1",
            occurred_or_observed_at_gte=start.astimezone(timezone.utc).isoformat(),
            occurred_or_observed_at_lt=end.astimezone(timezone.utc).isoformat(),
            order="asc",
            limit=_USAGE_ROW_LIMIT,
            contract_validation=False,
        )
    if suffix == ".jsonl":
        rows = []
        for row in read_facts(path):
            if row.get("fact_type") != "usage_event_v1":
                continue
            try:
                moment = parse_timestamp(row.get("occurred_at") or row.get("recorded_at"))
            except RulingParseError:
                continue
            if start <= moment < end:
                rows.append(row)
        return rows
    raise ValueError(f"unsupported usage ledger suffix: {suffix or '<none>'}")


def usage_window(weeks: int, now: datetime | None = None) -> tuple[datetime, datetime]:
    """The absolute instants the requested weeks span, for a ledger read."""
    start_days = recent_week_start_dates(weeks, now)
    return week_bounds(start_days[0])[0], week_bounds(start_days[-1])[1]


__all__ = [
    "DEFAULT_SERVICE_URL",
    "DEFAULT_WEEKS",
    "EXCLUDED_DIRECTORY_NAMES",
    "MAX_WEEKS",
    "RULINGS_TIMEZONE",
    "RULINGS_TIMEZONE_NAME",
    "RULING_CLASSES",
    "RULING_SOURCES",
    "Ruling",
    "RulingParseError",
    "RulingScan",
    "RulingsSourceUnavailable",
    "build_rulings_per_dollar",
    "build_weekly_rulings",
    "fetch_service_rulings",
    "iter_ruling_blocks",
    "iter_ruling_ledger_paths",
    "load_rulings",
    "local_week_start_date",
    "parse_ruling_yaml",
    "parse_timestamp",
    "read_ruling_ledger",
    "read_usage_rows",
    "ruling_as_dict",
    "ruling_from_mapping",
    "rulings_per_1k_usd",
    "scan_canon_rulings",
    "usage_window",
    "week_bounds",
    "week_key",
    "weekly_list_price",
    "weekly_ruling_counts",
    "recent_week_start_dates",
]
