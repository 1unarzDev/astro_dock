#!/usr/bin/env bash
# Build the workspace and load it into this shell: `source dev_init.sh [colcon args]`.
# Running it as a script only builds. Extra arguments go to `colcon build`.

_dev_init() {
    local ws args=()
    ws="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    # Jetson images bundle ROS with their overlays; other images use the distro.
    if [[ -f /opt/astro-setup.bash ]]; then
        source /opt/astro-setup.bash || return
    else
        source "/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash" || return
    fi
    # colcon refuses to mix install layouts, so follow the existing one.
    [[ "$(cat "$ws/install/.colcon_install_layout" 2>/dev/null)" == merged ]] && args+=(--merge-install)
    (cd "$ws" && colcon build "${args[@]}" "$@") || return
    source "$ws/install/setup.bash" && echo "[dev_init] built and sourced $ws"
}

_dev_init "$@" || { unset -f _dev_init; return 1 2>/dev/null || exit 1; }
unset -f _dev_init
