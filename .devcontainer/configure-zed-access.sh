#!/usr/bin/env bash
set -euo pipefail

username="${1:?Usage: configure-zed-access.sh USERNAME}"
sdk_root=/usr/local/zed

[[ -d "$sdk_root" ]] || exit 0

mapfile -t sdk_gids < <(
    find -L "$sdk_root" -xdev -printf '%G\n' 2>/dev/null | sort -un
)

for sdk_gid in "${sdk_gids[@]}"; do
    [[ "$sdk_gid" =~ ^[0-9]+$ ]] || continue
    sdk_group="$(getent group "$sdk_gid" | cut -d: -f1 || true)"
    if [[ -z "$sdk_group" ]]; then
        sdk_group="zed_${sdk_gid}"
        groupadd --gid "$sdk_gid" "$sdk_group"
    fi
    usermod -aG "$sdk_group" "$username"
done

echo "[zed-access] SDK GIDs: ${sdk_gids[*]:-none}"
echo "[zed-access] $username groups: $(id -nG "$username")"

# sudo initializes the account's supplementary groups, unlike some BuildKit
# USER executions. Assert the real runtime access while the build is still root.
if [[ -e "$sdk_root/tools/ZED_Diagnostic" ]]; then
    sudo -H -u "$username" test -r "$sdk_root/tools/ZED_Diagnostic"
    sudo -H -u "$username" test -x "$sdk_root/tools/ZED_Diagnostic"
fi
