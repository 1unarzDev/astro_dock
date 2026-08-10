#!/usr/bin/env bash
set -euo pipefail

mode="${1:-developer}"

check() {
    local description="$1"
    shift
    printf '[smoke] %-42s' "$description"
    if "$@"; then
        echo PASS
    else
        status=$?
        echo FAIL
        exit "$status"
    fi
}

check "developer user" test "$(id -un)" = roboboat
check "developer primary group" test "$(id -gn)" = roboboat
check "sudo acknowledgement marker" test -e "$HOME/.sudo_as_admin_successful"
check "passwordless sudo" sudo -n true
check "system Python package metadata" \
    /usr/bin/python3 -c 'import pkg_resources; tuple(pkg_resources.working_set)'

if [[ "$mode" == cuda || "$mode" == jetson ]]; then
    echo "[smoke] supplementary groups: $(id -nG)"
    check "ZED SDK directory traversal" test -x /usr/local/zed
    check "ZED Diagnostic readability" test -r /usr/local/zed/tools/ZED_Diagnostic
    check "ZED Diagnostic execution" test -x /usr/local/zed/tools/ZED_Diagnostic
    check "ZED wrapper environment" test -f /opt/zed_ros2/setup.bash
fi
