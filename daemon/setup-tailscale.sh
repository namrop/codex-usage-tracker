#!/usr/bin/env bash
set -euo pipefail

sudo tailscale funnel --bg --https=5174 5174

FunnelURL=$(tailscale funnel status 2>/dev/null | awk '/https:\/\// { print $1; exit }')
if [ -n "${FunnelURL}" ]; then
  echo "Tailscale dashboard URL: ${FunnelURL}"
else
  echo "Command sent to Tailscale. Run 'tailscale funnel status' for the dashboard URL."
fi
