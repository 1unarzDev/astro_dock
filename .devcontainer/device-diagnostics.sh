#!/usr/bin/env bash
set -euo pipefail

patterns=(
    '/dev/serial/by-id/*Cube*'
    '/dev/serial/by-id/*CubePilot*'
    '/dev/ttyACM*'
    '/dev/video*'
)

echo "User: $(id)"
echo "Container: ${container:-unknown}"
echo "SELinux: $(command -v getenforce >/dev/null 2>&1 && getenforce || echo unavailable)"
echo

found=false
for pattern in "${patterns[@]}"; do
    while IFS= read -r path; do
        [[ -e "$path" ]] || continue
        found=true
        target="$(readlink -f "$path")"
        echo "Device: $path"
        [[ "$target" != "$path" ]] && echo "Target: $target"
        stat -Lc 'Type: %F  Major:Minor: %t:%T  Owner: %U:%G  Mode: %A (%a)' "$target"
        command -v getfacl >/dev/null 2>&1 && getfacl -cp "$target" 2>/dev/null || true
        command -v udevadm >/dev/null 2>&1 && udevadm info --query=property --name="$target" 2>/dev/null \
            | grep -E '^(ID_VENDOR_ID|ID_MODEL_ID|ID_SERIAL|ID_PATH)=' || true
        command -v lsof >/dev/null 2>&1 && lsof "$target" 2>/dev/null || true
        command -v fuser >/dev/null 2>&1 && fuser -v "$target" 2>/dev/null || true
        [[ -r "$target" && -w "$target" ]] && echo "Access: read/write" || echo "Access: restricted (sudo remains available in the privileged container)"
        echo
    done < <(compgen -G "$pattern" || true)
done

$found || echo "No CubePilot, ACM serial, or video devices were found. Reconnect the device and rerun this script."
