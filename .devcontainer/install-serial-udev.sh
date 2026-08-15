#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RULE_SOURCE="${SCRIPT_DIR}/99-roboboat-serial.rules"
RULE_TARGET="/etc/udev/rules.d/99-roboboat-serial.rules"

getent passwd roboboat >/dev/null || {
    echo "Host user 'roboboat' does not exist; refusing to install rule." >&2
    exit 1
}

sudo install -m 0644 "${RULE_SOURCE}" "${RULE_TARGET}"
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=tty
echo "Installed ${RULE_TARGET}; reconnect serial devices if needed."
