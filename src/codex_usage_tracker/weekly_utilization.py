"""Weekly allowance utilization: how much of each primary weekly quota is
in use now, and how much was left unused at each observed reset.

Keeper request 2026-09-14 (Discord 1531875202860974264, message
1549243774775853067): "what percentage of my weekly windows I am using ...
how much I am leaving on the table, aggregate, and per provider". This is a
display metric over the quota ledger's existing observations — no routing,
no policy, no new storage.

Semantics (shared contract with the frontend, ``docs/weekly-utilization-
semantics.md``):

* One primary weekly quota per account (``_PRIMARY_WEEKLY_QUOTAS``). Any
  other weekly quota of the same family (``seven_day_fable``) is a
  ``model_cap`` detail row and never enters a denominator.
* Every aggregate is the arithmetic mean of ACCOUNT percentages
  (``equal_account``): two Claude accounts count twice, one Codex account
  once, and no account is weighted by how many cycles it happened to sample.
* Completed cycles are inferred from observed reset targets, clustered
  within five minutes of absolute time; a null reset never creates a cycle.
  A cycle reports the last valid sample observed at/before its reset — a
  polling estimate, not the provider's terminal figure; later samples that
  still carry the old target belong to the next window — and is eligible
  only when the rollover was observed at/after the boundary and that sample
  fell within three hours of it. Everything else stays visible with reasons.
* Missing, stale or invalid data is excluded and counted, never read as 0%.

The module is pure: rows in, JSON-ready dict out, with ``generated_at`` and
the account roster supplied by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .dashboard import (
    _PRIMARY_WEEKLY_QUOTAS,
    _PRIVATE_LEDGER_MAX_AGE_SECONDS,
    _QUOTA_PROVIDER_LABELS,
    _QUOTA_PROVIDER_ORDER,
    _QuotaAccountIdentity,
    _canonical_quota_provider,
    _fact_datetime,
    _iso_z,
    _quota_account_identity,
    _quota_observation_order_key,
    _quota_row_confidence,
    _quota_utilization_pct,
    _rfc3339_order_key,
)

WEEKS_DEFAULT = 4
WEEKS_MIN = 1
WEEKS_MAX = 12
WEIGHTING = "equal_account"
# Reset targets flap by seconds across minute/day boundaries; targets this
# close are one boundary. The same tolerance admits a rollover observed just
# before its nominal boundary.
_RESET_TOLERANCE = timedelta(minutes=5)
# A cycle's last sample must fall this close before its reset (the ledger's
# own freshness threshold) to stand in for the terminal reading.
_TAIL_WINDOW = timedelta(seconds=_PRIVATE_LEDGER_MAX_AGE_SECONDS)
_COUNTER_DROP_POINTS = Decimal("1")
_HUNDRED = Decimal("100")
_ONE_DECIMAL = Decimal("0.1")
# The one public account label ("Claude 1"), matching the frontend's
# accountDisplayLabel: bare short name for a single account.
_SHORT_LABELS = {
    "anthropic": "Claude",
    "openai-codex": "Codex",
    "opencode-go": "OpenCode Go",
    "z-ai-glm": "Z.AI",
    "kimi-k3-coding": "Kimi K3",
}


def weekly_lookback_days(weeks: int) -> int:
    """Read one extra week so the oldest requested cycle has its start."""
    return 7 * (weeks + 1)


@dataclass(frozen=True)
class _Sample:
    observed_at: datetime
    used: Decimal | None
    resets_at: datetime | None
    confidence: str


@dataclass
class _Cluster:
    anchor: datetime
    first_observed: datetime
    target: datetime
    samples: list[_Sample] = field(default_factory=list)


def _pct_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(_ONE_DECIMAL, rounding=ROUND_HALF_UP))


def _mean(values: list[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / len(values) if values else None


def _pair(used: Decimal | None) -> tuple[str | None, str | None]:
    return _pct_text(used), (None if used is None else _pct_text(_HUNDRED - used))


def _clusters(samples: list[_Sample]) -> list[_Cluster]:
    """Group observations by the reset boundary they report. A null-reset
    sample joins the open cluster only while that boundary is still ahead,
    so a mid-window drop it carries stays visible without a post-reset null
    ever being mistaken for the old window."""
    clusters: list[_Cluster] = []
    current: _Cluster | None = None
    for sample in samples:
        if sample.resets_at is None:
            if current is not None and sample.observed_at <= current.anchor + _RESET_TOLERANCE:
                current.samples.append(sample)
            continue
        if current is not None and abs(sample.resets_at - current.anchor) <= _RESET_TOLERANCE:
            current.samples.append(sample)
            current.target = sample.resets_at
            continue
        current = _Cluster(sample.resets_at, sample.observed_at, sample.resets_at, [sample])
        clusters.append(current)
    return clusters


def _cycle(cluster: _Cluster, successor: _Cluster | None, *, now: datetime) -> dict[str, Any] | None:
    """Judge one inferred period; ``None`` means it is the open window."""
    target = cluster.target
    reasons: list[str] = []
    if successor is None:
        if target > now - _RESET_TOLERANCE:
            return None
        reasons.append("unconfirmed_completion")
    elif successor.first_observed < target - _RESET_TOLERANCE:
        reasons.append("early_boundary_replacement")
    elif successor.anchor <= target + _RESET_TOLERANCE:
        reasons.append("target_not_forward")
    # Only readings taken at/before the target describe this window. A
    # sample observed after it — still carrying the old target for a few
    # minutes, or a null target — is the next window's counter: it neither
    # stands in for the last pre-reset reading nor reads as a mid-window
    # drop. Nothing is synthesized when no such reading exists.
    valid = [
        sample
        for sample in cluster.samples
        if sample.used is not None and sample.observed_at <= target
    ]
    last = valid[-1] if valid else None
    seconds_before: float | None = None
    if last is None:
        reasons.append("no_valid_sample")
    else:
        seconds_before = (target - last.observed_at).total_seconds()
        if "early_boundary_replacement" not in reasons and seconds_before > _TAIL_WINDOW.total_seconds():
            reasons.append("missing_tail")
    if any(
        earlier.used - later.used > _COUNTER_DROP_POINTS
        for earlier, later in zip(valid, valid[1:])
    ):
        reasons.append("counter_decreased")
    used_text, remaining_text = _pair(None if last is None else last.used)
    return {
        "resets_at": _iso_z(target),
        "last_observed_at": None if last is None else _iso_z(last.observed_at),
        "used_pct": used_text,
        "remaining_pct": remaining_text,
        "confidence": "unknown" if last is None else last.confidence,
        "eligible": not reasons,
        "reasons": reasons,
        "last_sample_seconds_before_reset": None if seconds_before is None else int(seconds_before),
        "_used": None if last is None else last.used,
    }


def _current(samples: list[_Sample], *, now: datetime) -> dict[str, Any]:
    valid = [sample for sample in samples if sample.used is not None]
    latest = valid[-1] if valid else None
    if not samples:
        status = "no_observations"
    elif latest is None:
        status = "no_utilization_ratio"
    elif now - latest.observed_at > _TAIL_WINDOW:
        status = "stale"
    elif latest.resets_at is not None and latest.observed_at < latest.resets_at <= now:
        status = "reset_passed_since_observation"
    else:
        status = "fresh"
    used_text, remaining_text = _pair(None if latest is None else latest.used)
    return {
        "used_pct": used_text,
        "remaining_pct": remaining_text,
        "observed_at": None if latest is None else _iso_z(latest.observed_at),
        "resets_at": None if latest is None or latest.resets_at is None else _iso_z(latest.resets_at),
        "confidence": "unknown" if latest is None else latest.confidence,
        "status": status,
        "eligible": status == "fresh",
        "_used": None if latest is None else latest.used,
    }


def _summary(
    windows: list[dict[str, Any]],
    contribution: Any,
) -> dict[str, Any]:
    """Equal-account mean over the primary windows. ``contribution`` maps a
    window to ``(used, estimated, cycles)`` or ``None`` when it is excluded."""
    per_provider: dict[str, dict[str, Any]] = {
        provider: {"used": [], "excluded": 0, "estimated": 0, "cycles": 0}
        for provider in _QUOTA_PROVIDER_ORDER
        if provider in _PRIMARY_WEEKLY_QUOTAS
    }
    for window in windows:
        if window["role"] != "primary":
            continue
        bucket = per_provider.setdefault(
            window["provider"], {"used": [], "excluded": 0, "estimated": 0, "cycles": 0}
        )
        contributed = contribution(window)
        if contributed is None:
            bucket["excluded"] += 1
            continue
        used, estimated, cycles = contributed
        bucket["used"].append(used)
        bucket["estimated"] += int(estimated)
        bucket["cycles"] += cycles

    def render(used: list[Decimal], excluded: int, estimated: int, cycles: int) -> dict[str, Any]:
        used_text, remaining_text = _pair(_mean(used))
        return {
            "used_pct": used_text,
            "remaining_pct": remaining_text,
            "account_count": len(used),
            "excluded_account_count": excluded,
            "estimated_account_count": estimated,
            "cycle_count": cycles,
        }

    providers = [
        {"provider": provider, "label": _QUOTA_PROVIDER_LABELS.get(provider, provider), **render(**bucket)}
        for provider, bucket in per_provider.items()
    ]
    return {
        **render(
            [value for bucket in per_provider.values() for value in bucket["used"]],
            sum(bucket["excluded"] for bucket in per_provider.values()),
            sum(bucket["estimated"] for bucket in per_provider.values()),
            sum(bucket["cycles"] for bucket in per_provider.values()),
        ),
        "providers": providers,
    }


def build_weekly_utilization(
    rows: list[dict[str, Any]],
    *,
    generated_at: datetime,
    weeks: int,
    account_indexes: dict[str, dict[_QuotaAccountIdentity, int]],
) -> dict[str, Any]:
    """Project quota observations into the weekly-utilization payload.

    ``account_indexes`` is the authoritative roster (the 168-hour
    subscriptions window): accounts outside it are ignored rather than
    inserted, so a retired or quiet account can never renumber a live one,
    and every roster account gets a primary window row even with no data.
    """
    grouped: dict[tuple[str, str, _QuotaAccountIdentity], list[tuple[Any, _Sample]]] = {}
    source_rows = invalid_rows = outside_roster = 0
    for row in rows:
        provider = _canonical_quota_provider(row.get("provider"))
        primary = _PRIMARY_WEEKLY_QUOTAS.get(provider)
        quota_name = str(row.get("quota_name") or "unknown")
        if primary is None or not (quota_name == primary or quota_name.startswith(f"{primary}_")):
            continue
        account = _quota_account_identity(row)
        if account not in account_indexes.get(provider, {}):
            outside_roster += 1
            continue
        source_rows += 1
        observed_key = _rfc3339_order_key(row.get("observed_at"))
        if observed_key is None:
            invalid_rows += 1
            continue
        observed_at = _fact_datetime(row.get("observed_at"))
        if observed_at > generated_at:
            continue
        pct = _quota_utilization_pct(row)
        if pct is None:
            invalid_rows += 1
        reset_key = _rfc3339_order_key(row.get("resets_at"))
        sample = _Sample(
            observed_at=observed_at,
            used=None if pct is None else Decimal(str(pct)),
            resets_at=None if reset_key is None else _fact_datetime(row.get("resets_at")),
            confidence=_quota_row_confidence(row),
        )
        grouped.setdefault((provider, quota_name, account), []).append(
            (_quota_observation_order_key(row), sample)
        )

    keys = set(grouped)
    for provider, primary in _PRIMARY_WEEKLY_QUOTAS.items():
        for account in account_indexes.get(provider, {}):
            keys.add((provider, primary, account))

    def provider_rank(provider: str) -> int:
        return _QUOTA_PROVIDER_ORDER.index(provider) if provider in _QUOTA_PROVIDER_ORDER else len(_QUOTA_PROVIDER_ORDER)

    windows: list[dict[str, Any]] = []
    excluded_cycles = 0
    for provider, quota_name, account in sorted(
        keys,
        key=lambda key: (
            provider_rank(key[0]),
            key[0],
            account_indexes[key[0]][key[2]],
            key[1] != _PRIMARY_WEEKLY_QUOTAS[key[0]],
            key[1],
        ),
    ):
        samples = [sample for _order, sample in sorted(grouped.get((provider, quota_name, account), []), key=lambda item: item[0])]
        clusters = _clusters(samples)
        cycles = [
            cycle
            for index, cluster in enumerate(clusters)
            if (cycle := _cycle(cluster, clusters[index + 1] if index + 1 < len(clusters) else None, now=generated_at))
            is not None
        ]
        cycles.sort(key=lambda cycle: cycle["resets_at"], reverse=True)
        used_cycles: list[dict[str, Any]] = []
        for cycle in cycles:
            if cycle["eligible"] and len(used_cycles) < weeks:
                used_cycles.append(cycle)
            elif cycle["eligible"]:
                cycle["eligible"] = False
                cycle["reasons"] = ["beyond_requested_weeks"]
        excluded_cycles += len(cycles) - len(used_cycles)
        completed_used = _mean([cycle["_used"] for cycle in used_cycles])
        used_text, remaining_text = _pair(completed_used)
        index = account_indexes[provider][account]
        count = len(account_indexes[provider])
        short = _SHORT_LABELS.get(provider, _QUOTA_PROVIDER_LABELS.get(provider, provider))
        windows.append(
            {
                "provider": provider,
                "label": f"{short} {index}" if count > 1 else short,
                "provider_account_index": index,
                "provider_account_count": count,
                "quota_name": quota_name,
                "role": "primary" if quota_name == _PRIMARY_WEEKLY_QUOTAS[provider] else "model_cap",
                "current": _current(samples, now=generated_at),
                "completed": {
                    "used_pct": used_text,
                    "remaining_pct": remaining_text,
                    "cycle_count": len(used_cycles),
                    "excluded_cycle_count": len(cycles) - len(used_cycles),
                },
                "cycles": cycles,
                "_completed_used": completed_used,
                "_completed_estimated": any(cycle["confidence"] != "exact" for cycle in used_cycles),
            }
        )

    current = _summary(
        windows,
        lambda window: (
            (window["current"]["_used"], window["current"]["confidence"] != "exact", 0)
            if window["current"]["eligible"]
            else None
        ),
    )
    completed = _summary(
        windows,
        lambda window: (
            (window["_completed_used"], window["_completed_estimated"], window["completed"]["cycle_count"])
            if window["_completed_used"] is not None
            else None
        ),
    )
    for window in windows:
        del window["_completed_used"], window["_completed_estimated"], window["current"]["_used"]
        for cycle in window["cycles"]:
            del cycle["_used"]

    # The roster also carries accounts with no weekly quota (OpenRouter,
    # DeepSeek); only accounts of a primary-weekly provider have a window.
    tracked_accounts = sum(
        len(accounts) for provider, accounts in account_indexes.items() if provider in _PRIMARY_WEEKLY_QUOTAS
    )
    notes = [
        "Average weekly allowance used is the arithmetic mean of account percentages "
        f"(equal_account): each of the {tracked_accounts} tracked primary weekly accounts counts "
        "once; model caps are detail rows outside every aggregate.",
        "Current readings older than 3h, without a usable ratio, or observed before a reset "
        "that has since passed are excluded and counted, never read as 0% used.",
        "Completed cycles are inferred from observed reset targets (clustered within 5 minutes); "
        "each reports the last sample observed within 3h before its reset — a polling estimate, "
        f"not the provider's terminal figure. The newest {weeks} eligible cycles per account count.",
    ]
    if outside_roster:
        notes.append(
            f"{outside_roster} observations from accounts outside the current 168h roster were ignored."
        )
    return {
        "generated_at": _iso_z(generated_at),
        "weeks": weeks,
        "lookback_days": weekly_lookback_days(weeks),
        "weighting": WEIGHTING,
        "current": current,
        "completed": completed,
        "windows": windows,
        "coverage": {
            "source_rows": source_rows,
            "invalid_rows": invalid_rows,
            "excluded_cycles": excluded_cycles,
        },
        "notes": notes,
    }
