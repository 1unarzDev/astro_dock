#!/usr/bin/env bash
set -eo pipefail

source "/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"
if [[ -n "${ROS_WS:-}" && -f "$ROS_WS/install/setup.bash" ]]; then
  source "$ROS_WS/install/setup.bash"
fi

exec python3 "$ROS_WS/.devcontainer/mixed_pointcloud_bridge.py" "$@"
