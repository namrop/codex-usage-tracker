"""Public-safe hourly projection of the private canonical usage ledger."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .canonical_ledger import MAX_SAFE_INTEGER, ValidationError, canonical_timestamp, query_sqlite_facts


PUBLIC_PROJECTION_KIND = "namrop_public_usage_projection.v3"
DEFAULT_PUBLIC_PROJECTION_SOURCE = "unified-usage-public-projection"
DEFAULT_PUBLIC_PROJECTION_HOURS = 168
MAX_PUBLIC_PROJECTION_HOURS = 168
_MAX_INPUT_EVENTS = 100_000
_MAX_PUBLIC_PROJECTION_BYTES = 1024 * 1024
_SQLITE_SUFFIXES = frozenset({".sqlite3", ".sqlite", ".db"})
_TOKEN_FIELDS = (
    "input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
)
_TOP_LEVEL_KEYS = frozenset({
    "kind", "schema_version", "source", "generated_at", "bucket_minutes", "rows",
    "provider_rows", "model_rows", "harness_rows", "summary",
})
_ROW_KEYS = frozenset({
    "window_start", "window_end", "input_tokens", "cache_read_tokens", "cache_write_tokens",
    "output_tokens", "reasoning_tokens", "prompt_tokens", "total_tokens", "request_attempts",
    "cache_hit_pct", "measurement_confidence",
})
_SUMMARY_KEYS = frozenset({
    "updated_at", "window_count", "total_tokens", "request_attempts", "latest_total_tokens",
    "latest_cache_hit_pct",
})
_PROVIDER_ROW_KEYS = frozenset({
    "label", "total_tokens", "request_attempts", "share_pct", "measurement_confidence",
})
_MODEL_ROW_KEYS = frozenset({
    "provider_label", "model_label", "total_tokens", "request_attempts", "share_pct",
    "measurement_confidence",
})
_HARNESS_ROW_KEYS = frozenset({
    "label", "total_tokens", "request_attempts", "share_pct", "measurement_confidence",
})
_CONFIDENCE_VALUES = frozenset({"exact", "reconstructed", "mixed", "unknown"})
_PUBLIC_SOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PUBLIC_PROVIDER_LABELS = frozenset({
    "Codex", "Anthropic", "OpenAI", "OpenCode Go", "OpenCode", "OpenRouter",
    "DeepSeek", "Local models", "LLM Gateway", "Kimi Coding", "Z.AI", "Other",
})
_PUBLIC_PROVIDER_MAP = {
    "openai-codex": "Codex",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "harness-openai": "OpenAI",
    "opencode-go": "OpenCode Go",
    "opencode": "OpenCode",
    "openrouter": "OpenRouter",
    "deepseek": "DeepSeek",
    "acubens-mlx": "Local models",
    "harness-acubens": "Local models",
    "llmgateway": "LLM Gateway",
    "kimi-coding": "Kimi Coding",
    "zai": "Z.AI",
}
_PUBLIC_HARNESS_MAP = {
    "hermes": "Hermes",
    "claude_code": "Claude Code",
}
_PUBLIC_HARNESS_LABELS = frozenset({*_PUBLIC_HARNESS_MAP.values(), "Other"})
_PUBLIC_MODEL_MAP = {
    # OpenAI / Codex
    "gpt-5.6": "GPT-5.6",
    "gpt-5.5": "GPT-5.5",
    "gpt-5.5-fast": "GPT-5.5 Fast",
    "gpt-5.3-codex": "GPT-5.3 Codex",
    "gpt-5.2-codex": "GPT-5.2 Codex",
    # Anthropic
    "claude-sonnet-5": "Claude Sonnet 5",
    "claude-fable-5": "Claude Fable 5",
    "claude-opus-5": "Claude Opus 5",
    "claude-opus-4-8": "Claude Opus 4.8",
    "claude-opus-4.8": "Claude Opus 4.8",
    "claude-opus-4-7": "Claude Opus 4.7",
    "claude-opus-4.7": "Claude Opus 4.7",
    "claude-haiku-4-5-20251001": "Claude Haiku 4.5",
    "claude-sonnet-4": "Claude Sonnet 4",
    # Hosted public model families currently represented in the ledger.
    "deepseek-v4-pro": "DeepSeek V4 Pro",
    "deepseek-v4-flash": "DeepSeek V4 Flash",
    "deepseek-chat": "DeepSeek Chat",
    "deepseek-reasoner": "DeepSeek Reasoner",
    "deepseek-v3.2": "DeepSeek V3.2",
    "glm-5.2": "GLM 5.2",
    "glm-5.1": "GLM 5.1",
    "glm-5": "GLM 5",
    "glm-4.7": "GLM 4.7",
    "glm-4.6": "GLM 4.6",
    "k3": "Kimi K3 Coding",
    "qwen3.7-plus": "Qwen 3.7 Plus",
    "qwen3.7-max": "Qwen 3.7 Max",
    "qwen3.6-plus": "Qwen 3.6 Plus",
    "qwen3.6-27b": "Qwen 3.6 27B",
    "qwen3.6-35b-a3b": "Qwen 3.6 35B A3B",
    "qwen3.5-35b-a3b": "Qwen 3.5 35B A3B",
    "qwen3.5-9b": "Qwen 3.5 9B",
    "qwen3:30b": "Qwen 3 30B",
    "qwen3-coder-next": "Qwen 3 Coder Next",
    "gemma-4-31b-it": "Gemma 4 31B IT",
    "gemma-4-26b-a4b-it": "Gemma 4 26B A4B IT",
    "gemma-4-26b-a4b-it-mxfp4": "Gemma 4 26B A4B IT",
    "gemma-3-27b-it": "Gemma 3 27B IT",
    "grok-4.20-reasoning": "Grok 4.20 Reasoning",
    "grok-4.1": "Grok 4.1",
    "grok-4-1-fast-reasoning": "Grok 4.1 Fast Reasoning",
    "grok-code-fast-1": "Grok Code Fast 1",
    "aion-2.0": "Aion 2.0",
    "aion-1.0": "Aion 1.0",
    "aion-1.0-mini": "Aion 1.0 Mini",
}
_PUBLIC_MODEL_LABELS = frozenset(_PUBLIC_MODEL_MAP.values())
_PUBLIC_MODEL_LABEL_MAP = {label.casefold(): label for label in _PUBLIC_MODEL_LABELS}
def _utc_text(value: datetime) -> str:
    """Render a timezone-aware datetime using the canonical ledger timestamp form."""
    return canonical_timestamp(value.astimezone(timezone.utc).isoformat())


def _validated_hours(hours: int) -> int:
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= MAX_PUBLIC_PROJECTION_HOURS:
        raise ValueError(f"hours must be an integer from 1 through {MAX_PUBLIC_PROJECTION_HOURS}")
    return hours


def _validated_source(source: Any) -> str:
    """Accept an opaque public marker, never a path or free-form private text."""
    if not isinstance(source, str) or _PUBLIC_SOURCE_RE.fullmatch(source) is None:
        raise ValueError(
            "source must be a 1-128 character public marker containing only "
            "ASCII letters, digits, '.', '_', ':', or '-'"
        )
    return source


def _event_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValidationError("usage event occurred_at is not a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("usage event occurred_at is not a timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError("usage event occurred_at is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _bucket_confidence(values: set[str]) -> str:
    if values == {"exact"}:
        return "exact"
    if values == {"reconstructed"}:
        return "reconstructed"
    if not values or values <= {"estimated", "unknown"}:
        return "unknown"
    return "mixed"


def _request_attempts(row: dict[str, Any]) -> int:
    if row.get("record_kind") == "api_attempt":
        return 1
    if row.get("record_kind") == "historical_aggregate":
        return int(row.get("reconstructed_call_count") or 0)
    return 0


def _public_provider_label(value: Any) -> str:
    if not isinstance(value, str):
        return "Other"
    return _PUBLIC_PROVIDER_MAP.get(value.casefold(), "Other")


def _public_harness_label(value: Any) -> str:
    if not isinstance(value, str):
        return "Other"
    return _PUBLIC_HARNESS_MAP.get(value.casefold(), "Other")


def _public_model_label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    # Accept only a finite set after stripping a provider namespace and the
    # internal execution suffix. This cannot turn a family-shaped private ID
    # into public output unless its basename is explicitly allowlisted.
    segment = value.rsplit("/", 1)[-1]
    segment = re.sub(r"(?:-sol)+$", "", segment, flags=re.IGNORECASE)
    normalized = segment.casefold()
    return _PUBLIC_MODEL_MAP.get(normalized) or _PUBLIC_MODEL_LABEL_MAP.get(normalized)


def _fact_total_tokens(row: dict[str, Any]) -> int:
    return sum(int(row.get(field) or 0) for field in (
        "input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens",
    ))


def _add_comparison_fact(group: dict[str, Any], fact: dict[str, Any]) -> None:
    fact_total = _fact_total_tokens(fact)
    group["total_tokens"] += fact_total
    group["request_attempts"] += _request_attempts(fact)
    group["confidences"].add(str(fact.get("measurement_confidence") or "unknown"))
    if fact.get("record_kind") == "correction" and fact_total != 0:
        group["has_token_correction"] = True


def _merge_comparison_groups(destination: dict[str, Any], source: dict[str, Any]) -> None:
    destination["total_tokens"] += source["total_tokens"]
    destination["request_attempts"] += source["request_attempts"]
    destination["confidences"].update(source["confidences"])
    destination["has_token_correction"] |= source["has_token_correction"]


def _empty_comparison_group() -> dict[str, Any]:
    return {
        "total_tokens": 0,
        "request_attempts": 0,
        "confidences": set(),
        "has_token_correction": False,
    }


def _share_pct(total_tokens: int, projection_total: int) -> float:
    if projection_total <= 0 or total_tokens <= 0:
        return 0.0
    return _ratio_percentage(total_tokens, projection_total)


def _cache_hit_pct(cache_read_tokens: int, prompt_tokens: int) -> float | None:
    if cache_read_tokens < 0 or prompt_tokens <= 0:
        return None
    return _ratio_percentage(cache_read_tokens, prompt_tokens)


def _ratio_percentage(numerator: int, denominator: int) -> float:
    if numerator <= 0 or denominator <= 0:
        return 0.0
    if numerator >= denominator:
        return 100.0
    rounded, remainder = divmod(numerator * 1000, denominator)
    twice_remainder = remainder * 2
    if twice_remainder > denominator or (twice_remainder == denominator and rounded % 2 == 1):
        rounded += 1
    return rounded / 10


def _bounded_groups(
    groups: dict[Any, dict[str, Any]], *, other_key: Any, maximum: int
) -> list[tuple[Any, dict[str, Any]]]:
    if any(
        group["has_token_correction"] and group["total_tokens"] <= 0
        for group in groups.values()
    ):
        raise ValueError("public comparison groups cannot represent correction arithmetic")
    positive = {key: value for key, value in groups.items() if value["total_tokens"] > 0}
    other = positive.get(other_key)
    named = sorted(
        ((key, value) for key, value in positive.items() if key != other_key),
        key=lambda item: (-item[1]["total_tokens"], item[0]),
    )
    needs_other = other is not None or len(named) > maximum
    keep_count = maximum - 1 if needs_other else maximum
    kept = named[:keep_count]
    dropped = named[keep_count:]
    if needs_other:
        merged = _empty_comparison_group()
        if other is not None:
            _merge_comparison_groups(merged, other)
        for _key, group in dropped:
            _merge_comparison_groups(merged, group)
        if merged["total_tokens"] > 0:
            kept.append((other_key, merged))
    return sorted(kept, key=lambda item: (-item[1]["total_tokens"], item[0]))


def _ranking_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attribute corrections only through their canonical target identity."""
    by_identity = {
        (fact.get("source_namespace"), fact.get("source_event_id")): fact
        for fact in facts
    }
    attributed: list[dict[str, Any]] = []
    for fact in facts:
        if fact.get("record_kind") != "correction":
            attributed.append(fact)
            continue
        target = by_identity.get((
            fact.get("corrects_source_namespace"),
            fact.get("corrects_source_event_id"),
        ))
        if target is None:
            raise ValueError("correction target attribution is unavailable")
        attributed.append({
            **fact,
            "harness": target.get("harness"),
            "provider": target.get("provider"),
            "model_requested": target.get("model_requested"),
            "model_reported": target.get("model_reported"),
        })
    return attributed


def _build_provider_rows(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for fact in facts:
        label = _public_provider_label(fact.get("provider"))
        _add_comparison_fact(groups.setdefault(label, _empty_comparison_group()), fact)
    bounded = _bounded_groups(groups, other_key="Other", maximum=8)
    ranking_total = sum(group["total_tokens"] for _label, group in bounded)
    return [
        {
            "label": label,
            "total_tokens": group["total_tokens"],
            "request_attempts": group["request_attempts"],
            "share_pct": _share_pct(group["total_tokens"], ranking_total),
            "measurement_confidence": _bucket_confidence(group["confidences"]),
        }
        for label, group in bounded
    ]


def _build_model_rows(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    other_key = ("Other", "Other")
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for fact in facts:
        provider = _public_provider_label(fact.get("provider"))
        model = _public_model_label(fact.get("model_reported") or fact.get("model_requested"))
        key = (provider, model) if provider != "Other" and model is not None else other_key
        _add_comparison_fact(groups.setdefault(key, _empty_comparison_group()), fact)
    bounded = _bounded_groups(groups, other_key=other_key, maximum=12)
    ranking_total = sum(group["total_tokens"] for _key, group in bounded)
    return [
        {
            "provider_label": key[0],
            "model_label": key[1],
            "total_tokens": group["total_tokens"],
            "request_attempts": group["request_attempts"],
            "share_pct": _share_pct(group["total_tokens"], ranking_total),
            "measurement_confidence": _bucket_confidence(group["confidences"]),
        }
        for key, group in bounded
    ]


def _build_harness_rows(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for fact in facts:
        label = _public_harness_label(fact.get("harness"))
        _add_comparison_fact(groups.setdefault(label, _empty_comparison_group()), fact)
    bounded = _bounded_groups(groups, other_key="Other", maximum=4)
    ranking_total = sum(group["total_tokens"] for _label, group in bounded)
    return [
        {
            "label": label,
            "total_tokens": group["total_tokens"],
            "request_attempts": group["request_attempts"],
            "share_pct": _share_pct(group["total_tokens"], ranking_total),
            "measurement_confidence": _bucket_confidence(group["confidences"]),
        }
        for label, group in bounded
    ]


def build_unified_public_projection(
    ledger_path: str | Path,
    *,
    quota_ledger_path: str | Path | None = None,
    hours: int = DEFAULT_PUBLIC_PROJECTION_HOURS,
    source: str = DEFAULT_PUBLIC_PROJECTION_SOURCE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build complete UTC hourly buckets from a type-bound usage SQLite ledger."""
    count = _validated_hours(hours)
    public_source = _validated_source(source)
    target = Path(ledger_path).expanduser()
    if target.suffix.casefold() not in _SQLITE_SUFFIXES:
        raise ValueError("unified public projection input must be SQLite")
    if not target.exists() or not target.is_file():
        raise ValueError("unified public projection input must be an existing SQLite ledger")

    generated = datetime.now(timezone.utc) if now is None else now
    if generated.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    generated = generated.astimezone(timezone.utc)
    complete_end = generated.replace(minute=0, second=0, microsecond=0)
    projection_start = complete_end - timedelta(hours=count)
    facts = query_sqlite_facts(
        target,
        fact_type="usage_event_v1",
        occurred_or_observed_at_gte=_utc_text(projection_start),
        occurred_or_observed_at_lt=_utc_text(complete_end),
        order="asc",
        limit=_MAX_INPUT_EVENTS + 1,
    )
    if len(facts) > _MAX_INPUT_EVENTS:
        raise ValueError("usage query exceeds the bounded public projection event limit")

    buckets: list[dict[str, Any]] = []
    confidences: list[set[str]] = []
    for index in range(count):
        window_start = projection_start + timedelta(hours=index)
        window_end = window_start + timedelta(hours=1)
        buckets.append({
            "window_start": _utc_text(window_start),
            "window_end": _utc_text(window_end),
            "input_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "prompt_tokens": 0,
            "total_tokens": 0,
            "request_attempts": 0,
            "cache_hit_pct": None,
            "measurement_confidence": "unknown",
        })
        confidences.append(set())

    for fact in facts:
        occurred_at = _event_datetime(fact.get("occurred_at"))
        if occurred_at < projection_start or occurred_at >= complete_end:
            continue
        bucket_index = int((occurred_at - projection_start).total_seconds() // 3600)
        bucket = buckets[bucket_index]
        for field in _TOKEN_FIELDS:
            bucket[field] += int(fact.get(field) or 0)
        bucket["request_attempts"] += _request_attempts(fact)
        confidences[bucket_index].add(str(fact.get("measurement_confidence") or "unknown"))

    for bucket, confidence_values in zip(buckets, confidences, strict=True):
        bucket["prompt_tokens"] = (
            bucket["input_tokens"] + bucket["cache_read_tokens"] + bucket["cache_write_tokens"]
        )
        # Canonical output already includes reported reasoning; reasoning remains diagnostic.
        bucket["total_tokens"] = bucket["prompt_tokens"] + bucket["output_tokens"]
        prompt_tokens = bucket["prompt_tokens"]
        bucket["cache_hit_pct"] = _cache_hit_pct(bucket["cache_read_tokens"], prompt_tokens)
        bucket["measurement_confidence"] = _bucket_confidence(confidence_values)

    latest = buckets[-1]
    generated_at = _utc_text(generated)
    summary = {
        "updated_at": generated_at,
        "window_count": len(buckets),
        "total_tokens": sum(row["total_tokens"] for row in buckets),
        "request_attempts": sum(row["request_attempts"] for row in buckets),
        "latest_total_tokens": latest["total_tokens"],
        "latest_cache_hit_pct": latest["cache_hit_pct"],
    }
    ranking_facts = _ranking_facts(facts)
    payload = {
        "kind": PUBLIC_PROJECTION_KIND,
        "schema_version": 3,
        "source": public_source,
        "generated_at": generated_at,
        "bucket_minutes": 60,
        "rows": buckets,
        "provider_rows": _build_provider_rows(ranking_facts),
        "model_rows": _build_model_rows(ranking_facts),
        "harness_rows": _build_harness_rows(ranking_facts),
        "summary": summary,
    }
    validate_unified_public_projection(payload)
    return payload


def _require_exact_keys(value: Any, expected: frozenset[str], location: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{location} must contain exactly the allowlisted keys")
    return value


def _require_safe_int(value: Any, location: str, *, nonnegative: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or abs(value) > MAX_SAFE_INTEGER:
        raise ValueError(f"{location} must be an I-JSON safe integer")
    if nonnegative and value < 0:
        raise ValueError(f"{location} must be nonnegative")


def _require_percentage(value: Any, location: str, *, nullable: bool = True) -> None:
    if value is None:
        if nullable:
            return
        raise ValueError(f"{location} must be a percentage")
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 100
    ):
        raise ValueError(f"{location} must be null or a percentage")


def _require_confidence(value: Any, location: str) -> None:
    if not isinstance(value, str) or value not in _CONFIDENCE_VALUES:
        raise ValueError(f"{location} is invalid")


def _require_utc_timestamp(value: Any, location: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{location} must be a canonical UTC timestamp")
    try:
        normalized = canonical_timestamp(value)
    except ValidationError as exc:
        raise ValueError(f"{location} must be a canonical UTC timestamp") from exc
    if normalized != value or not value.endswith("Z"):
        raise ValueError(f"{location} must be a canonical UTC timestamp")
    return _event_datetime(value)


def _serialize_public_projection(payload: dict[str, Any]) -> bytes:
    """Serialize exactly as the atomic writer does, including final newline."""
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def validate_unified_public_projection(payload: Any) -> None:
    """Reject any projection that is not the exact public allowlist schema."""
    top = _require_exact_keys(payload, _TOP_LEVEL_KEYS, "projection")
    if (
        top["kind"] != PUBLIC_PROJECTION_KIND
        or isinstance(top["schema_version"], bool)
        or top["schema_version"] != 3
        or isinstance(top["bucket_minutes"], bool)
        or top["bucket_minutes"] != 60
    ):
        raise ValueError("projection contract marker is invalid")
    _validated_source(top["source"])
    generated_at = _require_utc_timestamp(top["generated_at"], "generated_at")
    rows = top["rows"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_PUBLIC_PROJECTION_HOURS:
        raise ValueError("projection rows must be an array of 1 through 168 buckets")
    previous_end: datetime | None = None
    for index, candidate in enumerate(rows):
        row = _require_exact_keys(candidate, _ROW_KEYS, f"rows[{index}]")
        window_start = _require_utc_timestamp(row["window_start"], f"rows[{index}].window_start")
        window_end = _require_utc_timestamp(row["window_end"], f"rows[{index}].window_end")
        if window_start.minute or window_start.second or window_start.microsecond:
            raise ValueError(f"rows[{index}].window_start must be on a UTC hour boundary")
        if window_end - window_start != timedelta(hours=1):
            raise ValueError(f"rows[{index}] must describe exactly one complete hour")
        if previous_end is not None and window_start != previous_end:
            raise ValueError("projection rows must be contiguous and chronological")
        previous_end = window_end
        for field in (*_TOKEN_FIELDS, "prompt_tokens", "total_tokens"):
            _require_safe_int(row[field], f"rows[{index}].{field}")
        _require_safe_int(row["request_attempts"], f"rows[{index}].request_attempts", nonnegative=True)
        _require_percentage(row["cache_hit_pct"], f"rows[{index}].cache_hit_pct")
        _require_confidence(
            row["measurement_confidence"], f"rows[{index}].measurement_confidence"
        )
        expected_prompt = row["input_tokens"] + row["cache_read_tokens"] + row["cache_write_tokens"]
        if row["prompt_tokens"] != expected_prompt:
            raise ValueError(f"rows[{index}].prompt_tokens does not match its token buckets")
        if row["total_tokens"] != expected_prompt + row["output_tokens"]:
            raise ValueError(f"rows[{index}].total_tokens does not match prompt plus output tokens")
        expected_cache_hit = _cache_hit_pct(row["cache_read_tokens"], expected_prompt)
        if row["cache_hit_pct"] != expected_cache_hit:
            raise ValueError(f"rows[{index}].cache_hit_pct does not match its token buckets")
    complete_end = generated_at.replace(minute=0, second=0, microsecond=0)
    if previous_end != complete_end:
        raise ValueError("projection rows must end at the latest complete UTC hour")

    provider_rows = top["provider_rows"]
    if not isinstance(provider_rows, list) or len(provider_rows) > 8:
        raise ValueError("provider_rows must be an array of at most 8 rows")
    for index, candidate in enumerate(provider_rows):
        row = _require_exact_keys(candidate, _PROVIDER_ROW_KEYS, f"provider_rows[{index}]")
        if not isinstance(row["label"], str) or row["label"] not in _PUBLIC_PROVIDER_LABELS:
            raise ValueError(f"provider_rows[{index}].label is invalid")
        _require_safe_int(row["total_tokens"], f"provider_rows[{index}].total_tokens")
        if row["total_tokens"] <= 0:
            raise ValueError(f"provider_rows[{index}].total_tokens must be positive")
        _require_safe_int(
            row["request_attempts"], f"provider_rows[{index}].request_attempts", nonnegative=True
        )
        _require_percentage(
            row["share_pct"], f"provider_rows[{index}].share_pct", nullable=False
        )
        _require_confidence(
            row["measurement_confidence"], f"provider_rows[{index}].measurement_confidence"
        )
    if len({row["label"] for row in provider_rows}) != len(provider_rows):
        raise ValueError("provider_rows labels must be unique")
    if provider_rows != sorted(provider_rows, key=lambda row: (-row["total_tokens"], row["label"])):
        raise ValueError("provider_rows must be sorted descending")

    model_rows = top["model_rows"]
    if not isinstance(model_rows, list) or len(model_rows) > 12:
        raise ValueError("model_rows must be an array of at most 12 rows")
    for index, candidate in enumerate(model_rows):
        row = _require_exact_keys(candidate, _MODEL_ROW_KEYS, f"model_rows[{index}]")
        if (
            not isinstance(row["provider_label"], str)
            or row["provider_label"] not in _PUBLIC_PROVIDER_LABELS
        ):
            raise ValueError(f"model_rows[{index}].provider_label is invalid")
        model_label = row["model_label"]
        if not isinstance(model_label, str):
            raise ValueError(f"model_rows[{index}].model_label is invalid")
        if model_label == "Other":
            if row["provider_label"] != "Other":
                raise ValueError("the model Other row must use provider_label Other")
        elif _public_model_label(model_label) != model_label:
            raise ValueError(f"model_rows[{index}].model_label is invalid")
        _require_safe_int(row["total_tokens"], f"model_rows[{index}].total_tokens")
        if row["total_tokens"] <= 0:
            raise ValueError(f"model_rows[{index}].total_tokens must be positive")
        _require_safe_int(
            row["request_attempts"], f"model_rows[{index}].request_attempts", nonnegative=True
        )
        _require_percentage(row["share_pct"], f"model_rows[{index}].share_pct", nullable=False)
        _require_confidence(
            row["measurement_confidence"], f"model_rows[{index}].measurement_confidence"
        )
    model_identities = [(row["provider_label"], row["model_label"]) for row in model_rows]
    if len(set(model_identities)) != len(model_identities):
        raise ValueError("model_rows identities must be unique")
    if model_rows != sorted(
        model_rows,
        key=lambda row: (-row["total_tokens"], row["provider_label"], row["model_label"]),
    ):
        raise ValueError("model_rows must be sorted descending")

    harness_rows = top["harness_rows"]
    if not isinstance(harness_rows, list) or len(harness_rows) > 4:
        raise ValueError("harness_rows must be an array of at most 4 rows")
    for index, candidate in enumerate(harness_rows):
        row = _require_exact_keys(candidate, _HARNESS_ROW_KEYS, f"harness_rows[{index}]")
        if not isinstance(row["label"], str) or row["label"] not in _PUBLIC_HARNESS_LABELS:
            raise ValueError(f"harness_rows[{index}].label is invalid")
        _require_safe_int(row["total_tokens"], f"harness_rows[{index}].total_tokens")
        if row["total_tokens"] <= 0:
            raise ValueError(f"harness_rows[{index}].total_tokens must be positive")
        _require_safe_int(
            row["request_attempts"], f"harness_rows[{index}].request_attempts", nonnegative=True
        )
        _require_percentage(row["share_pct"], f"harness_rows[{index}].share_pct", nullable=False)
        _require_confidence(
            row["measurement_confidence"], f"harness_rows[{index}].measurement_confidence"
        )
    if len({row["label"] for row in harness_rows}) != len(harness_rows):
        raise ValueError("harness_rows labels must be unique")
    if harness_rows != sorted(harness_rows, key=lambda row: (-row["total_tokens"], row["label"])):
        raise ValueError("harness_rows must be sorted descending")

    summary = _require_exact_keys(top["summary"], _SUMMARY_KEYS, "summary")
    _require_utc_timestamp(summary["updated_at"], "summary.updated_at")
    for field in ("window_count", "request_attempts"):
        _require_safe_int(summary[field], f"summary.{field}", nonnegative=True)
    for field in ("total_tokens", "latest_total_tokens"):
        _require_safe_int(summary[field], f"summary.{field}")
    _require_percentage(summary["latest_cache_hit_pct"], "summary.latest_cache_hit_pct")
    if summary["window_count"] != len(rows):
        raise ValueError("summary.window_count does not match rows")
    if summary["updated_at"] != top["generated_at"]:
        raise ValueError("summary.updated_at does not match generated_at")
    if summary["total_tokens"] != sum(row["total_tokens"] for row in rows):
        raise ValueError("summary.total_tokens does not match rows")
    if summary["request_attempts"] != sum(row["request_attempts"] for row in rows):
        raise ValueError("summary.request_attempts does not match rows")
    latest = rows[-1]
    if summary["latest_total_tokens"] != latest["total_tokens"]:
        raise ValueError("summary.latest_total_tokens does not match the latest row")
    if summary["latest_cache_hit_pct"] != latest["cache_hit_pct"]:
        raise ValueError("summary.latest_cache_hit_pct does not match the latest row")

    for location, comparison_rows in (
        ("provider_rows", provider_rows),
        ("model_rows", model_rows),
        ("harness_rows", harness_rows),
    ):
        ranking_total = sum(row["total_tokens"] for row in comparison_rows)
        if ranking_total != summary["total_tokens"]:
            raise ValueError(f"{location} total_tokens must match summary.total_tokens")
        comparison_requests = sum(row["request_attempts"] for row in comparison_rows)
        if comparison_requests > summary["request_attempts"]:
            raise ValueError(
                f"{location} request_attempts must not exceed summary.request_attempts"
            )
        for index, row in enumerate(comparison_rows):
            expected_share = _share_pct(row["total_tokens"], ranking_total)
            if row["share_pct"] != expected_share:
                raise ValueError(f"{location}[{index}].share_pct does not match total_tokens")

    if len(_serialize_public_projection(top)) >= _MAX_PUBLIC_PROJECTION_BYTES:
        raise ValueError("public projection must remain smaller than 1 MiB")


def write_unified_public_projection(
    ledger_path: str | Path,
    projection_path: str | Path,
    *,
    quota_ledger_path: str | Path | None = None,
    hours: int = DEFAULT_PUBLIC_PROJECTION_HOURS,
    source: str = DEFAULT_PUBLIC_PROJECTION_SOURCE,
    now: datetime | None = None,
) -> Path:
    """Validate and atomically replace one same-directory public JSON artifact."""
    output = Path(projection_path).expanduser()
    private_inputs = [Path(ledger_path).expanduser()]
    if quota_ledger_path is not None:
        private_inputs.append(Path(quota_ledger_path).expanduser())
    try:
        for private_input in private_inputs:
            aliases_input = output.resolve(strict=False) == private_input.resolve(strict=False)
            if output.exists() and private_input.exists():
                aliases_input = aliases_input or os.path.samefile(output, private_input)
            if aliases_input:
                raise ValueError("public projection output must not be the same file as its private input")
    except OSError as exc:
        raise ValueError("could not establish that public output differs from private input") from exc
    payload = build_unified_public_projection(
        ledger_path,
        quota_ledger_path=quota_ledger_path,
        hours=hours,
        source=source,
        now=now,
    )
    validate_unified_public_projection(payload)
    serialized = _serialize_public_projection(payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        directory_fd = os.open(output.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return output
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
