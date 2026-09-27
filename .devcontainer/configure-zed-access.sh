#!/usr/bin/env bash
set -euo pipefail

username="${1:?Usage: configure-zed-access.sh USERNAME}"
sdk_root=/usr/local/zed

[[ -d "$sdk_root" ]] || exit 0

# Some SDK images ship root-only (0700/0600) files. Group membership
# cannot grant access to those. SDK binaries/data are public, not secrets.
chmod -R a+rX "$sdk_root"
# Calibration and downloaded model caches must be writable by the operator.
for directory in settings resources; do
    install -d "$sdk_root/$directory"
    chown -R "$username:$(id -gn "$username")" "$sdk_root/$directory"
done

# sudo initializes the account's supplementary groups, unlike some BuildKit
# USER executions. Assert the real runtime access while the build is still root.
if [[ -e "$sdk_root/tools/ZED_Diagnostic" ]]; then
    sudo -H -u "$username" test -r "$sdk_root/tools/ZED_Diagnostic"
    sudo -H -u "$username" test -x "$sdk_root/tools/ZED_Diagnostic"
fi
if [[ -e "$sdk_root/lib/libsl_zed.so" ]]; then
    sudo -H -u "$username" test -r "$sdk_root/lib/libsl_zed.so"
fi
