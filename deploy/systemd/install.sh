#!/usr/bin/env bash
# Installs AI Sentinel as user-level systemd services (no sudo needed/used — this is meant for
# shared boxes like kolmogorov where a personal service has no business in /etc/systemd/system).
# Run this ON the target host, from the repo root: ./deploy/systemd/install.sh
set -euo pipefail
cd "$(dirname "$0")/../.."

mkdir -p ~/.config/systemd/user
cp deploy/systemd/ai-sentinel-demo.service deploy/systemd/ai-sentinel-engine.service ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now ai-sentinel-demo.service ai-sentinel-engine.service

# Lets the user's systemd instance (and these services) start at boot without an active login —
# without this, "survives a reboot" only means "survives until someone next logs in."
loginctl enable-linger "$(whoami)" 2>/dev/null || true

echo
echo "Installed. Useful commands:"
echo "  systemctl --user status ai-sentinel-demo ai-sentinel-engine"
echo "  journalctl --user -u ai-sentinel-demo -u ai-sentinel-engine -f"
echo "  systemctl --user restart ai-sentinel-demo ai-sentinel-engine"
