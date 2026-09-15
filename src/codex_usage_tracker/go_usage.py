"""OpenCode Go weighted-usage explanation: what consumed the shared weekly
allowance this week, by model.

Keeper request 2026-09-14 (Discord 1531875202860974264, message
1549262223074009089): explain what consumes the shared Go allowance. This
is a read-only display projection over rows the dashboard route supplies —
no routing, no policy, no new storage, no ledger rewrite.

Verified native semantics (public docs https://opencode.ai/docs/go/#usage-limits
and the upstream handler, captured 2026-09-15T03:31Z):

* ONE shared weighted pool across Go models. Each request's raw model cost
  (list rate per token) is multiplied by the model's cost multiplier and
  debited from the pool; the weekly pool is 30 normalized units ("$30 of
  1x-model usage"), reset Monday 00:00 UTC.
* The native usage API reports each window as a WHOLE percent:
  ``floor(min(100, used / limit * 100))``, ``100`` when rate-limited. That
  integer is the provider's own figure and is the headline here; a percent
  ``p`` below 100 stands for the band ``[p, p + 1)``, and ``100`` means "at
  least 100%" with no exact remainder claimable.

The model rows are a CURRENT-TARIFF ESTIMATE: this week's canonical tokens
priced at the tariff table below (verified for the model actually in use)
and weighted by the model multiplier, expressed in quota units and as a
percent of the 30-unit pool. They are never rescaled to the native total,
never called a bill, and any model the table does not know stays unpriced
with its tokens still counted. Reasoning tokens are diagnostic only —
canonical ``output_tokens`` already include them.

The tariff lives HERE (not in list_pricing) because a financial list-price
equivalent and a quota-unit debit are different quantities that happen to
share the same per-token rate today.

The module is pure: rows in, JSON-ready dict out (Decimal strings, ints,
bools, ISO-8601 Z timestamps), ``generated_at`` supplied by the caller.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any

PROVIDER = "opencode-go"
ACCOUNT_LABEL = "OpenCode Go"
NATIVE_QUOTA_NAME = "week"
# The native GET /zen/go/v1/usage collector marks its rows with this surface;
# the older cookie-backed server-function surface is not accepted here.
NATIVE_SOURCE_SURFACE = "opencode_go_usage_api"
NATIVE_PERCENT_RESOLUTION = 1
NATIVE_FRESH_SECONDS = 3 * 3600
NATIVE_BASIS = "provider_reported"
MODELED_BASIS = "current_tariff_estimate"
WEEKLY_LIMIT_QUOTA_UNITS = Decimal("30")

# Verified 2026-09-15T03:31Z from the public Go docs for the one model in
# use this week. Rates are USD per million tokens; the multiplier is the
# quota weight (a 4x model debits four normalized units per list dollar).
# Models not listed are reported as unpriced — no default multiplier is
# ever guessed.
GO_TARIFF: dict[str, Any] = {
    "as_of": "2026-09-15T03:31:00Z",
    "source_url": "https://opencode.ai/docs/go/#usage-limits",
    "basis": MODELED_BASIS,
    "weekly_limit_quota_units": str(WEEKLY_LIMIT_QUOTA_UNITS),
    "models": {
        "glm-5.3": {
            "input_usd_per_m": "1.40",
            "output_usd_per_m": "4.40",
            "cache_read_usd_per_m": "0.26",
            "cache_write_usd_per_m": "0",
            "multiplier": "4",
        },
    },
}

_TOKEN_BUCKETS = (
    ("input_tokens", "input_usd_per_m"),
    ("cache_read_tokens", "cache_read_usd_per_m"),
    ("cache_write_tokens", "cache_write_usd_per_m"),
    ("output_tokens", "output_usd_per_m"),
)
_TOKEN_FIELDS = tuple(field for field, _ in _TOKEN_BUCKETS)
_MILLION = Decimal("1000000")
_HUNDRED = Decimal("100")
_PCT_PLACES = Decimal("0.001")
_USD_PLACES = Decimal("0.00000001")
_UNKNOWN_MODEL = "unknown"

_USAGE_IGNORE_REASONS = (
    "malformed", "other_fact_type", "other_provider", "invalid_timestamp", "before_window", "future",
)
_QUOTA_IGNORE_REASONS = (
    "malformed", "other_fact_type", "other_provider", "other_quota_name", "other_source_surface",
    "other_unit", "invalid_timestamp", "before_window", "future", "missing_percent",
    "non_integer_percent", "out_of_range",
)


# ── small pure helpers ─────────────────────────────────────────────────────


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso_z(value: datetime) -> str:
    value = value.astimezone(timezone.utc)
    if value.microsecond:
        return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _week_start(moment: datetime) -> datetime:
    day = moment.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _pct_text(value: Decimal) -> str:
    return str(value.quantize(_PCT_PLACES, rounding=ROUND_HALF_EVEN))


def _usd_text(value: Decimal) -> str:
    return str(value.quantize(_USD_PLACES, rounding=ROUND_HALF_EVEN))


def _model_key(row: Mapping[str, Any]) -> str:
    for field in ("model_reported", "model_requested"):
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return _UNKNOWN_MODEL


# ── usage rows → per-model accumulators ────────────────────────────────────


class _ModelAccumulator:
    __slots__ = ("model", "event_count", "correction_count", "missing_token_event_count", "tokens", "reasoning")

    def __init__(self, model: str) -> None:
        self.model = model
        self.event_count = 0
        self.correction_count = 0
        self.missing_token_event_count = 0
        self.tokens = {field: 0 for field in _TOKEN_FIELDS}
        self.reasoning = 0

    def add(self, row: Mapping[str, Any]) -> None:
        self.event_count += 1
        if row.get("record_kind") == "correction":
            self.correction_count += 1
        missing = False
        for field in _TOKEN_FIELDS:
            value = _int_or_none(row.get(field))
            if value is None:
                missing = True
                continue
            self.tokens[field] += value
        if missing:
            self.missing_token_event_count += 1
        reasoning = _int_or_none(row.get("reasoning_tokens"))
        if reasoning is not None:
            self.reasoning += reasoning


def _collect_usage(
    usage_rows: Iterable[Any], window_start: datetime, generated_at: datetime,
    native_observed_at: datetime | None,
) -> tuple[dict[str, _ModelAccumulator], dict[str, Any]]:
    """Accumulate this week's counted rows by model. Rows after
    ``generated_at`` are future and never counted — so they are never
    reported as activity after the native reading either."""
    ignored = {reason: 0 for reason in _USAGE_IGNORE_REASONS}
    seen: set[tuple[str, str]] = set()
    models: dict[str, _ModelAccumulator] = {}
    source_rows = 0
    duplicates = 0
    counted = 0
    latest_usage_at: datetime | None = None
    after_native = 0
    for row in usage_rows:
        source_rows += 1
        if not isinstance(row, Mapping):
            ignored["malformed"] += 1
            continue
        if row.get("fact_type") != "usage_event_v1":
            ignored["other_fact_type"] += 1
            continue
        provider = row.get("provider")
        if not isinstance(provider, str) or provider.strip().lower() != PROVIDER:
            ignored["other_provider"] += 1
            continue
        occurred = _parse_dt(row.get("occurred_at"))
        if occurred is None:
            ignored["invalid_timestamp"] += 1
            continue
        if occurred < window_start:
            ignored["before_window"] += 1
            continue
        if occurred > generated_at:
            ignored["future"] += 1
            continue
        identity = (str(row.get("source_namespace")), str(row.get("source_event_id")))
        if identity in seen:
            duplicates += 1
            continue
        seen.add(identity)
        counted += 1
        if latest_usage_at is None or occurred > latest_usage_at:
            latest_usage_at = occurred
        if native_observed_at is not None and occurred > native_observed_at:
            after_native += 1
        key = _model_key(row)
        accumulator = models.get(key)
        if accumulator is None:
            accumulator = models[key] = _ModelAccumulator(key)
        accumulator.add(row)
    coverage = {
        "source_usage_rows": source_rows,
        "usage_rows_in_window": counted,
        "duplicate_rows": duplicates,
        "correction_rows": sum(acc.correction_count for acc in models.values()),
        "missing_token_rows": sum(acc.missing_token_event_count for acc in models.values()),
        # Cutoff of the counted usage: the latest occurred_at that made it in.
        "latest_usage_at": None if latest_usage_at is None else _iso_z(latest_usage_at),
        # Counted rows occurring strictly after the native reading; null
        # when there is no native reading to measure against.
        "usage_rows_after_native": None if native_observed_at is None else after_native,
        "ignored_usage_rows": ignored,
    }
    return models, coverage


def _price_model(accumulator: _ModelAccumulator) -> tuple[dict[str, Any], Decimal | None]:
    """Display row plus the UNROUNDED quota units (None when unpriced). The
    exact Decimal never enters the payload; it feeds the reconciliation."""
    tariff = GO_TARIFF["models"].get(accumulator.model)
    total_tokens = sum(accumulator.tokens.values())
    row: dict[str, Any] = {
        "model": accumulator.model,
        "event_count": accumulator.event_count,
        "correction_count": accumulator.correction_count,
        "missing_token_event_count": accumulator.missing_token_event_count,
        **{field: accumulator.tokens[field] for field in _TOKEN_FIELDS},
        "total_tokens": total_tokens,
        "reasoning_tokens": accumulator.reasoning,
        "multiplier": None,
        "estimated_list_cost_usd": None,
        "estimated_quota_units": None,
        "estimated_quota_pct": None,
        "share_of_modeled_pct": None,
        "pricing_status": "unpriced",
        "basis": MODELED_BASIS,
    }
    if tariff is None:
        return row, None
    cost = Decimal("0")
    for field, rate_field in _TOKEN_BUCKETS:
        cost += Decimal(accumulator.tokens[field]) * Decimal(tariff[rate_field]) / _MILLION
    multiplier = Decimal(tariff["multiplier"])
    units = cost * multiplier
    pct = units / WEEKLY_LIMIT_QUOTA_UNITS * _HUNDRED
    row.update(
        multiplier=tariff["multiplier"],
        estimated_list_cost_usd=_usd_text(cost),
        estimated_quota_units=_usd_text(units),
        estimated_quota_pct=_pct_text(pct),
        pricing_status="priced",
    )
    return row, units


def _model_rows(models: Mapping[str, _ModelAccumulator]) -> tuple[list[dict[str, Any]], Decimal]:
    """Sorted display rows and the exact (unrounded) priced quota units."""
    priced_rows = [_price_model(acc) for acc in models.values()]
    rows = [row for row, _ in priced_rows]
    exact_units = sum((units for _, units in priced_rows if units is not None), Decimal("0"))
    priced_units = sum(
        (Decimal(row["estimated_quota_units"]) for row in rows if row["pricing_status"] == "priced"),
        Decimal("0"),
    )
    for row in rows:
        if row["pricing_status"] == "priced" and priced_units > 0:
            row["share_of_modeled_pct"] = _pct_text(Decimal(row["estimated_quota_units"]) / priced_units * _HUNDRED)
    rows.sort(
        key=lambda row: (
            0 if row["pricing_status"] == "priced" else 1,
            -Decimal(row["estimated_quota_units"] or "0"),
            -row["total_tokens"],
            row["model"],
        )
    )
    return rows, exact_units


def _totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    priced = [row for row in rows if row["pricing_status"] == "priced"]
    unpriced = [row for row in rows if row["pricing_status"] != "priced"]
    totals: dict[str, Any] = {
        "event_count": sum(row["event_count"] for row in rows),
        "priced_event_count": sum(row["event_count"] for row in priced),
        "unpriced_event_count": sum(row["event_count"] for row in unpriced),
        "model_count": len(rows),
        "priced_model_count": len(priced),
        "unpriced_model_count": len(unpriced),
        **{field: sum(row[field] for row in rows) for field in _TOKEN_FIELDS},
        "total_tokens": sum(row["total_tokens"] for row in rows),
        "reasoning_tokens": sum(row["reasoning_tokens"] for row in rows),
        # Additive over the priced rows exactly as printed, so the rows and
        # the total never disagree by a rounding hair.
        "estimated_list_cost_usd": _usd_text(sum((Decimal(row["estimated_list_cost_usd"]) for row in priced), Decimal("0"))),
        "estimated_quota_units": _usd_text(sum((Decimal(row["estimated_quota_units"]) for row in priced), Decimal("0"))),
        "estimated_quota_pct": _pct_text(sum((Decimal(row["estimated_quota_pct"]) for row in priced), Decimal("0"))),
        "weekly_limit_quota_units": str(WEEKLY_LIMIT_QUOTA_UNITS),
        "basis": MODELED_BASIS,
    }
    return totals


# ── quota rows → the one native weekly reading ─────────────────────────────


def _missing_native() -> dict[str, Any]:
    return {
        "percent": None,
        "status": None,
        "observed_at": None,
        "resets_at": None,
        "fresh": False,
        "age_seconds": None,
        "capped": None,
        "percent_resolution": NATIVE_PERCENT_RESOLUTION,
        "source_surface": NATIVE_SOURCE_SURFACE,
        "basis": NATIVE_BASIS,
    }


def _native_percent(value: Any) -> tuple[int | None, str | None]:
    """Whole-point percent from a canonical decimal string, or the reason it
    is unusable."""
    if value is None:
        return None, "missing_percent"
    if isinstance(value, bool):
        return None, "non_integer_percent"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None, "non_integer_percent"
    if not number.is_finite() or number != number.to_integral_value():
        return None, "non_integer_percent"
    percent = int(number)
    if percent < 0 or percent > 100:
        return None, "out_of_range"
    return percent, None


def _select_native(
    quota_rows: Iterable[Any], window_start: datetime, generated_at: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    ignored = {reason: 0 for reason in _QUOTA_IGNORE_REASONS}
    source_rows = 0
    candidates: list[tuple[tuple[datetime, str], dict[str, Any]]] = []
    for row in quota_rows:
        source_rows += 1
        if not isinstance(row, Mapping):
            ignored["malformed"] += 1
            continue
        if row.get("fact_type") != "quota_observation_v1":
            ignored["other_fact_type"] += 1
            continue
        provider = row.get("provider")
        if not isinstance(provider, str) or provider.strip().lower() != PROVIDER:
            ignored["other_provider"] += 1
            continue
        if row.get("quota_name") != NATIVE_QUOTA_NAME:
            ignored["other_quota_name"] += 1
            continue
        if row.get("x_source_surface") != NATIVE_SOURCE_SURFACE:
            ignored["other_source_surface"] += 1
            continue
        if row.get("unit") != "percent":
            ignored["other_unit"] += 1
            continue
        observed = _parse_dt(row.get("observed_at"))
        if observed is None:
            ignored["invalid_timestamp"] += 1
            continue
        if observed < window_start:
            ignored["before_window"] += 1
            continue
        if observed > generated_at:
            ignored["future"] += 1
            continue
        percent, reason = _native_percent(row.get("used_value"))
        if reason is not None:
            ignored[reason] += 1
            continue
        assert percent is not None
        age = int((generated_at - observed).total_seconds())
        resets = _parse_dt(row.get("resets_at"))
        state = row.get("x_provider_state")
        native = {
            "percent": percent,
            "status": state if isinstance(state, str) and state else None,
            "observed_at": _iso_z(observed),
            "resets_at": None if resets is None else _iso_z(resets),
            "fresh": age < NATIVE_FRESH_SECONDS,
            "age_seconds": age,
            "capped": percent >= 100,
            "percent_resolution": NATIVE_PERCENT_RESOLUTION,
            "source_surface": NATIVE_SOURCE_SURFACE,
            "basis": NATIVE_BASIS,
        }
        candidates.append(((observed, str(row.get("source_observation_id"))), native))
    coverage = {
        "source_quota_rows": source_rows,
        "native_candidates": len(candidates),
        "ignored_quota_rows": ignored,
    }
    if not candidates:
        return _missing_native(), coverage
    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1], coverage


# ── reconciliation: modeled points against the native band ────────────────


def _reconcile(
    native: Mapping[str, Any], totals: Mapping[str, Any], *, complete: bool, exact_units: Decimal,
    latest_usage_at: str | None, usage_ahead: bool | None,
) -> dict[str, Any]:
    """Classify the modeled figure against the native band, in priority
    order: native_missing, native_stale, native_capped, modeled_incomplete,
    usage_ahead_of_native, then the band test. Only the band test may
    claim a rounding match or a numeric gap, and it uses the UNROUNDED
    quota units, never the 3-dp display percent."""
    result: dict[str, Any] = {
        "status": "native_missing",
        "native_pct": native["percent"],
        "native_lower_pct": None,
        "native_upper_pct": None,
        "modeled_pct": totals["estimated_quota_pct"],
        "modeled_quota_units": totals["estimated_quota_units"],
        "modeled_complete": complete,
        "latest_usage_at": latest_usage_at,
        "native_observed_at": native["observed_at"],
        "usage_ahead_of_native": usage_ahead,
        "matches_within_rounding": None,
        "band_gap_pct": None,
        "native_basis": NATIVE_BASIS,
        "modeled_basis": MODELED_BASIS,
    }
    percent = native["percent"]
    if percent is None:
        return result
    lower = Decimal(percent)
    capped = percent >= 100
    result["native_lower_pct"] = str(lower)
    result["native_upper_pct"] = None if capped else str(lower + 1)
    if not native["fresh"]:
        result["status"] = "native_stale"
        return result
    if capped:
        result["status"] = "native_capped"
        return result
    if not complete:
        result["status"] = "modeled_incomplete"
        return result
    if usage_ahead:
        result["status"] = "usage_ahead_of_native"
        return result
    upper = lower + 1
    # Compare in quota units (finite decimals: tokens × a 2-dp rate) so the
    # band edges are exact; the printed gap is derived from the same value.
    lower_units = lower * WEEKLY_LIMIT_QUOTA_UNITS / _HUNDRED
    upper_units = upper * WEEKLY_LIMIT_QUOTA_UNITS / _HUNDRED
    exact_pct = exact_units / WEEKLY_LIMIT_QUOTA_UNITS * _HUNDRED
    if exact_units < lower_units:
        result["status"] = "modeled_below"
        result["matches_within_rounding"] = False
        result["band_gap_pct"] = _pct_text(exact_pct - lower)
    elif exact_units >= upper_units:
        result["status"] = "modeled_above"
        result["matches_within_rounding"] = False
        result["band_gap_pct"] = _pct_text(exact_pct - upper)
    else:
        result["status"] = "within_rounding"
        result["matches_within_rounding"] = True
    return result


def _notes(
    native: Mapping[str, Any], reconciliation: Mapping[str, Any], rows: list[dict[str, Any]], coverage: Mapping[str, Any],
) -> list[str]:
    notes = [
        "Native weekly percent is provider-reported at whole-point resolution: floor-rounded and capped at 100, "
        "so a value p below 100 stands for the band [p, p+1).",
        f"Model rows are a current-tariff estimate: this week's canonical tokens priced at the tariff verified "
        f"{GO_TARIFF['as_of']} and weighted by each model's multiplier over the {WEEKLY_LIMIT_QUOTA_UNITS}-unit weekly pool. "
        "Not a measured charge and not a proven quota debit; the schedule could have changed within the week.",
        "Reasoning tokens are shown for diagnosis only; canonical output tokens already include them.",
    ]
    if native["percent"] is None:
        notes.append("No native weekly reading from the Go usage API this week; the native figure is unknown, not zero.")
    elif not native["fresh"]:
        notes.append(
            f"The latest native reading is stale (older than {NATIVE_FRESH_SECONDS // 3600}h); it is shown but not compared."
        )
    elif native["capped"]:
        notes.append("Native reports at least 100% (capped); no exact remainder can be claimed against the modeled figure.")
    elif reconciliation["status"] == "modeled_incomplete":
        notes.append(
            f"Modeled coverage is incomplete ({coverage['unpriced_events']} unpriced events, "
            f"{coverage['missing_token_rows']} events with missing token fields); the priced figure is shown but "
            "no rounding match or gap is claimed against the native percent."
        )
    elif reconciliation["status"] == "usage_ahead_of_native":
        notes.append(
            f"Counted usage runs through {reconciliation['latest_usage_at']}, after the native reading observed at "
            f"{reconciliation['native_observed_at']}; the two totals have different cutoffs, so no rounding match "
            "or gap is claimed."
        )
    elif reconciliation["status"] == "within_rounding":
        notes.append("Modeled points fall inside the native whole-point band; the difference is within rounding.")
    else:
        notes.append(
            "Modeled points fall outside the native whole-point band; the gap stays visible and the rows are not rescaled."
        )
    if reconciliation["usage_ahead_of_native"] and reconciliation["status"] != "usage_ahead_of_native":
        notes.append(
            f"Counted usage runs through {reconciliation['latest_usage_at']}, after the native reading observed at "
            f"{reconciliation['native_observed_at']}; the two cutoffs differ."
        )
    unpriced = [row for row in rows if row["pricing_status"] != "priced"]
    if unpriced:
        names = ", ".join(row["model"] for row in unpriced)
        events = sum(row["event_count"] for row in unpriced)
        notes.append(
            f"{events} events on {len(unpriced)} unpriced model(s) ({names}) carry no multiplier in the tariff table; "
            "their tokens are counted but not valued."
        )
    if coverage["missing_token_rows"]:
        notes.append(
            f"{coverage['missing_token_rows']} events are missing one or more token fields; present buckets are counted "
            "and the missing ones are unknown."
        )
    return notes


# ── entry point ────────────────────────────────────────────────────────────


def build_go_usage(
    usage_rows: Iterable[Any], quota_rows: Iterable[Any], *, generated_at: datetime,
) -> dict[str, Any]:
    """Project this UTC week's OpenCode Go usage by model against the native
    weekly percent. ``generated_at`` must be timezone-aware; rows after it
    are ignored as future."""
    if not isinstance(generated_at, datetime) or generated_at.tzinfo is None:
        raise ValueError("generated_at must be a timezone-aware datetime")
    generated_at = generated_at.astimezone(timezone.utc)
    window_start = _week_start(generated_at)
    window_end = window_start + timedelta(days=7)

    native, quota_coverage = _select_native(quota_rows if quota_rows is not None else (), window_start, generated_at)
    native_observed_at = _parse_dt(native["observed_at"])
    models, usage_coverage = _collect_usage(
        usage_rows if usage_rows is not None else (), window_start, generated_at, native_observed_at,
    )
    rows, exact_units = _model_rows(models)
    totals = _totals(rows)
    complete = totals["unpriced_event_count"] == 0 and usage_coverage["missing_token_rows"] == 0
    after_native = usage_coverage["usage_rows_after_native"]
    reconciliation = _reconcile(
        native, totals, complete=complete, exact_units=exact_units,
        latest_usage_at=usage_coverage["latest_usage_at"],
        usage_ahead=None if after_native is None else after_native > 0,
    )
    coverage = {
        **usage_coverage,
        "unpriced_events": totals["unpriced_event_count"],
        "unpriced_models": sorted(row["model"] for row in rows if row["pricing_status"] != "priced"),
        **quota_coverage,
    }
    return {
        "generated_at": _iso_z(generated_at),
        "window": {"started_at": _iso_z(window_start), "ends_at": _iso_z(window_end), "as_of": _iso_z(generated_at)},
        "account": {
            "provider": PROVIDER,
            "label": ACCOUNT_LABEL,
            "provider_account_index": 1,
            "provider_account_count": 1,
        },
        "native": native,
        "models": rows,
        "totals": totals,
        "reconciliation": reconciliation,
        "coverage": coverage,
        "tariff": {
            "as_of": GO_TARIFF["as_of"],
            "source_url": GO_TARIFF["source_url"],
            "basis": MODELED_BASIS,
            "weekly_limit_quota_units": str(WEEKLY_LIMIT_QUOTA_UNITS),
            "models": {name: dict(entry) for name, entry in GO_TARIFF["models"].items()},
        },
        "notes": _notes(native, reconciliation, rows, coverage),
    }
