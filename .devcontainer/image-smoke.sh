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
    /usr/bin/python3 -c 'import importlib.metadata; tuple(importlib.metadata.distributions())'

if [[ "$mode" == cuda || "$mode" == jetson ]]; then
    echo "[smoke] supplementary groups: $(id -nG)"
    check "ZED SDK directory traversal" test -x /usr/local/zed
    check "ZED Diagnostic readability" test -r /usr/local/zed/tools/ZED_Diagnostic
    check "ZED Diagnostic execution" test -x /usr/local/zed/tools/ZED_Diagnostic
    check "ZED wrapper environment" test -f /opt/zed_ros2/setup.bash
fi

if [[ "$mode" == jetson ]]; then
    check "source-built topic_tools" bash -lc \
        'source /opt/ros/jazzy/setup.bash && ros2 pkg prefix topic_tools >/dev/null'
    check "source-built Velodyne driver" bash -lc \
        'source /opt/astro-setup.bash && ros2 pkg prefix velodyne_driver >/dev/null'
    check "source-built vision_msgs" bash -lc \
        'source /opt/ros/jazzy/setup.bash && ros2 pkg prefix vision_msgs >/dev/null'
else
    check "ROS run and bag commands" bash -lc \
        'source /opt/ros/jazzy/setup.bash && ros2 run --help >/dev/null && ros2 bag --help >/dev/null'
    check "hardware TUI Python dependencies" /usr/bin/python3 -c 'import rich, gpiod, yaml'
    check "MAVROS and Extras" bash -lc \
        'source /opt/ros/jazzy/setup.bash && ros2 pkg prefix mavros && ros2 pkg prefix mavros_extras'
    check "MAVROS geoid dataset" test -r /usr/share/GeographicLib/geoids/egm96-5.pgm
    check "Velodyne driver" bash -lc \
        'source /opt/ros/jazzy/setup.bash && ros2 pkg prefix velodyne_driver'
fi
