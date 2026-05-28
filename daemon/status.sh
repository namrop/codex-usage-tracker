#!/usr/bin/env bash
set -euo pipefail

echo "LaunchAgent status:"
launchctl list | grep com.lux.codex || echo "No com.lux.codex launch agents currently loaded."

echo
echo "--- codex-usage-tracker.log (last 5 lines) ---"
if [[ -f "$HOME/Library/Logs/codex-usage-tracker.log" ]]; then
  tail -n 5 "$HOME/Library/Logs/codex-usage-tracker.log"
else
  echo "Log file not found: $HOME/Library/Logs/codex-usage-tracker.log"
fi

echo "--- codex-dashboard.log (last 5 lines) ---"
if [[ -f "$HOME/Library/Logs/codex-dashboard.log" ]]; then
  tail -n 5 "$HOME/Library/Logs/codex-dashboard.log"
else
  echo "Log file not found: $HOME/Library/Logs/codex-dashboard.log"
fi
