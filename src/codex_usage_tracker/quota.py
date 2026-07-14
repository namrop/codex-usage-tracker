"""Canonical quota observations for subscription and direct-provider accounts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from typing import Any, Iterable, Mapping
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .canonical_ledger import ValidationError, canonical_timestamp, decimal_string, normalize_fact
from .ledger import reconcile_snapshot_windows

OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
GO_WINDOWS: tuple[tuple[str, timedelta, Decimal], ...] = (
    ("five_hour", timedelta(hours=5), Decimal("12")),
    ("week", timedelta(days=7), Decimal("30")),
    ("month", timedelta(days=30), Decimal("60")),
)


def _now(value: str | datetime | None = None) -> str:
    if value is None:
        value = datetime.now(timezone.utc).isoformat()
    if isinstance(value, datetime):
        value = value.isoformat()
    return canonical_timestamp(value)


def _quota(
    *,
    namespace: str,
    observation_id: str,
    harness: str,
    observed_at: str | datetime,
    provider: str,
    quota_name: str,
    window_kind: str = "unknown",
    unit: str = "provider_unit",
    confidence: str = "exact",
    limit: Any = None,
    remaining: Any = None,
    used: Any = None,
    resets_at: Any = None,
    window_started_at: Any = None,
    window_ends_at: Any = None,
    **extensions: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "fact_type": "quota_observation_v1",
        "schema_version": 1,
        "source_namespace": namespace,
        "source_observation_id": observation_id,
        "harness": harness,
        "observed_at": _now(observed_at),
        "provider": provider,
        "quota_name": quota_name,
        "quota_scope": "account",
        "window_kind": window_kind,
        "window_started_at": window_started_at,
        "window_ends_at": window_ends_at,
        "resets_at": resets_at,
        "limit_value": decimal_string(limit, "limit_value"),
        "remaining_value": decimal_string(remaining, "remaining_value"),
        "used_value": decimal_string(used, "used_value"),
        "unit": unit,
        "measurement_confidence": confidence,
        "provider_payload_ref": None,
    }
    if any(not key.startswith("x_") for key in extensions):
        raise ValidationError("quota extensions must use x_ namespaced fields")
    row.update(extensions)
    return normalize_fact(row)


def _remaining_percent(used: Any) -> str | None:
    value = decimal_string(used, "used_value")
    if value is None:
        return None
    return decimal_string(max(Decimal("0"), Decimal("100") - Decimal(value)), "remaining_value")


def _reset(value: Any) -> str | None:
    if value is None:
        return None
    return canonical_timestamp(value)


def codex_quota_observations(
    ledger_path: str | Path,
    source_namespace: str,
) -> list[dict[str, Any]]:
    """Project the newest legacy Codex snapshot into secret-free quota facts."""
    path = Path(ledger_path).expanduser()
    if not path.exists():
        return []
    snapshots: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fp:
        for number, line in enumerate(fp, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: malformed JSON: {exc.msg}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{number}: snapshot must be an object")
            snapshots.append(reconcile_snapshot_windows(value))
    if not snapshots:
        return []
    snapshot = max(
        snapshots,
        key=lambda item: _parse_dt(item.get("fetched_at")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    observed = snapshot.get("fetched_at")
    if not observed:
        raise ValueError(f"{path}: newest snapshot has no fetched_at")
    snapshot_id = str(snapshot.get("id") or _now(observed))
    definitions = (
        ("five_hour", "session_used_pct", "session_reset_at"),
        ("week", "weekly_used_pct", "weekly_reset_at"),
    )
    rows = [
        _quota(
            namespace=source_namespace,
            observation_id=f"{snapshot_id}:{name}",
            harness="codex",
            observed_at=observed,
            provider="openai",
            quota_name=name,
            window_kind="rolling",
            unit="percent",
            limit="100",
            used=snapshot.get(used_field),
            remaining=_remaining_percent(snapshot.get(used_field)),
            resets_at=_reset(snapshot.get(reset_field)),
            x_plan_type=snapshot.get("plan_type"),
        )
        for name, used_field, reset_field in definitions
    ]
    # Spark is an independent pair of limits when present in newer snapshots.
    if "spark_session_used_pct" in snapshot or "spark_weekly_used_pct" in snapshot:
        for name, used_field, reset_field in (
            ("spark_five_hour", "spark_session_used_pct", "spark_session_reset_at"),
            ("spark_week", "spark_weekly_used_pct", "spark_weekly_reset_at"),
        ):
            rows.append(
                _quota(
                    namespace=source_namespace,
                    observation_id=f"{snapshot_id}:{name}",
                    harness="codex",
                    observed_at=observed,
                    provider="openai",
                    quota_name=name,
                    window_kind="rolling",
                    unit="percent",
                    limit="100",
                    used=snapshot.get(used_field),
                    remaining=_remaining_percent(snapshot.get(used_field)),
                    resets_at=_reset(snapshot.get(reset_field)),
                    x_plan_type=snapshot.get("plan_type"),
                )
            )
    return rows


def _payload(response: Any) -> Mapping[str, Any]:
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, Mapping):
        raise ValueError("provider response must be a JSON object")
    return value


def collect_openrouter_quota(
    api_key: str,
    source_namespace: str,
    *,
    observed_at: str | datetime | None = None,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Fetch OpenRouter account credits and the active API-key quota directly."""
    observed = _now(observed_at)
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(timeout=timeout) as client:
        credits_payload = _payload(client.get(OPENROUTER_CREDITS_URL, headers=headers))
        key_payload = _payload(client.get(OPENROUTER_KEY_URL, headers=headers))
    credits = credits_payload.get("data") if isinstance(credits_payload.get("data"), Mapping) else credits_payload
    key = key_payload.get("data") if isinstance(key_payload.get("data"), Mapping) else key_payload
    total = credits.get("total_credits")
    spent = credits.get("total_usage")
    balance = None
    if total is not None and spent is not None:
        balance = decimal_string(Decimal(str(total)) - Decimal(str(spent)), "remaining_value")
    rows = [
        _quota(
            namespace=source_namespace,
            observation_id=f"{observed}:credit_balance",
            harness="provider_api",
            observed_at=observed,
            provider="openrouter",
            quota_name="credit_balance",
            window_kind="lifetime",
            unit="credits",
            limit=total,
            remaining=balance,
            used=spent,
        ),
        _quota(
            namespace=source_namespace,
            observation_id=f"{observed}:api_key_quota",
            harness="provider_api",
            observed_at=observed,
            provider="openrouter",
            quota_name="api_key_quota",
            window_kind="unknown",
            unit="credits",
            limit=key.get("limit"),
            remaining=key.get("limit_remaining"),
            used=key.get("usage"),
        ),
    ]
    return rows


def collect_deepseek_quota(
    api_key: str,
    source_namespace: str,
    *,
    observed_at: str | datetime | None = None,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Fetch DeepSeek's account balance without retaining credentials or raw data."""
    observed = _now(observed_at)
    with httpx.Client(timeout=timeout) as client:
        payload = _payload(
            client.get(
                DEEPSEEK_BALANCE_URL,
                headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
            )
        )
    balances = payload.get("balance_infos")
    if not isinstance(balances, list):
        balances = []
    rows: list[dict[str, Any]] = []
    for index, balance in enumerate(balances):
        if not isinstance(balance, Mapping):
            continue
        currency = str(balance.get("currency") or "provider_unit").lower()
        unit = "usd" if currency == "usd" else "provider_unit"
        rows.append(
            _quota(
                namespace=source_namespace,
                observation_id=f"{observed}:balance:{index}:{currency}",
                harness="provider_api",
                observed_at=observed,
                provider="deepseek",
                quota_name="account_balance",
                window_kind="lifetime",
                unit=unit,
                remaining=balance.get("total_balance"),
                x_currency=currency.upper(),
                x_is_available=payload.get("is_available"),
                x_granted_balance=decimal_string(balance.get("granted_balance"), "granted_balance"),
                x_topped_up_balance=decimal_string(balance.get("topped_up_balance"), "topped_up_balance"),
            )
        )
    return rows


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def derive_opencode_go_quotas(
    usage_facts: Iterable[Mapping[str, Any]],
    source_namespace: str,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Estimate Go subscription windows from OpenCode's local quota-cost values."""
    observed_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    facts = [row for row in usage_facts if row.get("provider") == "opencode-go"]
    harnesses = sorted({str(row.get("harness")) for row in facts if row.get("x_opencode_quota_cost_usd") is not None})
    rows: list[dict[str, Any]] = []
    for name, duration, limit in GO_WINDOWS:
        start = observed_dt - duration
        used = Decimal("0")
        valued_events = 0
        for fact in facts:
            occurred = _parse_dt(fact.get("occurred_at"))
            if occurred is None or occurred < start or occurred > observed_dt:
                continue
            value = fact.get("x_opencode_quota_cost_usd")
            if value is None:
                continue
            try:
                used += Decimal(str(value))
            except InvalidOperation:
                continue
            valued_events += 1
        rows.append(
            _quota(
                namespace=source_namespace,
                observation_id=f"{_now(observed_dt)}:{name}",
                harness="opencode",
                observed_at=observed_dt,
                provider="opencode-go",
                quota_name=name,
                window_kind="rolling",
                unit="usd",
                confidence="estimated",
                limit=limit,
                used=used,
                remaining=max(Decimal("0"), limit - used),
                window_started_at=start.isoformat(),
                window_ends_at=observed_dt.isoformat(),
                x_coverage={
                    "status": "partial",
                    "harnesses_with_values": harnesses,
                    "valued_events": valued_events,
                },
            )
        )
    return rows


_CLAUDE_USAGE_WINDOWS = (
    ("Current session", "five_hour"),
    ("Current week (all models)", "seven_day"),
    ("Current week (Fable)", "seven_day_fable"),
)


def _claude_usage_values(screen: str) -> dict[str, tuple[Decimal, str]]:
    """Parse each Claude ``/usage`` section without crossing heading boundaries."""
    lines = screen.splitlines()
    labels = {label for label, _ in _CLAUDE_USAGE_WINDOWS}
    headings = [(index, line.strip()) for index, line in enumerate(lines) if line.strip() in labels]
    by_label = {label: index for index, label in headings}
    for required in ("Current session", "Current week (all models)"):
        if required not in by_label:
            raise ValueError(f"Claude Code /usage output omitted {required}")

    values: dict[str, tuple[Decimal, str]] = {}
    ordered = sorted(headings)
    for position, (start, label) in enumerate(ordered):
        end = ordered[position + 1][0] if position + 1 < len(ordered) else len(lines)
        block = "\n".join(lines[start + 1 : end])
        used_match = re.search(r"(\d+(?:\.\d+)?)% used", block)
        reset_match = re.search(r"^\s*Resets\s+(.+?)\s*$", block, re.MULTILINE)
        if not used_match or not reset_match:
            raise ValueError(f"Claude Code /usage section {label} is incomplete")
        quota_name = dict(_CLAUDE_USAGE_WINDOWS)[label]
        values[quota_name] = (Decimal(used_match.group(1)), reset_match.group(1))
    return values


def _unlock_close(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _tmux_capture(session_name: str) -> str:
    completed = subprocess.run(
        ["tmux", "capture-pane", "-t", session_name, "-p", "-S", "-120"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    return completed.stdout


def _tmux_keys(session_name: str, *keys: str) -> None:
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, *keys],
        check=True,
        capture_output=True,
        text=True,
        timeout=5.0,
    )


def capture_claude_code_usage_screen(
    *,
    claude_command: str | Path = "claude",
    probe_dir: str | Path,
    timeout: float = 25.0,
) -> str:
    """Open Claude Code's authenticated TUI and capture its built-in ``/usage`` view.

    The probe runs in safe mode, performs no model turn, and reuses one stable
    session ID in a tracker-owned private directory so repeated collections do
    not flood Claude Code's session history.
    """
    raw_target = Path(probe_dir).expanduser()
    if raw_target.is_symlink():
        raise ValueError("Claude Code probe directory must not be a symlink")
    raw_target.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = raw_target.resolve()
    if target.stat().st_uid != os.getuid():
        raise ValueError("Claude Code probe directory must be owned by the current user")
    os.chmod(target, 0o700)
    lock_path = target.with_name(f".{target.name}.lock")
    lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        lock_flags |= os.O_NOFOLLOW
    elif lock_path.is_symlink():
        raise ValueError("Claude Code probe lock must not be a symlink")
    try:
        lock_fd = os.open(lock_path, lock_flags, 0o600)
    except OSError as exc:
        if lock_path.is_symlink():
            raise ValueError("Claude Code probe lock must not be a symlink") from exc
        raise
    lock_stat = os.fstat(lock_fd)
    if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.getuid():
        os.close(lock_fd)
        raise ValueError("Claude Code probe lock must be a current-user regular file")
    os.fchmod(lock_fd, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    if any(target.iterdir()):
        _unlock_close(lock_fd)
        raise ValueError("Claude Code probe directory must be empty")
    stable_session_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex-usage-tracker:{target}"))
    tmux_session = f"claude-usage-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    command = [
        "tmux", "new-session", "-d", "-s", tmux_session,
        "-x", "140", "-y", "50", "-c", str(target),
        str(claude_command), "--safe-mode", "--session-id", stable_session_id,
    ]
    started = time.monotonic()
    usage_sent = False
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=5.0)
        while time.monotonic() - started < timeout:
            screen = _tmux_capture(tmux_session)
            if "Quick safety check" in screen and "Yes, I trust this folder" in screen:
                _tmux_keys(tmux_session, "Enter")
                time.sleep(0.5)
                continue
            if not usage_sent and "❯" in screen:
                _tmux_keys(tmux_session, "/usage", "Enter")
                usage_sent = True
                time.sleep(0.5)
                continue
            if usage_sent and "Current session" in screen and "Current week (all models)" in screen:
                try:
                    _claude_usage_values(screen)
                except ValueError:
                    time.sleep(0.25)
                    continue
                return screen
            time.sleep(0.25)
        raise TimeoutError("Claude Code /usage screen did not become ready")
    finally:
        try:
            _tmux_keys(tmux_session, "Escape")
            _tmux_keys(tmux_session, "/exit", "Enter")
        except Exception:
            pass
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", tmux_session],
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
        except Exception:
            pass
        finally:
            _unlock_close(lock_fd)


def _claude_reset_at(reset_text: str, observed: datetime) -> str | None:
    match = re.fullmatch(r"\s*(.+?)\s*\(([^()]+)\)\s*", reset_text)
    if not match:
        return None
    clock_text, timezone_name = match.groups()
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return None
    local_observed = observed.astimezone(zone)
    for date_format in ("%b %d, %I:%M%p", "%b %d, %I%p"):
        try:
            parsed = datetime.strptime(
                f"{local_observed.year} {clock_text}", f"%Y {date_format}"
            )
        except ValueError:
            continue
        candidate = parsed.replace(tzinfo=zone)
        if candidate < local_observed - timedelta(days=30):
            candidate = candidate.replace(year=candidate.year + 1)
        return canonical_timestamp(candidate.isoformat())
    for time_format in ("%I:%M%p", "%I%p"):
        try:
            parsed = datetime.strptime(clock_text, time_format)
        except ValueError:
            continue
        candidate = local_observed.replace(
            hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0
        )
        if candidate <= local_observed:
            candidate += timedelta(days=1)
        return canonical_timestamp(candidate.isoformat())
    return None


def collect_claude_code_quota(
    source_namespace: str,
    *,
    claude_command: str | Path = "claude",
    probe_dir: str | Path,
    timeout: float = 25.0,
    observed_at: str | datetime | None = None,
) -> list[dict[str, Any]]:
    """Collect subscription limits from Claude Code CLI's own ``/usage`` view."""
    observed = _now(observed_at)
    observed_dt = _parse_dt(observed)
    if observed_dt is None:
        raise ValueError("observed_at must be a timezone-aware timestamp")
    screen = capture_claude_code_usage_screen(
        claude_command=claude_command,
        probe_dir=probe_dir,
        timeout=timeout,
    )
    rows: list[dict[str, Any]] = []
    values = _claude_usage_values(screen)
    for quota_name, (used, reset_text) in values.items():
        resets_at = _claude_reset_at(reset_text, observed_dt)
        if resets_at is None:
            raise ValueError(f"Claude Code /usage reset time for {quota_name} is unparseable")
        rows.append(
            _quota(
                namespace=source_namespace,
                observation_id=f"{observed}:{quota_name}",
                harness="claude_code",
                observed_at=observed,
                provider="anthropic",
                quota_name=quota_name,
                window_kind="rolling",
                unit="percent",
                confidence="exact",
                limit=Decimal("100"),
                used=used,
                remaining=max(Decimal("0"), Decimal("100") - used),
                resets_at=resets_at,
                x_source_surface="claude_code_cli_usage",
            )
        )
    return rows


# Naming aliases used by collector integrations.
collect_codex_quota = codex_quota_observations
collect_opencode_go_quota = derive_opencode_go_quotas
