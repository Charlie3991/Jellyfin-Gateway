#!/usr/bin/env bash
# Manual runner (no systemd): starts qBittorrent (if not running) and the
# gateway. Prefers the desktop qBittorrent GUI when a display session is
# available (the gateway talks to its WebUI on :8090); falls back to
# headless qbittorrent-nox otherwise. Prefer the systemd user unit on this
# machine:
#   systemctl --user start jellyfin-gateway
set -euo pipefail
cd "$(dirname "$0")"

if pgrep -x qbittorrent > /dev/null || pgrep -x qbittorrent-nox > /dev/null; then
  echo "qBittorrent already running"
elif { [ -n "${WAYLAND_DISPLAY:-}" ] || [ -n "${DISPLAY:-}" ]; } && command -v qbittorrent > /dev/null; then
  echo "starting desktop qBittorrent ..."
  nohup qbittorrent >> /tmp/qbittorrent-gui.log 2>&1 &
  sleep 8
else
  echo "no display session - starting qbittorrent-nox ..."
  nohup qbittorrent-nox >> /tmp/qbittorrent-nox.log 2>&1 &
  sleep 5
fi

# config.py parses .env as inert data.  Never source dashboard-controlled
# values into a shell.
exec .venv/bin/python gateway.py
