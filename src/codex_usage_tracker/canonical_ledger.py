"""Canonical private usage/quota JSONL validation and idempotent append."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from typing import Any, Callable, Iterable

MAX_SAFE_INTEGER = 2**53 - 1
_RFC3339_RE = re.compile(
    r"^(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})T"
    r"(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
    r"(?P<fraction>\.[0-9]+)?(?P<zone>Z|[+-][0-9]{2}:[0-9]{2})$",
    re.ASCII,
)
_DECIMAL_RE = re.compile(r"^[+-]?[0-9]+(?:\.[0-9]+)?$", re.ASCII)
# ISO 4217 has a deliberately bounded vocabulary.  This immutable set covers
# active tender currencies plus common historic codes encountered in invoices.
ISO_4217_CODES = frozenset(
    "AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND "
    "BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF CHE CHF CHW CLF CLP CNY COP COU "
    "CRC CUC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS "
    "GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY "
    "KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA "
    "MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD "
    "OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD "
    "SHP SLE SLL SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS "
    "UAH UGX USD USN UYI UYU UYW UZS VED VES VND VUV WST XAF XAG XAU XBA XBB XBC "
    "XBD XCD XDR XOF XPD XPF XPT XSU XTS XUA XXX YER ZAR ZMW ZWL "
    "ATS BEF CYP DEM EEK ESP FIM FRF GRD IEP ITL LTL LVL MTL NLG PTE SKK ZWD"
    .split()
)
SECRET_KEYS = {
    "api_key",
    "x_api_key",
    "access_token",
    "refresh_token",
    "authorization",
    "proxy_authorization",
    "bearer",
    "token",
    "secret",
    "password",
    "client_secret",
    "cookie",
    "set_cookie",
    "credentials",
}
COMPACT_SECRET_KEYS = {key.replace("_", "") for key in SECRET_KEYS}
SENSITIVE_KEY_PARTS = frozenset(
    {"secret", "secrets", "password", "passwords", "passwd", "credential", "credentials", "bearer", "cookie", "cookies"}
)
SENSITIVE_KEY_PREFIXES = frozenset({"api", "access", "secret", "private", "client", "auth", "oauth"})
TOKEN_FIELDS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens")
USAGE_OPTIONAL_DEFAULTS: dict[str, Any] = {
    "event_uid": None, "surface": None, "session_id": None, "logical_call_id": None,
    "attempt_no": None, "provider_request_id": None, "provider": None, "upstream_provider": None,
    "model_requested": None, "model_reported": None, "api_mode": None, "billing_mode": None,
    "request_status": None, "error_class": None, "latency_ms": None,
    "input_tokens": None, "cache_read_tokens": None, "cache_write_tokens": None,
    "output_tokens": None, "reasoning_tokens": None, "missing_fields": [], "attribution_gaps": [],
    "estimated_cost_usd": None, "actual_cost_usd": None, "cost_source": None,
    "pricing_version": None, "reconstructed_call_count": None,
    "corrects_source_namespace": None, "corrects_source_event_id": None,
}
QUOTA_OPTIONAL_DEFAULTS: dict[str, Any] = {
    "account_ref": None, "window_started_at": None, "window_ends_at": None, "resets_at": None,
    "limit_value": None, "remaining_value": None, "used_value": None, "provider_payload_ref": None,
}
BILLING_OPTIONAL_DEFAULTS: dict[str, Any] = {
    "account_ref": None,
    "billing_period_start": None,
    "billing_period_end": None,
    "invoice_id": None,
    "line_item_id": None,
    "usage_event_refs": [],
    "description_code": None,
    "provider_receipt_id": None,
}
USAGE_REQUIRED = ("source_namespace", "source_event_id", "harness", "purpose", "record_kind", "occurred_at", "recorded_at", "usage_source", "usage_completeness", "measurement_confidence", "cost_status")
QUOTA_REQUIRED = ("source_namespace", "source_observation_id", "harness", "observed_at", "provider", "quota_name", "quota_scope", "window_kind", "unit", "measurement_confidence")
BILLING_REQUIRED = (
    "source_namespace", "source_billing_fact_id", "provider", "occurred_at",
    "transaction_kind", "status", "amount", "currency",
)
SUPPORTED_FACT_TYPES = frozenset({"usage_event_v1", "quota_observation_v1", "billing_fact_v1"})

class ValidationError(ValueError): pass
class IdentityConflictError(ValueError): pass
class MalformedLedgerError(ValueError): pass

@dataclass(frozen=True)
class AppendResult:
    discovered: int
    appended: int
    replayed: int


@dataclass(frozen=True)
class ConflictSummary:
    """Safe diagnostic for one rejected incoming canonical identity."""

    identity_sha256: str
    existing_sha256: str
    incoming_sha256: str
    changed_fields: tuple[str, ...]
    resolution: str = "quarantined"


def _reject_secrets(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            text = str(key).casefold()
            normalized_key = "".join("_" if character in "-_ " else character for character in text)
            candidate = normalized_key
            while candidate.startswith("x_"):
                candidate = candidate[2:]
            variants = {normalized_key, candidate}
            variants.update(item[:-1] for item in tuple(variants) if item.endswith("s"))
            compact_variants = {item.replace("_", "") for item in variants}
            parts = {part for item in variants for part in item.split("_") if part}
            credential_key_suffix = any(
                tokens[-1] in {"key", "keys"} and tokens[0] in SENSITIVE_KEY_PREFIXES
                for item in variants
                if (tokens := [part for part in item.split("_") if part])
            )
            credential_token_suffix = any(
                tokens[-1] in {"token", "tokens"} and tokens[0] in SENSITIVE_KEY_PREFIXES
                for item in variants
                if (tokens := [part for part in item.split("_") if part])
            )
            if (
                variants.intersection(SECRET_KEYS)
                or compact_variants.intersection(COMPACT_SECRET_KEYS)
                or parts.intersection(SENSITIVE_KEY_PARTS)
                or credential_key_suffix
                or credential_token_suffix
            ):
                raise ValidationError(f"credential-bearing field {key!r} at {path}")
            _reject_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secrets(child, f"{path}[{index}]")


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be an RFC 3339 timestamp")
    match = _RFC3339_RE.fullmatch(value)
    if match is None:
        raise ValidationError(f"{field} must be an RFC 3339 timestamp")
    hour, minute, second = (int(match[name]) for name in ("hour", "minute", "second"))
    zone = match["zone"]
    try:
        base = datetime.fromisoformat(f"{match['date']}T{hour:02d}:{minute:02d}:{second:02d}")
        if zone == "Z":
            offset = timedelta(0)
        else:
            zone_hour, zone_minute = (int(part) for part in zone[1:].split(":"))
            if zone_hour > 23 or zone_minute > 59:
                raise ValueError("invalid RFC 3339 offset")
            offset = timedelta(hours=zone_hour, minutes=zone_minute)
            if zone[0] == "-":
                offset = -offset
        utc = base - offset
    except ValueError as exc:
        raise ValidationError(f"{field} must be an RFC 3339 timestamp") from exc
    fraction = (match["fraction"] or "")[1:].rstrip("0")
    return utc.strftime("%Y-%m-%dT%H:%M:%S") + (f".{fraction}" if fraction else "") + "Z"


def canonical_timestamp(value: Any) -> str:
    if isinstance(value, (int, float)):
        return _timestamp(datetime.fromtimestamp(float(value), timezone.utc).isoformat(), "timestamp")
    return _timestamp(value, "timestamp")


def decimal_string(value: Any, field: str = "decimal", *, allow_negative: bool = False) -> str | None:
    if value is None: return None
    if isinstance(value, bool): raise ValidationError(f"{field} must be a decimal string")
    if isinstance(value, str):
        text = value
        if _DECIMAL_RE.fullmatch(text) is None:
            raise ValidationError(f"{field} must be a plain ASCII decimal string")
    elif isinstance(value, (int, Decimal, float)):
        # Trusted adapters use this utility before constructing contract strings.
        text = str(value)
    else:
        raise ValidationError(f"{field} must be a decimal string")
    try: number = Decimal(text)
    except (InvalidOperation, ValueError) as exc: raise ValidationError(f"{field} must be a decimal string") from exc
    if not number.is_finite() or (number < 0 and not allow_negative): raise ValidationError(f"{field} must be nonnegative")
    if number == 0: return "0"
    rendered = format(number, "f")
    if "." in rendered: rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _nonempty(row: dict[str, Any], fields: Iterable[str]) -> None:
    for field in fields:
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise ValidationError(f"{field} must be a non-empty string")


def _optional_strings(row: dict[str, Any], fields: Iterable[str]) -> None:
    for field in fields:
        value = row.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValidationError(f"{field} must be a non-empty string or null")


def _string_or_null(row: dict[str, Any], fields: Iterable[str]) -> None:
    for field in fields:
        if row.get(field) is not None and not isinstance(row[field], str):
            raise ValidationError(f"{field} must be a string or null")


def _json_safe(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValidationError(f"unpaired surrogate at {path}")
        return
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise ValidationError(f"integer outside the I-JSON safe range at {path}")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError(f"non-finite number at {path}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _json_safe(child, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValidationError(f"object key at {path} must be a string")
            if any(0xD800 <= ord(character) <= 0xDFFF for character in key):
                raise ValidationError(f"unpaired surrogate in object key at {path}")
            _json_safe(child, f"{path}.{key}")
        return
    raise ValidationError(f"unsupported JSON value at {path}")


def _int(value: Any, field: str, *, signed: bool = False) -> int | None:
    if value is None: return None
    if isinstance(value, bool) or not isinstance(value, int): raise ValidationError(f"{field} must be an integer or null")
    minimum = -MAX_SAFE_INTEGER if signed else 0
    if value < minimum or value > MAX_SAFE_INTEGER: raise ValidationError(f"{field} is out of range")
    return value


def normalize_fact(fact: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(fact, dict): raise ValidationError("fact must be an object")
    _reject_secrets(fact)
    row = dict(fact)
    if isinstance(row.get("schema_version"), bool) or not isinstance(row.get("schema_version"), int) or row["schema_version"] != 1: raise ValidationError("schema_version must equal integer 1")
    kind = row.get("fact_type")
    if kind == "usage_event_v1":
        _nonempty(row, ("fact_type", *USAGE_REQUIRED))
        for field, default in USAGE_OPTIONAL_DEFAULTS.items(): row.setdefault(field, list(default) if isinstance(default, list) else default)
        _optional_strings(row, (
            "event_uid", "surface", "session_id", "logical_call_id", "provider_request_id", "provider",
            "upstream_provider", "model_requested", "model_reported", "api_mode", "billing_mode",
            "request_status", "error_class", "cost_source", "pricing_version", "corrects_source_namespace",
            "corrects_source_event_id",
        ))
        if row["purpose"] not in {"main","background_review","compression","vision","subagent","historical_backfill","other"} and not row["purpose"].startswith("x_"): raise ValidationError("unsupported purpose")
        if row["record_kind"] not in {"api_attempt","historical_aggregate","correction"}: raise ValidationError("unsupported record_kind")
        if row["usage_source"] not in {"provider_reported","harness_counted","reconstructed","estimated","unknown"}: raise ValidationError("unsupported usage_source")
        if row["usage_completeness"] not in {"complete","partial","unknown"}: raise ValidationError("unsupported usage_completeness")
        if row["measurement_confidence"] not in {"exact","reconstructed","estimated","unknown"}: raise ValidationError("unsupported measurement_confidence")
        if row["cost_status"] not in {"actual","estimated","included","unknown"}: raise ValidationError("unsupported cost_status")
        if row["request_status"] is not None and row["request_status"] not in {"ok", "error", "timeout", "cancelled", "unknown"}:
            raise ValidationError("unsupported request_status")
        row["occurred_at"] = _timestamp(row["occurred_at"], "occurred_at")
        row["recorded_at"] = _timestamp(row["recorded_at"], "recorded_at")
        signed = row["record_kind"] == "correction"
        for field in TOKEN_FIELDS: row[field] = _int(row[field], field, signed=signed)
        row["attempt_no"] = _int(row["attempt_no"], "attempt_no")
        if row["attempt_no"] == 0: raise ValidationError("attempt_no must be positive")
        row["latency_ms"] = _int(row["latency_ms"], "latency_ms")
        if row["record_kind"] in {"historical_aggregate", "correction"} and row["latency_ms"] is not None:
            raise ValidationError("latency_ms must be null for aggregates and corrections")
        row["reconstructed_call_count"] = _int(row["reconstructed_call_count"], "reconstructed_call_count")
        allowed_gap_values = {
            "missing_fields": set(TOKEN_FIELDS),
            "attribution_gaps": {"provider", "upstream_provider", "model_requested", "model_reported"},
        }
        for field in ("missing_fields", "attribution_gaps"):
            value=row[field]
            if not isinstance(value,list) or not all(isinstance(x,str) for x in value) or len(value)!=len(set(value)): raise ValidationError(f"{field} must contain unique strings")
            if not set(value) <= allowed_gap_values[field]:
                raise ValidationError(f"{field} contains unsupported canonical field names")
            row[field]=sorted(value)
        _string_or_null(row, ("estimated_cost_usd", "actual_cost_usd"))
        for field in ("estimated_cost_usd","actual_cost_usd"): row[field]=decimal_string(row[field],field,allow_negative=signed)
        if row["cost_status"] == "included":
            if row["estimated_cost_usd"] is not None or row["actual_cost_usd"] is not None: raise ValidationError("included usage must have null cost fields")
        if (row["corrects_source_namespace"] is None) != (row["corrects_source_event_id"] is None): raise ValidationError("correction pointers must be paired")
        if row["record_kind"] != "correction" and row["corrects_source_namespace"] is not None:
            raise ValidationError("correction pointers are only valid on corrections")
    elif kind == "quota_observation_v1":
        _nonempty(row, ("fact_type", *QUOTA_REQUIRED))
        for field, default in QUOTA_OPTIONAL_DEFAULTS.items(): row.setdefault(field, default)
        _optional_strings(row, ("account_ref", "provider_payload_ref"))
        row["observed_at"]=_timestamp(row["observed_at"],"observed_at")
        for field in ("window_started_at","window_ends_at","resets_at"):
            if row[field] is not None: row[field]=_timestamp(row[field],field)
        _string_or_null(row, ("limit_value", "remaining_value", "used_value"))
        for field in ("limit_value","remaining_value","used_value"): row[field]=decimal_string(row[field],field)
        if row["quota_scope"] not in {"account","organization","model","endpoint","unknown"}: raise ValidationError("unsupported quota_scope")
        if row["window_kind"] not in {"fixed","rolling","lifetime","unknown"}: raise ValidationError("unsupported window_kind")
        if row["unit"] not in {"requests","tokens","usd","credits","percent","provider_unit"}: raise ValidationError("unsupported unit")
        if row["measurement_confidence"] not in {"exact","estimated","unknown"}: raise ValidationError("unsupported measurement_confidence")
    elif kind == "billing_fact_v1":
        _nonempty(row, ("fact_type", *BILLING_REQUIRED))
        for field, default in BILLING_OPTIONAL_DEFAULTS.items():
            row.setdefault(field, list(default) if isinstance(default, list) else default)
        _string_or_null(row, (
            "account_ref", "invoice_id", "line_item_id", "description_code", "provider_receipt_id",
        ))
        row["occurred_at"] = _timestamp(row["occurred_at"], "occurred_at")
        for field in ("billing_period_start", "billing_period_end"):
            value = row[field]
            if value is not None:
                row[field] = _timestamp(value, field)
        if row["transaction_kind"] not in {"charge", "credit", "refund", "adjustment", "tax", "payment"}:
            raise ValidationError("unsupported transaction_kind")
        if row["status"] not in {"pending", "posted", "void", "unknown"}:
            raise ValidationError("unsupported status")
        if row["currency"] not in ISO_4217_CODES:
            raise ValidationError("currency must be an uppercase three-letter ISO 4217 code")
        row["amount"] = decimal_string(row["amount"], "amount", allow_negative=True)
        assert row["amount"] is not None
        amount = Decimal(row["amount"])
        if amount != 0:
            if row["transaction_kind"] in {"charge", "tax"} and amount < 0:
                raise ValidationError("transaction_kind/amount sign mismatch")
            if row["transaction_kind"] in {"credit", "refund", "payment"} and amount > 0:
                raise ValidationError("transaction_kind/amount sign mismatch")
        refs = row["usage_event_refs"]
        if not isinstance(refs, list):
            raise ValidationError("usage_event_refs must be an array")
        normalized_refs: list[dict[str, Any]] = []
        identities: set[tuple[str, str]] = set()
        for index, ref in enumerate(refs):
            if not isinstance(ref, dict):
                raise ValidationError(f"usage_event_refs[{index}] must be an object")
            _nonempty(ref, ("source_namespace", "source_event_id"))
            for key in ref:
                if key not in {"source_namespace", "source_event_id"} and not key.startswith("x_"):
                    raise ValidationError(f"unknown unnamespaced field {key} in usage_event_refs[{index}]")
            identity = (ref["source_namespace"], ref["source_event_id"])
            if identity in identities:
                raise ValidationError("usage_event_refs must contain unique normative identities")
            identities.add(identity)
            normalized_refs.append(dict(ref))
        row["usage_event_refs"] = sorted(
            normalized_refs, key=lambda ref: (ref["source_namespace"], ref["source_event_id"])
        )
    else: raise ValidationError("unsupported fact_type")
    for key in row:
        allowed = (
            {"fact_type", "schema_version", *USAGE_REQUIRED, *USAGE_OPTIONAL_DEFAULTS}
            if kind == "usage_event_v1"
            else {"fact_type", "schema_version", *QUOTA_REQUIRED, *QUOTA_OPTIONAL_DEFAULTS}
            if kind == "quota_observation_v1"
            else {"fact_type", "schema_version", *BILLING_REQUIRED, *BILLING_OPTIONAL_DEFAULTS}
        )
        if key not in allowed and not key.startswith("x_"):
            raise ValidationError(f"unknown unnamespaced field {key}")
    _json_safe(row)
    return row


def _jcs_string(value: str) -> str:
    pieces = ['"']
    short_escapes = {8: "\\b", 9: "\\t", 10: "\\n", 12: "\\f", 13: "\\r"}
    for character in value:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise ValidationError("JCS strings must not contain unpaired surrogates")
        if character == '"':
            pieces.append('\\"')
        elif character == "\\":
            pieces.append("\\\\")
        elif codepoint in short_escapes:
            pieces.append(short_escapes[codepoint])
        elif codepoint < 0x20:
            pieces.append(f"\\u{codepoint:04x}")
        else:
            pieces.append(character)
    pieces.append('"')
    return "".join(pieces)


def _jcs_number(value: int | float) -> str:
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise ValidationError("integer is outside the I-JSON safe range")
        return str(value)
    if not math.isfinite(value):
        raise ValidationError("JCS numbers must be finite")
    if value == 0:
        return "0"
    negative = value < 0
    absolute = -value if negative else value
    rendered = repr(absolute).lower()
    # Python and ECMAScript use the same shortest-round-trip binary64 digits,
    # but choose fixed/exponential notation at different thresholds.
    if 1e-6 <= absolute < 1e21:
        rendered = format(Decimal(rendered), "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
    else:
        if "e" not in rendered:
            decimal = Decimal(rendered)
            rendered = format(decimal.normalize(), "e")
        mantissa, exponent_text = rendered.split("e", 1)
        exponent = int(exponent_text)
        rendered = f"{mantissa}e{'+' if exponent >= 0 else ''}{exponent}"
    return ("-" if negative else "") + rendered


def _jcs_dumps(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _jcs_string(value)
    if isinstance(value, (int, float)):
        return _jcs_number(value)
    if isinstance(value, list):
        return "[" + ",".join(_jcs_dumps(child) for child in value) + "]"
    if isinstance(value, dict):
        try:
            keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
        except (AttributeError, UnicodeEncodeError) as exc:
            raise ValidationError("JCS object keys must be valid Unicode strings") from exc
        return "{" + ",".join(
            f"{_jcs_string(key)}:{_jcs_dumps(value[key])}" for key in keys
        ) + "}"
    raise ValidationError("unsupported JCS value")


def canonical_json(fact: dict[str, Any]) -> str:
    return _jcs_dumps(normalize_fact(fact))


def _identity(row: dict[str, Any]) -> tuple[str, str]:
    key = {
        "usage_event_v1": "source_event_id",
        "quota_observation_v1": "source_observation_id",
        "billing_fact_v1": "source_billing_fact_id",
    }[row["fact_type"]]
    return row["source_namespace"], row[key]


def _conflict_summary(
    identity: tuple[str, str],
    existing_payload: str,
    incoming_payload: str,
    *,
    resolution: str = "quarantined",
) -> ConflictSummary:
    existing = json.loads(existing_payload)
    incoming = json.loads(incoming_payload)
    changed_fields = tuple(
        sorted(
            key
            for key in set(existing) | set(incoming)
            if existing.get(key) != incoming.get(key)
        )
    )
    identity_payload = _jcs_dumps([identity[0], identity[1]])
    return ConflictSummary(
        identity_sha256=hashlib.sha256(identity_payload.encode("utf-8")).hexdigest(),
        existing_sha256=hashlib.sha256(existing_payload.encode("utf-8")).hexdigest(),
        incoming_sha256=hashlib.sha256(incoming_payload.encode("utf-8")).hexdigest(),
        changed_fields=changed_fields,
        resolution=resolution,
    )


def _read_stream(fp: Any, display: str) -> list[dict[str, Any]]:
    # Parse the complete physical stream first. A corrupt JSON line is the most
    # actionable integrity error even if an earlier object also fails schema.
    fp.seek(0); raw_rows=[]
    try:
        for number,line in enumerate(fp,1):
            if not line.strip(): continue
            try: raw=json.loads(line)
            except json.JSONDecodeError as exc: raise MalformedLedgerError(f"{display}:{number}: malformed JSON: {exc.msg}") from exc
            raw_rows.append((number, raw))
    except UnicodeDecodeError as exc:
        raise MalformedLedgerError(f"{display}: ledger is not valid UTF-8") from exc
    rows=[]
    for number, raw in raw_rows:
        try: rows.append(normalize_fact(raw))
        except ValidationError as exc: raise MalformedLedgerError(f"{display}:{number}: {exc}") from exc
    return rows


def read_facts(path: str | Path) -> list[dict[str, Any]]:
    target=Path(path).expanduser()
    if not target.exists(): return []
    with _ledger_lock(target, fcntl.LOCK_SH):
        if not target.exists(): return []
        os.chmod(target, 0o600)
        with target.open("r",encoding="utf-8") as fp: return _read_stream(fp,str(target))


def _read_facts_without_side_effects(path: Path) -> list[dict[str, Any]]:
    """Read a stable JSONL snapshot without locks, chmod, or atime updates."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NOATIME", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MalformedLedgerError(f"cannot safely read JSONL source: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise MalformedLedgerError(f"JSONL source is not a regular file: {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            rows = _read_stream(stream, str(path))
            after = os.fstat(stream.fileno())
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise MalformedLedgerError("JSONL source changed during side-effect-free read")
        return rows
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _lock_path(target: Path) -> Path:
    return target.with_name(f"{target.name}.lock")


@contextmanager
def _ledger_lock(target: Path, operation: int):
    lock_path = _lock_path(target)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise MalformedLedgerError(f"cannot safely open ledger lock: {lock_path}") from exc
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
        ):
            raise MalformedLedgerError(f"unsafe ledger lock file: {lock_path}")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, operation)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _atomic_write(target: Path, rows: Iterable[dict[str, Any]]) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fp:
            fd = -1
            for row in rows:
                fp.write(canonical_json(row) + "\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def append_facts(path: str | Path, facts: Iterable[dict[str, Any]], *, dry_run: bool=False) -> AppendResult:
    incoming=[normalize_fact(f) for f in facts]
    target=Path(path).expanduser()
    if dry_run:
        existing = _read_facts_without_side_effects(target) if target.exists() else []
        result, _ = _compare(existing,incoming)
        return result
    target.parent.mkdir(parents=True,exist_ok=True)
    with _ledger_lock(target, fcntl.LOCK_EX):
        if target.exists():
            os.chmod(target, 0o600)
            with target.open("r", encoding="utf-8") as fp:
                existing = _read_stream(fp, str(target))
        else:
            existing = []
        result, pending = _compare(existing,incoming)
        if pending or not target.exists():
            _atomic_write(target, [*existing, *pending])
        return result


def append_facts_quarantined(
    path: str | Path,
    facts: Iterable[dict[str, Any]],
    *,
    dry_run: bool = False,
    equivalent_changed_fields: frozenset[str] = frozenset(),
) -> tuple[AppendResult, tuple[ConflictSummary, ...]]:
    """Append valid JSONL facts while returning changed incoming identities."""
    incoming = [normalize_fact(fact) for fact in facts]
    target = Path(path).expanduser()
    if dry_run:
        existing = _read_facts_without_side_effects(target) if target.exists() else []
        result, _pending, conflicts = _compare_quarantined(
            existing,
            incoming,
            equivalent_changed_fields=equivalent_changed_fields,
        )
        return result, tuple(conflicts)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _ledger_lock(target, fcntl.LOCK_EX):
        if target.exists():
            os.chmod(target, 0o600)
            with target.open("r", encoding="utf-8") as fp:
                existing = _read_stream(fp, str(target))
        else:
            existing = []
        result, pending, conflicts = _compare_quarantined(
            existing,
            incoming,
            equivalent_changed_fields=equivalent_changed_fields,
        )
        if pending or not target.exists():
            _atomic_write(target, [*existing, *pending])
        return result, tuple(conflicts)


def append_facts_quarantined_reconciled(
    path: str | Path,
    facts: Iterable[dict[str, Any]],
    *,
    reconcile: Callable[
        [list[dict[str, Any]], list[dict[str, Any]]],
        tuple[list[dict[str, Any]], Any],
    ],
    dry_run: bool = False,
    equivalent_changed_fields: frozenset[str] = frozenset(),
) -> tuple[AppendResult, tuple[ConflictSummary, ...], Any]:
    """Reconcile against a locked JSONL snapshot, then append atomically.

    The callback runs while the canonical writer lock is held so no standard
    writer can insert a correction between the semantic read and append.
    """
    incoming = [normalize_fact(fact) for fact in facts]
    target = Path(path).expanduser()

    def prepare(existing: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Any]:
        prepared, metadata = reconcile(existing, incoming)
        return [normalize_fact(fact) for fact in prepared], metadata

    if dry_run:
        existing = _read_facts_without_side_effects(target) if target.exists() else []
        prepared, metadata = prepare(existing)
        result, _pending, conflicts = _compare_quarantined(
            existing,
            prepared,
            equivalent_changed_fields=equivalent_changed_fields,
        )
        return result, tuple(conflicts), metadata

    target.parent.mkdir(parents=True, exist_ok=True)
    with _ledger_lock(target, fcntl.LOCK_EX):
        if target.exists():
            os.chmod(target, 0o600)
            with target.open("r", encoding="utf-8") as fp:
                existing = _read_stream(fp, str(target))
        else:
            existing = []
        prepared, metadata = prepare(existing)
        result, pending, conflicts = _compare_quarantined(
            existing,
            prepared,
            equivalent_changed_fields=equivalent_changed_fields,
        )
        if pending or not target.exists():
            _atomic_write(target, [*existing, *pending])
        return result, tuple(conflicts), metadata


def _compare_quarantined(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    *,
    equivalent_changed_fields: frozenset[str] = frozenset(),
) -> tuple[AppendResult, list[dict[str, Any]], list[ConflictSummary]]:
    seen: dict[tuple[str, str], str] = {}
    for row in existing:
        identity = _identity(row)
        payload = canonical_json(row)
        if identity in seen and seen[identity] != payload:
            raise IdentityConflictError(f"conflicting existing facts for {identity!r}")
        seen[identity] = payload

    grouped: dict[tuple[str, str], list[tuple[dict[str, Any], str]]] = {}
    for row in incoming:
        grouped.setdefault(_identity(row), []).append((row, canonical_json(row)))

    appended = replayed = 0
    pending: list[dict[str, Any]] = []
    conflicts: list[ConflictSummary] = []
    for identity, variants in grouped.items():
        distinct: dict[str, dict[str, Any]] = {}
        for row, payload in variants:
            distinct.setdefault(payload, row)
        existing_payload = seen.get(identity)

        if len(distinct) > 1:
            if existing_payload is not None:
                changed = [
                    _conflict_summary(identity, existing_payload, payload)
                    for payload in distinct
                    if payload != existing_payload
                ]
                if changed and all(
                    summary.changed_fields
                    and set(summary.changed_fields) <= equivalent_changed_fields
                    for summary in changed
                ):
                    replayed += len(variants)
                    conflicts.extend(
                        _conflict_summary(
                            identity,
                            existing_payload,
                            payload,
                            resolution="canonical_replay",
                        )
                        for payload in distinct
                        if payload != existing_payload
                    )
                    continue
                summary = next(
                    (
                        candidate
                        for candidate in changed
                        if not candidate.changed_fields
                        or not set(candidate.changed_fields) <= equivalent_changed_fields
                    ),
                    changed[0],
                )
            else:
                payloads = list(distinct)
                summary = _conflict_summary(identity, payloads[0], payloads[1])
            conflicts.append(summary)
            continue

        payload, row = next(iter((payload, row) for payload, row in distinct.items()))
        if existing_payload is None:
            seen[identity] = payload
            pending.append(row)
            appended += 1
            replayed += len(variants) - 1
            continue
        if existing_payload == payload:
            replayed += len(variants)
            continue
        summary = _conflict_summary(identity, existing_payload, payload)
        if summary.changed_fields and set(summary.changed_fields) <= equivalent_changed_fields:
            replayed += len(variants)
            conflicts.append(
                _conflict_summary(
                    identity,
                    existing_payload,
                    payload,
                    resolution="canonical_replay",
                )
            )
        else:
            conflicts.append(summary)
    return AppendResult(len(incoming), appended, replayed), pending, conflicts


def _compare(
    existing: list[dict[str,Any]], incoming: list[dict[str,Any]]
) -> tuple[AppendResult, list[dict[str, Any]]]:
    seen: dict[tuple[str, str], str] = {}
    for row in existing:
        identity = _identity(row)
        payload = canonical_json(row)
        if identity in seen and seen[identity] != payload:
            raise IdentityConflictError(f"conflicting existing facts for {identity!r}")
        seen[identity] = payload
    appended=replayed=0
    pending=[]
    for row in incoming:
        identity=_identity(row); payload=canonical_json(row)
        if identity in seen:
            if seen[identity] != payload: raise IdentityConflictError(f"conflicting replay for {identity!r}")
            replayed += 1
        else:
            seen[identity]=payload; pending.append(row); appended += 1
    return AppendResult(len(incoming),appended,replayed), pending


_SQLITE_SCHEMA_VERSION = "1"
_SQLITE_QUERY_LIMIT = 100_001
_SQLITE_QUERY_FILTER_COLUMNS = {
    "provider": "provider",
    "harness": "harness",
    "purpose": "purpose",
    "quota_name": "quota_name",
    "account_ref": "account_ref",
    "transaction_kind": "transaction_kind",
    "status": "transaction_status",
    "invoice_id": "invoice_id",
    "line_item_id": "line_item_id",
}
_SQLITE_QUERY_INDEXES = {
    "provider": "facts_provider_idx",
    "harness": "facts_harness_idx",
    "purpose": "facts_purpose_idx",
    "quota_name": "facts_quota_name_idx",
    "account_ref": "facts_account_ref_idx",
    "transaction_kind": "facts_transaction_idx",
    "status": "facts_status_idx",
    "invoice_id": "facts_invoice_line_idx",
    "line_item_id": "facts_line_item_idx",
}
_SQLITE_FACT_COLUMNS = (
    "ingestion_sequence", "ingested_at", "fact_type", "source_namespace", "source_identity",
    "canonical_json", "canonical_sha256", "occurred_or_observed_at", "provider", "harness",
    "purpose", "model", "quota_name", "account_ref", "transaction_kind", "transaction_status",
    "invoice_id", "line_item_id",
)
_SQLITE_TIMESTAMP_KEY_SQL = "rtrim(occurred_or_observed_at, 'Z')"
_SQLITE_TIMESTAMP_INDEX_SQL = (
    f"CREATE INDEX facts_timestamp_idx ON facts({_SQLITE_TIMESTAMP_KEY_SQL})"
)
_SQLITE_LEGACY_TIMESTAMP_INDEX_SQL = (
    "CREATE INDEX facts_timestamp_idx ON facts(occurred_or_observed_at)"
)
_SQLITE_INDEXES = {
    "facts_timestamp_idx": (False, ((None, False, "BINARY"),)),
    "facts_provider_idx": (False, (("provider", False, "BINARY"),)),
    "facts_harness_idx": (False, (("harness", False, "BINARY"),)),
    "facts_purpose_idx": (False, (("purpose", False, "BINARY"),)),
    "facts_model_idx": (False, (("model", False, "BINARY"),)),
    "facts_quota_name_idx": (False, (("quota_name", False, "BINARY"),)),
    "facts_account_ref_idx": (False, (("account_ref", False, "BINARY"),)),
    "facts_transaction_idx": (
        False,
        (("transaction_kind", False, "BINARY"), ("transaction_status", False, "BINARY")),
    ),
    "facts_status_idx": (False, (("transaction_status", False, "BINARY"),)),
    "facts_invoice_line_idx": (
        False,
        (("invoice_id", False, "BINARY"), ("line_item_id", False, "BINARY")),
    ),
    "facts_line_item_idx": (False, (("line_item_id", False, "BINARY"),)),
    "sqlite_autoindex_facts_1": (
        True,
        (("source_namespace", False, "BINARY"), ("source_identity", False, "BINARY")),
    ),
}
_SQLITE_TRIGGERS = {
    "facts_no_update": """CREATE TRIGGER facts_no_update BEFORE UPDATE ON facts BEGIN
        SELECT RAISE(ABORT, 'canonical facts are immutable'); END""",
    "facts_no_delete": """CREATE TRIGGER facts_no_delete BEFORE DELETE ON facts BEGIN
        SELECT RAISE(ABORT, 'canonical facts are immutable'); END""",
    "facts_fact_type_guard": """CREATE TRIGGER facts_fact_type_guard BEFORE INSERT ON facts
        WHEN NEW.fact_type != COALESCE((SELECT value FROM ledger_metadata WHERE key = 'fact_type'), '') BEGIN
        SELECT RAISE(ABORT, 'fact_type does not match ledger binding'); END""",
    "metadata_no_insert": """CREATE TRIGGER metadata_no_insert BEFORE INSERT ON ledger_metadata BEGIN
        SELECT RAISE(ABORT, 'ledger metadata is immutable'); END""",
    "metadata_no_update": """CREATE TRIGGER metadata_no_update BEFORE UPDATE ON ledger_metadata BEGIN
        SELECT RAISE(ABORT, 'ledger metadata is immutable'); END""",
    "metadata_no_delete": """CREATE TRIGGER metadata_no_delete BEFORE DELETE ON ledger_metadata BEGIN
        SELECT RAISE(ABORT, 'ledger metadata is immutable'); END""",
}
_SQLITE_FACTS_TABLE_SQL = """CREATE TABLE facts (
    ingestion_sequence INTEGER PRIMARY KEY AUTOINCREMENT, ingested_at TEXT NOT NULL,
    fact_type TEXT NOT NULL, source_namespace TEXT NOT NULL, source_identity TEXT NOT NULL,
    canonical_json TEXT NOT NULL, canonical_sha256 TEXT NOT NULL CHECK(length(canonical_sha256) = 64),
    occurred_or_observed_at TEXT, provider TEXT, harness TEXT, purpose TEXT, model TEXT,
    quota_name TEXT, account_ref TEXT, transaction_kind TEXT, transaction_status TEXT,
    invoice_id TEXT, line_item_id TEXT, UNIQUE(source_namespace, source_identity))"""
_SQLITE_METADATA_TABLE_SQL = """CREATE TABLE ledger_metadata (
    key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL) WITHOUT ROWID"""


def _check_fact_type(fact_type: str | None) -> None:
    if fact_type is not None and fact_type not in SUPPORTED_FACT_TYPES:
        raise ValidationError(f"unsupported fact_type {fact_type!r}")


def _homogeneous_fact_type(rows: list[dict[str, Any]], requested: str | None) -> str | None:
    _check_fact_type(requested)
    found = {row["fact_type"] for row in rows}
    if len(found) > 1:
        raise ValidationError("SQLite ledger batch must contain exactly one fact_type")
    if found and requested is not None and found != {requested}:
        raise ValidationError(f"fact_type does not match requested ledger type {requested!r}")
    return requested or (next(iter(found)) if found else None)


def _ensure_private_directory(path: Path) -> None:
    """Create only missing ancestors, privately, without changing existing directories."""
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:  # A concurrent creator won the race.
            pass


def _validate_regular_owned_file(path: Path, *, required: bool) -> os.stat_result | None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise MalformedLedgerError(f"required ledger artifact is missing: {path}")
        return None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.geteuid()
    ):
        raise MalformedLedgerError(f"unsafe ledger artifact: {path}")
    return metadata


def _validate_sqlite_artifacts(path: Path, *, database_required: bool = True) -> None:
    _validate_regular_owned_file(path, required=database_required)
    _validate_regular_owned_file(Path(f"{path}-wal"), required=False)
    _validate_regular_owned_file(Path(f"{path}-shm"), required=False)


def _secure_regular_file(path: Path) -> None:
    before = _validate_regular_owned_file(path, required=True)
    assert before is not None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MalformedLedgerError(f"cannot safely open ledger artifact: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise MalformedLedgerError(f"ledger artifact changed while opening: {path}")
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _secure_sqlite_files(path: Path) -> None:
    _secure_regular_file(path)
    for candidate in (Path(f"{path}-wal"), Path(f"{path}-shm")):
        if os.path.lexists(candidate):
            _secure_regular_file(candidate)


def _configure_sqlite(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
    if mode is None or str(mode[0]).casefold() != "wal":
        raise MalformedLedgerError("SQLite ledger could not enable WAL mode")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA busy_timeout = 5000")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise MalformedLedgerError("SQLite ledger could not enable foreign keys")
    if connection.execute("PRAGMA synchronous").fetchone()[0] < 2:
        raise MalformedLedgerError("SQLite ledger synchronous mode is not strong enough")


def _create_sqlite_schema(connection: sqlite3.Connection, fact_type: str) -> None:
    if fact_type not in SUPPORTED_FACT_TYPES:
        raise ValidationError("fact_type is required to create a SQLite ledger")
    # fact_type is selected from SUPPORTED_FACT_TYPES, so it is safe in this
    # initialization script. Keeping all DDL and binding metadata in one
    # transaction prevents a crash from leaving an apparently valid half-bound DB.
    connection.executescript(
        f"""
        BEGIN IMMEDIATE;
        CREATE TABLE ledger_metadata (
            key TEXT PRIMARY KEY NOT NULL,
            value TEXT NOT NULL
        ) WITHOUT ROWID;
        INSERT INTO ledger_metadata(key, value) VALUES
            ('schema_version', '{_SQLITE_SCHEMA_VERSION}'),
            ('fact_type', '{fact_type}');
        CREATE TABLE facts (
            ingestion_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            ingested_at TEXT NOT NULL,
            fact_type TEXT NOT NULL,
            source_namespace TEXT NOT NULL,
            source_identity TEXT NOT NULL,
            canonical_json TEXT NOT NULL,
            canonical_sha256 TEXT NOT NULL CHECK(length(canonical_sha256) = 64),
            occurred_or_observed_at TEXT,
            provider TEXT,
            harness TEXT,
            purpose TEXT,
            model TEXT,
            quota_name TEXT,
            account_ref TEXT,
            transaction_kind TEXT,
            transaction_status TEXT,
            invoice_id TEXT,
            line_item_id TEXT,
            UNIQUE(source_namespace, source_identity)
        );
        CREATE INDEX facts_timestamp_idx ON facts(rtrim(occurred_or_observed_at, 'Z'));
        CREATE INDEX facts_provider_idx ON facts(provider);
        CREATE INDEX facts_harness_idx ON facts(harness);
        CREATE INDEX facts_purpose_idx ON facts(purpose);
        CREATE INDEX facts_model_idx ON facts(model);
        CREATE INDEX facts_quota_name_idx ON facts(quota_name);
        CREATE INDEX facts_account_ref_idx ON facts(account_ref);
        CREATE INDEX facts_transaction_idx ON facts(transaction_kind, transaction_status);
        CREATE INDEX facts_status_idx ON facts(transaction_status);
        CREATE INDEX facts_invoice_line_idx ON facts(invoice_id, line_item_id);
        CREATE INDEX facts_line_item_idx ON facts(line_item_id);
        CREATE TRIGGER facts_no_update
        BEFORE UPDATE ON facts BEGIN
            SELECT RAISE(ABORT, 'canonical facts are immutable');
        END;
        CREATE TRIGGER facts_no_delete
        BEFORE DELETE ON facts BEGIN
            SELECT RAISE(ABORT, 'canonical facts are immutable');
        END;
        CREATE TRIGGER facts_fact_type_guard
        BEFORE INSERT ON facts
        WHEN NEW.fact_type != COALESCE(
            (SELECT value FROM ledger_metadata WHERE key = 'fact_type'), ''
        ) BEGIN
            SELECT RAISE(ABORT, 'fact_type does not match ledger binding');
        END;
        CREATE TRIGGER metadata_no_insert
        BEFORE INSERT ON ledger_metadata BEGIN
            SELECT RAISE(ABORT, 'ledger metadata is immutable');
        END;
        CREATE TRIGGER metadata_no_update
        BEFORE UPDATE ON ledger_metadata BEGIN
            SELECT RAISE(ABORT, 'ledger metadata is immutable');
        END;
        CREATE TRIGGER metadata_no_delete
        BEFORE DELETE ON ledger_metadata BEGIN
            SELECT RAISE(ABORT, 'ledger metadata is immutable');
        END;
        PRAGMA user_version = 1;
        COMMIT;
        """
    )


def _typed_values(row: dict[str, Any]) -> tuple[Any, ...]:
    kind = row["fact_type"]
    timestamp = row.get("observed_at") if kind == "quota_observation_v1" else row.get("occurred_at")
    model = row.get("model_reported") or row.get("model_requested")
    return (
        timestamp,
        row.get("provider"),
        row.get("harness"),
        row.get("purpose"),
        model,
        row.get("quota_name"),
        row.get("account_ref"),
        row.get("transaction_kind"),
        row.get("status"),
        row.get("invoice_id"),
        row.get("line_item_id"),
    )


def _database_metadata(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = connection.execute("SELECT key, value FROM ledger_metadata ORDER BY key").fetchall()
    except sqlite3.DatabaseError as exc:
        raise MalformedLedgerError("destination is not a canonical SQLite ledger") from exc
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in rows):
        raise MalformedLedgerError("invalid SQLite ledger metadata")
    return dict(rows)


def _validate_stored_row(stored: tuple[Any, ...], fact_type: str) -> dict[str, Any]:
    """Validate one selected row's payload, hash, identity, and extracted columns."""
    payload, digest, stored_type, namespace, identity, *typed = stored
    if not isinstance(payload, str) or not isinstance(digest, str):
        raise MalformedLedgerError("SQLite ledger contains invalid canonical payload data")
    actual_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if digest != actual_digest:
        raise MalformedLedgerError("SQLite ledger canonical hash mismatch")
    try:
        raw = json.loads(payload)
        normalized = normalize_fact(raw)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise MalformedLedgerError("SQLite ledger contains an invalid canonical fact") from exc
    if canonical_json(normalized) != payload:
        raise MalformedLedgerError("SQLite ledger contains noncanonical JSON")
    if stored_type != fact_type or normalized["fact_type"] != fact_type:
        raise MalformedLedgerError("SQLite ledger contains a mixed fact_type")
    if (namespace, identity) != _identity(normalized):
        raise MalformedLedgerError("SQLite ledger identity columns do not match canonical JSON")
    if tuple(typed) != _typed_values(normalized):
        raise MalformedLedgerError("SQLite ledger extracted columns do not match canonical JSON")
    return normalized


def _integrity_check_stored_row(stored: tuple[Any, ...], fact_type: str) -> dict[str, Any]:
    """Verify immutable SQLite integrity without repeating contract normalization.

    Facts are contract-normalized before append and protected by immutable
    triggers. Dashboard projections can therefore validate schema, payload hash,
    identity, and typed-column agreement without recursively re-running the full
    contract and secret scanner for every request. Whole-ledger audits retain
    canonical re-encoding and full contract validation.
    """
    payload, digest, stored_type, namespace, identity, *typed = stored
    if not isinstance(payload, str) or not isinstance(digest, str):
        raise MalformedLedgerError("SQLite ledger contains invalid canonical payload data")
    if digest != hashlib.sha256(payload.encode("utf-8")).hexdigest():
        raise MalformedLedgerError("SQLite ledger canonical hash mismatch")
    try:
        raw = json.loads(payload)
        if not isinstance(raw, dict):
            raise MalformedLedgerError("SQLite ledger contains an invalid canonical fact")
        if stored_type != fact_type or raw.get("fact_type") != fact_type:
            raise MalformedLedgerError("SQLite ledger contains a mixed fact_type")
        if (namespace, identity) != _identity(raw):
            raise MalformedLedgerError("SQLite ledger identity columns do not match canonical JSON")
        if tuple(typed) != _typed_values(raw):
            raise MalformedLedgerError("SQLite ledger extracted columns do not match canonical JSON")
    except (json.JSONDecodeError, KeyError, TypeError, ValidationError) as exc:
        raise MalformedLedgerError("SQLite ledger contains an invalid canonical fact") from exc
    return raw


def _validate_stored_facts(connection: sqlite3.Connection, fact_type: str) -> None:
    query = """
        SELECT canonical_json, canonical_sha256, fact_type, source_namespace, source_identity,
               occurred_or_observed_at, provider, harness, purpose, model, quota_name, account_ref,
               transaction_kind, transaction_status, invoice_id, line_item_id
        FROM facts ORDER BY ingestion_sequence
    """
    try:
        stored_rows = connection.execute(query).fetchall()
    except sqlite3.DatabaseError as exc:
        raise MalformedLedgerError("invalid SQLite facts table") from exc
    for stored in stored_rows:
        _validate_stored_row(stored, fact_type)


def _normalized_schema_sql(sql: str | None) -> str:
    if not isinstance(sql, str):
        return ""
    compact = " ".join(sql.rstrip(";").split()).casefold()
    return re.sub(r"\s*([(),;])\s*", r"\1", compact)


def _validate_sqlite_schema(
    connection: sqlite3.Connection,
    requested: str | None = None,
    *,
    verify_journal_mode: bool = True,
    timestamp_index_sql: str = _SQLITE_TIMESTAMP_INDEX_SQL,
) -> str:
    """Validate fixed-size schema and binding invariants without scanning facts."""
    try:
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
        schema_rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
        ).fetchall()
        columns = tuple(row[1] for row in connection.execute("PRAGMA table_xinfo(facts)"))
        index_rows = connection.execute("PRAGMA index_list(facts)").fetchall()
    except sqlite3.DatabaseError as exc:
        raise MalformedLedgerError("corrupt or incompatible SQLite ledger") from exc
    if verify_journal_mode and journal_mode != "wal":
        raise MalformedLedgerError("SQLite ledger is not persistently configured for WAL mode")
    expected_objects = (
        {("table", "facts", "facts"), ("table", "ledger_metadata", "ledger_metadata"),
         ("table", "sqlite_sequence", "sqlite_sequence")}
        | {("index", name, "facts") for name in _SQLITE_INDEXES}
        | {("trigger", name, "facts" if name.startswith("facts_") else "ledger_metadata")
           for name in _SQLITE_TRIGGERS}
    )
    actual_objects = {(kind, name, table) for kind, name, table, _sql in schema_rows}
    if actual_objects != expected_objects:
        raise MalformedLedgerError("SQLite ledger has unexpected or missing schema objects")
    schema_sql = {(kind, name): sql for kind, name, _table, sql in schema_rows}
    if (
        _normalized_schema_sql(schema_sql.get(("table", "facts")))
        != _normalized_schema_sql(_SQLITE_FACTS_TABLE_SQL)
        or _normalized_schema_sql(schema_sql.get(("table", "ledger_metadata")))
        != _normalized_schema_sql(_SQLITE_METADATA_TABLE_SQL)
    ):
        raise MalformedLedgerError("SQLite ledger table constraints have drifted")
    if columns != _SQLITE_FACT_COLUMNS:
        raise MalformedLedgerError("unsupported SQLite facts columns")
    if timestamp_index_sql == _SQLITE_TIMESTAMP_INDEX_SQL:
        expected_indexes = _SQLITE_INDEXES
    elif timestamp_index_sql == _SQLITE_LEGACY_TIMESTAMP_INDEX_SQL:
        expected_indexes = {
            **_SQLITE_INDEXES,
            "facts_timestamp_idx": (
                False,
                (("occurred_or_observed_at", False, "BINARY"),),
            ),
        }
    else:  # Internal callers may only audit one of the two allowlisted schemas.
        raise AssertionError("unsupported timestamp index schema")
    # This exact check is intentional: migration is allowed only from the
    # byte-for-byte CREATE statement previously emitted by this module.
    if schema_sql.get(("index", "facts_timestamp_idx")) != timestamp_index_sql:
        raise MalformedLedgerError("SQLite ledger timestamp index definition has drifted")
    actual_indexes: dict[str, tuple[bool, tuple[tuple[str | None, bool, str], ...]]] = {}
    for _sequence, name, unique, _origin, partial in index_rows:
        if partial:
            raise MalformedLedgerError("partial indexes are not valid ledger indexes")
        key_columns = tuple(
            (row[2], bool(row[3]), str(row[4]))
            for row in connection.execute(f'PRAGMA index_xinfo("{name}")') if row[5]
        )
        actual_indexes[name] = (bool(unique), key_columns)
    if actual_indexes != expected_indexes:
        raise MalformedLedgerError("SQLite ledger index definitions have drifted")
    for name, expected_sql in _SQLITE_TRIGGERS.items():
        if _normalized_schema_sql(schema_sql.get(("trigger", name))) != _normalized_schema_sql(expected_sql):
            raise MalformedLedgerError(f"SQLite ledger trigger {name!r} has drifted")
    metadata = _database_metadata(connection)
    if metadata.keys() != {"fact_type", "schema_version"} or metadata.get("schema_version") != _SQLITE_SCHEMA_VERSION:
        raise MalformedLedgerError("unsupported SQLite ledger metadata schema")
    bound_type = metadata.get("fact_type")
    if bound_type not in SUPPORTED_FACT_TYPES:
        raise MalformedLedgerError("SQLite ledger has an unsupported fact_type binding")
    if requested is not None and bound_type != requested:
        raise ValidationError(f"fact_type {requested!r} cannot be mixed into {bound_type!r} ledger")
    if user_version != int(_SQLITE_SCHEMA_VERSION):
        raise MalformedLedgerError("unsupported SQLite ledger user_version")
    return bound_type


def _audit_sqlite_connection(connection: sqlite3.Connection, requested: str | None = None) -> str:
    bound_type = _validate_sqlite_schema(connection, requested)
    try:
        check = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.DatabaseError as exc:
        raise MalformedLedgerError("SQLite integrity check failed") from exc
    if check != ("ok",):
        raise MalformedLedgerError("SQLite integrity check failed")
    _validate_stored_facts(connection, bound_type)
    return bound_type


def _migrate_legacy_timestamp_index(connection: sqlite3.Connection, requested: str) -> None:
    """Replace only the exact previously shipped timestamp index definition.

    The old schema is fully audited before DDL runs. This deliberately leaves
    unknown index SQL untouched so current-schema validation fails closed rather
    than blessing or repairing drift.
    """
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'index' AND name = 'facts_timestamp_idx'"
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise MalformedLedgerError("cannot inspect SQLite timestamp index") from exc
    index_sql = row[0] if row is not None else None
    if index_sql == _SQLITE_TIMESTAMP_INDEX_SQL:
        return
    if index_sql != _SQLITE_LEGACY_TIMESTAMP_INDEX_SQL:
        return
    _validate_sqlite_schema(
        connection,
        requested,
        timestamp_index_sql=_SQLITE_LEGACY_TIMESTAMP_INDEX_SQL,
    )
    try:
        connection.execute("DROP INDEX facts_timestamp_idx")
        connection.execute(_SQLITE_TIMESTAMP_INDEX_SQL)
    except sqlite3.DatabaseError as exc:
        raise MalformedLedgerError("could not migrate SQLite timestamp index") from exc


def _read_only_sqlite(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        _validate_sqlite_artifacts(path)
        immutable_parameter = "&immutable=1" if immutable else ""
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro{immutable_parameter}", uri=True
        )
        _validate_sqlite_artifacts(path)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise MalformedLedgerError("SQLite ledger could not enable foreign keys")
        return connection
    except (sqlite3.DatabaseError, MalformedLedgerError, OSError) as exc:
        if connection is not None:
            connection.close()
        if isinstance(exc, MalformedLedgerError):
            raise
        raise MalformedLedgerError(f"cannot open SQLite ledger {path}") from exc


def _validate_persistent_wal_header(path: Path) -> None:
    """Verify WAL persistence without opening a mutable SQLite connection.

    ``immutable=1`` is the dry-run-safe way to avoid creating SQLite sidecars,
    but such a connection reports ``journal_mode=delete``. Bytes 18 and 19 of
    the database header are the persistent read/write journal versions; both
    are 2 for WAL.
    """
    try:
        with path.open("rb") as stream:
            header = stream.read(20)
    except OSError as exc:
        raise MalformedLedgerError(f"cannot read SQLite ledger header: {path}") from exc
    if len(header) < 20 or header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x02\x02":
        raise MalformedLedgerError("SQLite ledger is not persistently configured for WAL mode")


def _reject_active_wal_for_immutable_read(path: Path) -> None:
    """Fail closed when immutable reads could ignore current WAL state."""
    if Path(f"{path}-wal").exists() or Path(f"{path}-shm").exists():
        raise MalformedLedgerError("dry-run cannot safely inspect an active WAL ledger")


def audit_sqlite_ledger(path: str | Path, *, fact_type: str | None = None) -> int:
    """Perform an explicit whole-ledger integrity and canonical-payload audit."""
    _check_fact_type(fact_type)
    target = Path(path).expanduser()
    if not target.exists() or not target.is_file():
        raise MalformedLedgerError(f"SQLite ledger is not a regular file: {target}")
    connection = _read_only_sqlite(target)
    try:
        _audit_sqlite_connection(connection, fact_type)
        return int(connection.execute("SELECT count(*) FROM facts").fetchone()[0])
    finally:
        connection.close()


def _execute_bounded_facts_query(
    connection: sqlite3.Connection, sql: str, parameters: tuple[Any, ...]
) -> list[tuple[Any, ...]]:
    """Small seam for query-plan tests; callers must provide fixed-shape SQL."""
    return connection.execute(sql, parameters).fetchall()


def query_sqlite_facts(
    path: str | Path,
    *,
    fact_type: str,
    filters: dict[str, str] | None = None,
    occurred_or_observed_at_gte: str | None = None,
    occurred_or_observed_at_lt: str | None = None,
    order: str = "asc",
    limit: int = _SQLITE_QUERY_LIMIT,
    offset: int = 0,
    contract_validation: bool = True,
) -> list[dict[str, Any]]:
    """Run a bounded, indexed private-ledger query and validate selected rows.

    ``fact_type`` is mandatory because every SQLite ledger is bound to exactly
    one contract type. Filters map only to immutable extracted columns. The
    extracted ``model`` column is intentionally not exposed: it prefers
    ``model_reported`` over ``model_requested`` and therefore cannot implement
    either field's exact contract semantics. Callers needing an exact model
    predicate must apply it to the returned canonical facts.

    This online query validates the fixed-size schema plus each returned row's
    canonical JSON, hash, identity, and typed-column agreement. It deliberately
    does not run ``quick_check`` or inspect non-returned historical rows; use
    :func:`audit_sqlite_ledger` for an explicit whole-ledger audit.
    """
    if not isinstance(fact_type, str) or fact_type not in SUPPORTED_FACT_TYPES:
        raise ValidationError("a supported fact_type binding is required")
    if not isinstance(contract_validation, bool):
        raise ValidationError("contract_validation must be a boolean")
    requested_filters = {} if filters is None else filters
    if not isinstance(requested_filters, dict):
        raise ValidationError("filters must be an object")
    for name, value in requested_filters.items():
        if name not in _SQLITE_QUERY_FILTER_COLUMNS:
            raise ValidationError(f"unsupported SQLite fact filter {name!r}")
        if not isinstance(value, str):
            raise ValidationError(f"SQLite fact filter {name!r} must be a string")
    normalized_order = order.casefold() if isinstance(order, str) else ""
    if normalized_order not in {"asc", "desc"}:
        raise ValidationError("order must be 'asc' or 'desc'")
    for name, value, maximum in (
        ("limit", limit, _SQLITE_QUERY_LIMIT),
        ("offset", offset, MAX_SAFE_INTEGER),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
            raise ValidationError(f"{name} must be a nonnegative integer no greater than {maximum}")
    lower_bound = None
    if occurred_or_observed_at_gte is not None:
        lower_bound = _timestamp(occurred_or_observed_at_gte, "occurred_or_observed_at_gte")
    upper_bound = None
    if occurred_or_observed_at_lt is not None:
        upper_bound = _timestamp(occurred_or_observed_at_lt, "occurred_or_observed_at_lt")

    target = Path(path).expanduser()
    if not target.exists():
        return []
    if not target.is_file():
        raise MalformedLedgerError(f"SQLite ledger is not a regular file: {target}")
    connection = _read_only_sqlite(target)
    try:
        _validate_sqlite_schema(connection, fact_type)
        clauses = ["fact_type = ?"]
        parameters: list[Any] = [fact_type]
        for name, value in requested_filters.items():
            clauses.append(f"{_SQLITE_QUERY_FILTER_COLUMNS[name]} = ?")
            parameters.append(value)
        if lower_bound is not None:
            clauses.append(f"{_SQLITE_TIMESTAMP_KEY_SQL} >= ?")
            # Canonical timestamps are fixed-width UTC through whole seconds;
            # removing only the terminal Z makes the exact second a lexical
            # prefix of every following fractional instant, at any precision.
            parameters.append(lower_bound[:-1])
        if upper_bound is not None:
            clauses.append(f"{_SQLITE_TIMESTAMP_KEY_SQL} < ?")
            parameters.append(upper_bound[:-1])

        # Force a matching allowlisted index so selectivity cannot be discarded
        # merely to satisfy ordering. Without a typed filter, date-bounded reads
        # use the timestamp index naturally and unfiltered reads stay bounded.
        indexed_by = ""
        if requested_filters:
            first_filter = next(iter(requested_filters))
            indexed_by = f" INDEXED BY {_SQLITE_QUERY_INDEXES[first_filter]}"
        elif lower_bound is not None or upper_bound is not None:
            indexed_by = " INDEXED BY facts_timestamp_idx"
        direction = normalized_order.upper()
        sql = f"""
            SELECT canonical_json, canonical_sha256, fact_type, source_namespace, source_identity,
                   occurred_or_observed_at, provider, harness, purpose, model, quota_name, account_ref,
                   transaction_kind, transaction_status, invoice_id, line_item_id
            FROM facts{indexed_by}
            WHERE {' AND '.join(clauses)}
            ORDER BY {_SQLITE_TIMESTAMP_KEY_SQL} {direction}, ingestion_sequence {direction}
            LIMIT ? OFFSET ?
        """
        parameters.extend((limit, offset))
        try:
            stored_rows = _execute_bounded_facts_query(connection, sql, tuple(parameters))
        except sqlite3.DatabaseError as exc:
            raise MalformedLedgerError("invalid SQLite facts query") from exc
        validator = _validate_stored_row if contract_validation else _integrity_check_stored_row
        return [validator(stored, fact_type) for stored in stored_rows]
    finally:
        connection.close()


def read_sqlite_facts(
    path: str | Path, *, fact_type: str | None = None
) -> list[dict[str, Any]]:
    """Read and integrity-check canonical facts in ingestion order."""
    _check_fact_type(fact_type)
    target = Path(path).expanduser()
    if not target.exists():
        return []
    if not target.is_file():
        raise MalformedLedgerError(f"SQLite ledger is not a regular file: {target}")
    connection = _read_only_sqlite(target)
    try:
        _audit_sqlite_connection(connection, fact_type)
        payloads = connection.execute(
            "SELECT canonical_json FROM facts ORDER BY ingestion_sequence"
        ).fetchall()
        return [json.loads(payload) for (payload,) in payloads]
    finally:
        connection.close()


def _open_writable_sqlite(path: Path, fact_type: str) -> tuple[sqlite3.Connection, bool]:
    existed = os.path.lexists(path)
    if existed:
        _validate_sqlite_artifacts(path)
    else:
        _ensure_private_directory(path.parent)
        preexisting_sidecars = [
            candidate
            for candidate in (Path(f"{path}-wal"), Path(f"{path}-shm"))
            if os.path.lexists(candidate)
        ]
        if preexisting_sidecars:
            raise MalformedLedgerError(
                f"cannot initialize absent database with pre-existing sidecar: {preexisting_sidecars[0]}"
            )
        _validate_sqlite_artifacts(path, database_required=False)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags, 0o600)
        os.close(descriptor)
    connection: sqlite3.Connection | None = None
    try:
        _validate_sqlite_artifacts(path)
        connection = sqlite3.connect(path, isolation_level=None)
        _validate_sqlite_artifacts(path)
        connection.execute("PRAGMA busy_timeout = 5000")
        if existed:
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
            if journal_mode != "wal":
                raise MalformedLedgerError("SQLite ledger is not persistently configured for WAL mode")
        _configure_sqlite(connection)
        if not existed:
            _create_sqlite_schema(connection, fact_type)
        _secure_sqlite_files(path)
        return connection, not existed
    except Exception:
        if connection is not None:
            connection.close()
        if not existed:
            for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
        raise


def _compare_sqlite_incoming(
    connection: sqlite3.Connection,
    incoming: list[dict[str, Any]],
    *,
    quarantine_conflicts: bool = False,
    equivalent_changed_fields: frozenset[str] = frozenset(),
) -> tuple[
    AppendResult,
    list[tuple[dict[str, Any], str, str]],
    list[ConflictSummary],
]:
    grouped: dict[tuple[str, str], list[tuple[dict[str, Any], str, str]]] = {}
    for row in incoming:
        payload = _jcs_dumps(row)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        grouped.setdefault(_identity(row), []).append((row, payload, digest))

    appended = replayed = 0
    pending: list[tuple[dict[str, Any], str, str]] = []
    conflicts: list[ConflictSummary] = []
    for identity, variants in grouped.items():
        distinct: dict[str, tuple[dict[str, Any], str]] = {}
        for row, payload, digest in variants:
            distinct.setdefault(payload, (row, digest))
        existing = connection.execute(
            "SELECT canonical_json, canonical_sha256 FROM facts WHERE source_namespace = ? AND source_identity = ?",
            identity,
        ).fetchone()
        existing_payload = str(existing[0]) if existing is not None else None

        if len(distinct) > 1:
            if not quarantine_conflicts:
                raise IdentityConflictError(f"conflicting replay for {identity!r}")
            if existing_payload is not None:
                changed = [
                    _conflict_summary(identity, existing_payload, payload)
                    for payload in distinct
                    if payload != existing_payload
                ]
                if changed and all(
                    summary.changed_fields
                    and set(summary.changed_fields) <= equivalent_changed_fields
                    for summary in changed
                ):
                    replayed += len(variants)
                    conflicts.extend(
                        _conflict_summary(
                            identity,
                            existing_payload,
                            payload,
                            resolution="canonical_replay",
                        )
                        for payload in distinct
                        if payload != existing_payload
                    )
                    continue
                summary = next(
                    (
                        candidate
                        for candidate in changed
                        if not candidate.changed_fields
                        or not set(candidate.changed_fields) <= equivalent_changed_fields
                    ),
                    changed[0],
                )
            else:
                payloads = list(distinct)
                summary = _conflict_summary(identity, payloads[0], payloads[1])
            conflicts.append(summary)
            continue

        payload, (row, digest) = next(iter(distinct.items()))
        if existing_payload is None:
            pending.append((row, payload, digest))
            appended += 1
            replayed += len(variants) - 1
            continue
        if existing == (payload, digest):
            replayed += len(variants)
            continue
        if not quarantine_conflicts:
            raise IdentityConflictError(f"conflicting replay for {identity!r}")
        summary = _conflict_summary(identity, existing_payload, payload)
        if summary.changed_fields and set(summary.changed_fields) <= equivalent_changed_fields:
            replayed += len(variants)
            conflicts.append(
                _conflict_summary(
                    identity,
                    existing_payload,
                    payload,
                    resolution="canonical_replay",
                )
            )
        else:
            conflicts.append(summary)
    return AppendResult(len(incoming), appended, replayed), pending, conflicts


def _append_sqlite_facts_policy_locked(
    target: Path,
    incoming: list[dict[str, Any]],
    expected: str,
    *,
    quarantine_conflicts: bool,
    equivalent_changed_fields: frozenset[str] = frozenset(),
    reconcile: Callable[
        [list[dict[str, Any]], list[dict[str, Any]]],
        tuple[list[dict[str, Any]], Any],
    ] | None = None,
) -> tuple[AppendResult, tuple[ConflictSummary, ...], Any]:
    connection, created = _open_writable_sqlite(target, expected)
    completed = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        _migrate_legacy_timestamp_index(connection, expected)
        _validate_sqlite_schema(connection, expected)
        _secure_sqlite_files(target)
        metadata: Any = None
        if reconcile is not None:
            payloads = connection.execute(
                "SELECT canonical_json FROM facts ORDER BY ingestion_sequence"
            ).fetchall()
            existing = [json.loads(payload) for (payload,) in payloads]
            incoming, metadata = reconcile(existing, incoming)
            incoming = [normalize_fact(fact) for fact in incoming]
            reconciled_type = _homogeneous_fact_type(incoming, expected)
            if reconciled_type != expected:
                raise ValidationError("reconciled SQLite batch changed fact_type")
        result, pending, conflicts = _compare_sqlite_incoming(
            connection,
            incoming,
            quarantine_conflicts=quarantine_conflicts,
            equivalent_changed_fields=equivalent_changed_fields,
        )
        ingested_at = _timestamp(datetime.now(timezone.utc).isoformat(), "ingested_at")
        for row, payload, digest in pending:
            namespace, identity = _identity(row)
            connection.execute(
                """
                INSERT INTO facts(
                    ingested_at, fact_type, source_namespace, source_identity, canonical_json,
                    canonical_sha256, occurred_or_observed_at, provider, harness, purpose, model,
                    quota_name, account_ref, transaction_kind, transaction_status, invoice_id, line_item_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ingested_at, expected, namespace, identity, payload, digest,
                    *_typed_values(row),
                ),
            )
        connection.execute("COMMIT")
        completed = True
        return result, tuple(conflicts), metadata
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
        if created and not completed:
            _remove_sqlite_artifacts(target)
        elif os.path.lexists(target):
            _secure_sqlite_files(target)


def _append_sqlite_facts_locked(
    target: Path,
    incoming: list[dict[str, Any]],
    expected: str,
) -> AppendResult:
    result, _conflicts, _metadata = _append_sqlite_facts_policy_locked(
        target,
        incoming,
        expected,
        quarantine_conflicts=False,
    )
    return result


def append_sqlite_facts(
    path: str | Path,
    facts: Iterable[dict[str, Any]],
    *,
    fact_type: str | None = None,
    dry_run: bool = False,
) -> AppendResult:
    """Transactionally append using only fixed schema checks and indexed identities."""
    incoming = [normalize_fact(fact) for fact in facts]
    expected = _homogeneous_fact_type(incoming, fact_type)
    target = Path(path).expanduser()
    if dry_run:
        if not target.exists():
            result, _pending = _compare([], incoming)
            return result
        if not target.is_file():
            raise MalformedLedgerError(f"SQLite ledger is not a regular file: {target}")
        _reject_active_wal_for_immutable_read(target)
        connection = _read_only_sqlite(target, immutable=True)
        try:
            _validate_persistent_wal_header(target)
            _validate_sqlite_schema(connection, expected, verify_journal_mode=False)
            result, _pending, _conflicts = _compare_sqlite_incoming(connection, incoming)
            _reject_active_wal_for_immutable_read(target)
            return result
        finally:
            connection.close()
    if expected is None:
        if not target.exists():
            raise ValidationError("fact_type is required to create an empty SQLite ledger")
        if not target.is_file():
            raise MalformedLedgerError(f"SQLite ledger is not a regular file: {target}")
        connection = _read_only_sqlite(target)
        try:
            _validate_sqlite_schema(connection)
            return AppendResult(0, 0, 0)
        finally:
            connection.close()

    _ensure_private_directory(target.parent)
    with _ledger_lock(target, fcntl.LOCK_EX):
        return _append_sqlite_facts_locked(target, incoming, expected)


def append_sqlite_facts_quarantined(
    path: str | Path,
    facts: Iterable[dict[str, Any]],
    *,
    fact_type: str | None = None,
    dry_run: bool = False,
    equivalent_changed_fields: frozenset[str] = frozenset(),
) -> tuple[AppendResult, tuple[ConflictSummary, ...]]:
    """Append non-conflicting SQLite facts while safely reporting changed replays."""
    incoming = [normalize_fact(fact) for fact in facts]
    expected = _homogeneous_fact_type(incoming, fact_type)
    target = Path(path).expanduser()
    if dry_run:
        if not target.exists():
            result, _pending, conflicts = _compare_quarantined(
                [],
                incoming,
                equivalent_changed_fields=equivalent_changed_fields,
            )
            return result, tuple(conflicts)
        if not target.is_file():
            raise MalformedLedgerError(f"SQLite ledger is not a regular file: {target}")
        _reject_active_wal_for_immutable_read(target)
        connection = _read_only_sqlite(target, immutable=True)
        try:
            _validate_persistent_wal_header(target)
            _validate_sqlite_schema(connection, expected, verify_journal_mode=False)
            result, _pending, conflicts = _compare_sqlite_incoming(
                connection,
                incoming,
                quarantine_conflicts=True,
                equivalent_changed_fields=equivalent_changed_fields,
            )
            _reject_active_wal_for_immutable_read(target)
            return result, tuple(conflicts)
        finally:
            connection.close()
    if expected is None:
        if not target.exists():
            raise ValidationError("fact_type is required to create an empty SQLite ledger")
        if not target.is_file():
            raise MalformedLedgerError(f"SQLite ledger is not a regular file: {target}")
        connection = _read_only_sqlite(target)
        try:
            _validate_sqlite_schema(connection)
            return AppendResult(0, 0, 0), ()
        finally:
            connection.close()

    _ensure_private_directory(target.parent)
    with _ledger_lock(target, fcntl.LOCK_EX):
        result, conflicts, _metadata = _append_sqlite_facts_policy_locked(
            target,
            incoming,
            expected,
            quarantine_conflicts=True,
            equivalent_changed_fields=equivalent_changed_fields,
        )
        return result, conflicts


def append_sqlite_facts_quarantined_reconciled(
    path: str | Path,
    facts: Iterable[dict[str, Any]],
    *,
    fact_type: str,
    reconcile: Callable[
        [list[dict[str, Any]], list[dict[str, Any]]],
        tuple[list[dict[str, Any]], Any],
    ],
    dry_run: bool = False,
    equivalent_changed_fields: frozenset[str] = frozenset(),
) -> tuple[AppendResult, tuple[ConflictSummary, ...], Any]:
    """Reconcile against one locked SQLite transaction before appending."""
    incoming = [normalize_fact(fact) for fact in facts]
    expected = _homogeneous_fact_type(incoming, fact_type)
    assert expected is not None
    target = Path(path).expanduser()

    if dry_run:
        if not target.exists():
            prepared, metadata = reconcile([], incoming)
            prepared = [normalize_fact(fact) for fact in prepared]
            result, _pending, conflicts = _compare_quarantined(
                [],
                prepared,
                equivalent_changed_fields=equivalent_changed_fields,
            )
            return result, tuple(conflicts), metadata
        if not target.is_file():
            raise MalformedLedgerError(f"SQLite ledger is not a regular file: {target}")
        _reject_active_wal_for_immutable_read(target)
        connection = _read_only_sqlite(target, immutable=True)
        try:
            _validate_persistent_wal_header(target)
            _validate_sqlite_schema(connection, expected, verify_journal_mode=False)
            payloads = connection.execute(
                "SELECT canonical_json FROM facts ORDER BY ingestion_sequence"
            ).fetchall()
            existing = [json.loads(payload) for (payload,) in payloads]
            prepared, metadata = reconcile(existing, incoming)
            prepared = [normalize_fact(fact) for fact in prepared]
            result, _pending, conflicts = _compare_sqlite_incoming(
                connection,
                prepared,
                quarantine_conflicts=True,
                equivalent_changed_fields=equivalent_changed_fields,
            )
            _reject_active_wal_for_immutable_read(target)
            return result, tuple(conflicts), metadata
        finally:
            connection.close()

    _ensure_private_directory(target.parent)
    with _ledger_lock(target, fcntl.LOCK_EX):
        return _append_sqlite_facts_policy_locked(
            target,
            incoming,
            expected,
            quarantine_conflicts=True,
            equivalent_changed_fields=equivalent_changed_fields,
            reconcile=reconcile,
        )


def _reject_path_alias(source: Path, destination: Path) -> None:
    try:
        same = source.resolve(strict=False) == destination.resolve(strict=False)
        if source.exists() and destination.exists():
            same = same or os.path.samefile(source, destination)
    except OSError as exc:
        raise ValidationError("could not resolve source and destination paths") from exc
    if same:
        raise ValidationError("source and destination resolve to the same file")


def _remove_sqlite_artifacts(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def migrate_jsonl_to_sqlite(
    jsonl_path: str | Path,
    sqlite_path: str | Path,
    *,
    fact_type: str | None = None,
    dry_run: bool = False,
) -> AppendResult:
    """Validate a unique homogeneous JSONL stream, then import it atomically."""
    source = Path(jsonl_path).expanduser()
    destination = Path(sqlite_path).expanduser()
    _reject_path_alias(source, destination)
    if not source.exists() or not source.is_file():
        raise MalformedLedgerError(f"JSONL migration source is not a regular file: {source}")
    rows = _read_facts_without_side_effects(source) if dry_run else read_facts(source)
    expected = _homogeneous_fact_type(rows, fact_type)
    if expected is None:
        raise ValidationError("fact_type is required to migrate an empty JSONL ledger")
    seen: set[tuple[str, str]] = set()
    for row in rows:
        identity = _identity(row)
        if identity in seen:
            raise IdentityConflictError(f"duplicate normative identity in migration source: {identity!r}")
        seen.add(identity)
    result = append_sqlite_facts(destination, rows, fact_type=expected, dry_run=dry_run)
    if result.discovered != len(rows):
        raise MalformedLedgerError("migration source/destination fact-count parity failed")
    return result


def export_sqlite_to_jsonl(
    sqlite_path: str | Path,
    jsonl_path: str | Path,
    *,
    fact_type: str | None = None,
) -> int:
    """Atomically export audited canonical facts under the JSONL append lock."""
    source = Path(sqlite_path).expanduser()
    target = Path(jsonl_path).expanduser()
    _reject_path_alias(source, target)
    if not source.exists() or not source.is_file():
        raise MalformedLedgerError(f"SQLite export source is not a regular file: {source}")
    rows = read_sqlite_facts(source, fact_type=fact_type)
    _ensure_private_directory(target.parent)
    with _ledger_lock(target, fcntl.LOCK_EX):
        _atomic_write(target, rows)
    return len(rows)
