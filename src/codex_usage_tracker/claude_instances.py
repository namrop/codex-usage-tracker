"""Validated, migration-safe Claude Code instance declarations."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, cast


_CLAUDE_SOURCE_SUFFIX_RE = re.compile(r"^claude-code(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class ClaudeInstance:
    """One Claude Code account/configuration and its stable source suffix."""

    source_suffix: str
    config_dir: str | Path

    def __post_init__(self) -> None:
        suffix = _validated_suffix(self.source_suffix)
        config_dir = _validated_config_dir(self.config_dir)
        object.__setattr__(self, "source_suffix", suffix)
        object.__setattr__(self, "config_dir", config_dir)

    @property
    def source_key(self) -> str:
        tail = self.source_suffix.removeprefix("claude-code").removeprefix("-")
        return "claude_code" + (f"_{tail.replace('-', '_')}" if tail else "")

    @property
    def quota_source_key(self) -> str:
        return f"{self.source_key}_quota"

    @property
    def account_ref(self) -> str | None:
        # Preserve the historical primary series, whose account_ref is null.
        return None if self.source_suffix == "claude-code" else self.source_suffix

    @property
    def quota_config_dir(self) -> Path | None:
        """Return the custom config environment root used by quota probes.

        The primary account intentionally uses Claude Code's default split
        layout (``~/.claude`` plus ``~/.claude.json``), which is not equivalent
        to setting ``CLAUDE_CONFIG_DIR=~/.claude``. Only named secondary
        instances use their declared root as a custom configuration directory.
        """
        return (
            None
            if self.source_suffix == "claude-code"
            else cast(Path, self.config_dir)
        )

    def usage_namespace(self, source_prefix: str) -> str:
        return f"{source_prefix}:{self.source_suffix}"

    def quota_namespace(self, source_prefix: str) -> str:
        return f"{source_prefix}:{self.source_suffix}-quota"

    def probe_dir(self, base: str | Path) -> Path:
        target = Path(base).expanduser()
        if self.source_suffix == "claude-code":
            return target
        tail = self.source_suffix.removeprefix("claude-code-")
        return target.with_name(f"{target.name}-{tail}")


def _validated_suffix(value: Any) -> str:
    if not isinstance(value, str) or not _CLAUDE_SOURCE_SUFFIX_RE.fullmatch(value):
        raise ValueError(
            "Claude source suffix must be claude-code or claude-code- followed by "
            "lowercase alphanumeric hyphen-separated segments"
        )
    return value


def _validated_config_dir(value: Any) -> Path:
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise ValueError("Claude config directory must be a nonempty path") from exc
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise ValueError("Claude config directory must be a nonempty path")
    try:
        target = Path(raw).expanduser()
    except (KeyError, RuntimeError) as exc:
        raise ValueError("Claude config directory could not be expanded") from exc
    if not target.is_absolute():
        raise ValueError("Claude config directory must be absolute or user-home expandable")
    return target


def parse_claude_instance_declaration(value: str) -> ClaudeInstance:
    """Parse one ``SOURCE_SUFFIX=CONFIG_DIR`` command-line declaration."""
    if not isinstance(value, str) or "=" not in value:
        raise ValueError("Claude instance must use SOURCE_SUFFIX=CONFIG_DIR")
    source_suffix, config_dir = value.split("=", 1)
    return ClaudeInstance(source_suffix, config_dir)


def normalize_claude_instances(
    values: Iterable[ClaudeInstance | Mapping[str, Any] | tuple[Any, Any]]
    | Mapping[str, Any]
    | None,
) -> tuple[ClaudeInstance, ...]:
    """Normalize direct-call structures and reject duplicate stable suffixes."""
    if values is None:
        return ()
    if isinstance(values, Mapping):
        candidates: Iterable[Any] = values.items()
    else:
        candidates = values

    normalized: list[ClaudeInstance] = []
    seen: set[str] = set()
    for candidate in candidates:
        if isinstance(candidate, ClaudeInstance):
            instance = candidate
        elif isinstance(candidate, Mapping):
            if "source_suffix" not in candidate or "config_dir" not in candidate:
                raise ValueError(
                    "Claude instance mappings require source_suffix and config_dir"
                )
            instance = ClaudeInstance(
                candidate["source_suffix"], candidate["config_dir"]
            )
        else:
            if not isinstance(candidate, tuple) or len(candidate) != 2:
                raise ValueError(
                    "Claude instances must contain source_suffix/config_dir pairs"
                )
            source_suffix, config_dir = candidate
            instance = ClaudeInstance(cast(str, source_suffix), config_dir)
        if instance.source_suffix in seen:
            raise ValueError(f"duplicate Claude source suffix: {instance.source_suffix}")
        seen.add(instance.source_suffix)
        normalized.append(instance)
    return tuple(normalized)
