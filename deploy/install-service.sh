#!/usr/bin/env bash
# Install Pride as a user service. No root: everything lives in the user session.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
unit_dir="$HOME/.config/systemd/user"
mkdir -p "$unit_dir" "$HOME/.local/share/pride/logs"
cp "$here/pride.service" "$unit_dir/pride.service"
# the unit hardcodes /home/nox; rewrite it for whoever is installing
sed -i "s|/home/nox|$HOME|g" "$unit_dir/pride.service"
systemctl --user daemon-reload
systemctl --user enable --now pride.service
sleep 2
systemctl --user --no-pager status pride.service | head -12
echo
echo "logs:    journalctl --user -u pride -f"
echo "or:      tail -f $HOME/.local/share/pride/logs/daemon.log"
echo "control: systemctl --user {stop,restart,disable} pride"
