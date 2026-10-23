#!/usr/bin/env bash
# The dev image must carry the tools boat_test needs to send bags off the boat.
set -euo pipefail

packages="$(cd "$(dirname "$0")/../.." && pwd)/.devcontainer/packages.txt"
for pkg in rsync openssh-client; do
    grep -qE "^$pkg( |$)" "$packages" || { echo "FAIL: $pkg missing from packages.txt"; exit 1; }
done
echo "ok"
