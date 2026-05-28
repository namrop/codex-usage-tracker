#!/usr/bin/env bash
set -euo pipefail

LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
USAGE_TRACKER_PLIST="$LAUNCH_AGENTS_DIR/com.lux.codex-usage-tracker.plist"
DASHBOARD_PLIST="$LAUNCH_AGENTS_DIR/com.lux.codex-dashboard.plist"

unload_and_remove() {
  local plist="$1"
  echo "Unloading ${plist}..."
  if [[ -f "$plist" ]]; then
    launchctl unload "$plist" 2>/dev/null || true
    rm -f "$plist"
    echo "Removed ${plist}"
  else
    echo "Skip unload/remove (not found): ${plist}"
  fi
}

unload_and_remove "$USAGE_TRACKER_PLIST"
unload_and_remove "$DASHBOARD_PLIST"

echo "Uninstall complete."
