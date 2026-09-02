"""Screen-driving rules for the Claude Code ``/usage`` TUI probe.

Every dialog Claude Code draws highlights its selected entry with the same
cursor glyph as the composer, so the probe has to recognise the composer
itself, pick "Yes" in the trust prompt explicitly, and fail fast on screens it
cannot drive past. These tests replay captured 2.1.257 screens.
"""

from __future__ import annotations

import pytest

from codex_usage_tracker import quota as quota_module

USAGE_SCREEN = """
   Current session
   █████████████████                                  34% used
   Resets 3:40pm (America/New_York)

   Current week (all models)
   ███████████████████████▌                           47% used
   Resets Jul 16, 8pm (America/New_York)
"""

COMPOSER_SCREEN = """
 ▐▛███▛█   Claude Code v2.1.257
⚠ Safe mode: all customizations are disabled
──────────────────────────────────────────────────
❯ Try "how do I log an error?"
──────────────────────────────────────────────────
  ⏵⏵ auto mode on (shift+tab to cycle)
"""

TRUST_SCREEN = """
 Accessing workspace:
 /home/luis/.local/state/codex-usage-tracker/claude-probe
 Quick safety check: Is this a project you created or one you trust?
 Claude Code'll be able to read, edit, and execute files here.
 ❯ No, exit
   Yes, I trust this folder
 Enter to confirm · Esc to cancel
"""

LOGIN_SCREEN = """
 Select login method:
 ❯ 1. Claude account with subscription · Pro, Max, Team, or Enterprise
   2. Anthropic Console account · API usage billing
 Enter to confirm · Esc to exit
"""

THEME_SCREEN = """
 Choose the text style that looks best with your terminal
 ❯ 2. Dark mode ✔
   1. Light mode
"""

# Claude Code 2.1.257 opening ``/usage`` as a Settings dialog while the usage
# endpoint is rate limited (persisted probe screen, 2026-09-02). No composer,
# no "Current session"; each attempt used to burn its full timeout.
RATE_LIMITED_USAGE_SCREEN = """
 ▐▛███▛█   Claude Code v2.1.257
▝▜██████▀  Opus 5 (1M context) · Claude Max
  ▝▝ ▝▝    ~/.local/state/codex-usage-tracker/claude-probe-secondary

⚠ Safe mode: all customizations are disabled (CLAUDE.md, skills, plugins, hooks, MCP, agents, and more)
  Restart without --safe-mode to re-enable
⚠ Your login expires in 2 days · run /login to renew

● Remote Control disconnected — /login

▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔
   Settings  Status   Config   Usage   Stats

   Session

   Total cost:            $0.0000
   Total duration (API):  0s
   Total duration (wall): 0s
   Total code changes:    0 lines added, 0 lines removed
   Usage:                 0 input, 0 output, 0 cache read, 0 cache write

   Error: Usage endpoint is rate limited. Please try again in a moment.

   r to retry · Esc to cancel
"""

GENERIC_DIALOG_ERROR_SCREEN = RATE_LIMITED_USAGE_SCREEN.replace(
    "Error: Usage endpoint is rate limited. Please try again in a moment.",
    "Error: Something else went wrong.",
)


def _drive(monkeypatch, screens, *, timeout=1.0):
    sent: list[tuple[str, ...]] = []
    frames = iter(screens)
    last = {"screen": ""}

    def capture(session):
        try:
            last["screen"] = next(frames)
        except StopIteration:
            pass
        return last["screen"]

    class Completed:
        stdout = ""

    monkeypatch.setattr(quota_module.subprocess, "run", lambda *a, **k: Completed())
    monkeypatch.setattr(quota_module, "_tmux_capture", capture)
    monkeypatch.setattr(quota_module, "_tmux_keys", lambda session, *keys: sent.append(keys))
    monkeypatch.setattr(quota_module.time, "sleep", lambda seconds: None)
    return sent


def test_composer_is_recognised_and_menus_are_not():
    assert quota_module._claude_prompt_ready(COMPOSER_SCREEN)
    assert quota_module._claude_prompt_ready("❯")
    assert quota_module._claude_prompt_ready("❯ ")
    assert not quota_module._claude_prompt_ready(LOGIN_SCREEN)
    assert not quota_module._claude_prompt_ready(THEME_SCREEN)
    assert not quota_module._claude_prompt_ready(TRUST_SCREEN)


def test_trust_prompt_selects_yes_rather_than_the_highlighted_exit(tmp_path, monkeypatch):
    sent = _drive(monkeypatch, [TRUST_SCREEN, COMPOSER_SCREEN, USAGE_SCREEN])
    screen = quota_module.capture_claude_code_usage_screen(probe_dir=tmp_path / "probe")
    assert "Current session" in screen
    assert sent[0] == ("Down", "Enter")
    assert sent[1] == ("/usage", "Enter")


def test_trust_prompt_confirms_when_yes_is_already_highlighted(tmp_path, monkeypatch):
    highlighted_yes = TRUST_SCREEN.replace(" ❯ No, exit", "   No, exit").replace(
        "   Yes, I trust this folder", " ❯ Yes, I trust this folder"
    )
    sent = _drive(monkeypatch, [highlighted_yes, COMPOSER_SCREEN, USAGE_SCREEN])
    quota_module.capture_claude_code_usage_screen(probe_dir=tmp_path / "probe")
    assert sent[0] == ("Enter",)


@pytest.mark.parametrize(
    ("screen", "reason"),
    [
        (LOGIN_SCREEN, "login required"),
        (THEME_SCREEN, "first-run onboarding"),
        (RATE_LIMITED_USAGE_SCREEN, "usage endpoint rate limited"),
        (GENERIC_DIALOG_ERROR_SCREEN, "usage dialog error: Something else went wrong."),
    ],
)
def test_unresolvable_screens_fail_fast_and_persist_the_screen(tmp_path, monkeypatch, screen, reason):
    sent = _drive(monkeypatch, [screen, screen])
    probe = tmp_path / "probe"
    with pytest.raises(quota_module.ClaudeProbeBlocked, match=reason):
        quota_module.capture_claude_code_usage_screen(probe_dir=probe)
    assert ("/usage", "Enter") not in sent
    persisted = tmp_path / ".probe.last-screen.txt"
    assert persisted.read_text(encoding="utf-8").strip() == screen.strip()
    assert (persisted.stat().st_mode & 0o777) == 0o600


def test_blocked_probe_is_not_retried(tmp_path, monkeypatch):
    calls = []

    def blocked(**kwargs):
        calls.append(kwargs)
        raise quota_module.ClaudeProbeBlocked("Claude Code /usage probe blocked: login required")

    monkeypatch.setattr(quota_module, "capture_claude_code_usage_screen", blocked)
    with pytest.raises(quota_module.ClaudeProbeBlocked):
        quota_module.capture_claude_code_usage_screen_with_retry(probe_dir=tmp_path / "probe")
    assert len(calls) == 1


def test_exited_cli_reports_blocked_instead_of_timeout(tmp_path, monkeypatch):
    _drive(monkeypatch, [COMPOSER_SCREEN])

    def gone(session):
        raise quota_module.subprocess.CalledProcessError(1, ["tmux", "capture-pane"])

    monkeypatch.setattr(quota_module, "_tmux_capture", gone)
    with pytest.raises(quota_module.ClaudeProbeBlocked, match="exited"):
        quota_module.capture_claude_code_usage_screen(probe_dir=tmp_path / "probe")


def test_swallowed_usage_command_is_resent_from_the_composer(tmp_path, monkeypatch):
    monkeypatch.setattr(quota_module, "CLAUDE_USAGE_RESEND_AFTER", 0.0)
    sent = _drive(monkeypatch, [COMPOSER_SCREEN, COMPOSER_SCREEN, USAGE_SCREEN])
    quota_module.capture_claude_code_usage_screen(probe_dir=tmp_path / "probe")
    assert sent.count(("/usage", "Enter")) == 2


def test_timeout_persists_the_last_screen(tmp_path, monkeypatch):
    _drive(monkeypatch, [COMPOSER_SCREEN, COMPOSER_SCREEN])
    monkeypatch.setattr(quota_module, "CLAUDE_USAGE_MAX_SENDS", 1)
    with pytest.raises(TimeoutError, match="did not become ready"):
        quota_module.capture_claude_code_usage_screen(probe_dir=tmp_path / "probe", timeout=0.05)
    assert "Try \"how do I log an error?\"" in (tmp_path / ".probe.last-screen.txt").read_text(encoding="utf-8")


def test_persisted_screen_drops_url_lines(tmp_path):
    probe = tmp_path / "probe"
    probe.mkdir()
    quota_module._persist_probe_screen(probe, "Select login method\nOpen https://claude.ai/oauth?code=secret\n ❯ 1. Claude account")
    text = (tmp_path / ".probe.last-screen.txt").read_text(encoding="utf-8")
    assert "secret" not in text and "Select login method" in text


def test_usage_dialog_errors_block_but_healthy_screens_do_not():
    assert quota_module._claude_probe_blocker(RATE_LIMITED_USAGE_SCREEN) == "usage endpoint rate limited"
    assert quota_module._claude_probe_blocker(GENERIC_DIALOG_ERROR_SCREEN) == (
        "usage dialog error: Something else went wrong."
    )
    # The rate-limited dialog hides the composer, so the resend path can never fire.
    assert not quota_module._claude_prompt_ready(RATE_LIMITED_USAGE_SCREEN)
    for healthy in (USAGE_SCREEN, COMPOSER_SCREEN, TRUST_SCREEN):
        assert quota_module._claude_probe_blocker(healthy) is None
    # "Error:" outside the usage dialog (e.g. a transcript line) is not a blocker.
    assert quota_module._claude_probe_blocker(COMPOSER_SCREEN + "\n  Error: earlier tool call failed\n") is None
