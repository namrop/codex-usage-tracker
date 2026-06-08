#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_DIR="$PROJECT_ROOT/daemon"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

USAGE_TRACKER_PLIST="$PLIST_DIR/com.lux.codex-usage-tracker.plist"
DASHBOARD_PLIST="$PLIST_DIR/com.lux.codex-dashboard.plist"
AUTOCOMMIT_PLIST="$PLIST_DIR/com.lux.codex-usage-ledger-autocommit.plist"
USAGE_TRACKER_DEST="$LAUNCH_AGENTS_DIR/com.lux.codex-usage-tracker.plist"
DASHBOARD_DEST="$LAUNCH_AGENTS_DIR/com.lux.codex-dashboard.plist"
AUTOCOMMIT_DEST="$LAUNCH_AGENTS_DIR/com.lux.codex-usage-ledger-autocommit.plist"

if [[ -n "${CODEX_TRACKER_PYTHON:-}" && -x "${CODEX_TRACKER_PYTHON}" ]]; then
  PYTHON_PATH="${CODEX_TRACKER_PYTHON}"
elif PYTHON_PATH="$(command -v python3 2>/dev/null)"; then
  :
else
  PYTHON_PATH="/usr/bin/python3"
fi

echo "Using Python path: ${PYTHON_PATH}"
mkdir -p "$LAUNCH_AGENTS_DIR"
echo "Ensured launch agent directory: ${LAUNCH_AGENTS_DIR}"

deploy_plist() {
  local source_plist="$1"
  local dest_plist="$2"
  local label="$3"

  echo "Deploying ${label} launch agent..."
  cp "$source_plist" "$dest_plist"
  sed -i '' "s|<string>PYTHON_PATH</string>|<string>${PYTHON_PATH}</string>|g" "$dest_plist"
  echo "Updated ${dest_plist} with Python path."
}

deploy_plist "$USAGE_TRACKER_PLIST" "$USAGE_TRACKER_DEST" "com.lux.codex-usage-tracker"
deploy_plist "$DASHBOARD_PLIST" "$DASHBOARD_DEST" "com.lux.codex-dashboard"
deploy_plist "$AUTOCOMMIT_PLIST" "$AUTOCOMMIT_DEST" "com.lux.codex-usage-ledger-autocommit"

for entry in \
  "$USAGE_TRACKER_DEST:com.lux.codex-usage-tracker" \
  "$DASHBOARD_DEST:com.lux.codex-dashboard" \
  "$AUTOCOMMIT_DEST:com.lux.codex-usage-ledger-autocommit"; do
  plist="${entry%%:*}"
  label="${entry##*:}"
  echo "Loading $(basename "$plist")..."
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load -w "$plist"
  echo "Starting ${label}..."
  launchctl start "$label" 2>/dev/null || true
done

echo "Deployment complete."
