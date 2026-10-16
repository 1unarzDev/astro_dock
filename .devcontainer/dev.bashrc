export ROS_WS="${ROS_WS:-$HOME/roboboat_ws}"

# Load ROS, the image overlays and the built workspace. Later entries take
# precedence: each overlay was built on the ones before it, and the workspace
# overrides installed packages.
ros_env() {
  local prefix
  for prefix in "/opt/ros/${ROS_DISTRO:-jazzy}" /opt/zed_ros2 /opt/velodyne /opt/astro-extra "$ROS_WS/install"; do
    if [[ -f "$prefix/setup.bash" ]]; then source "$prefix/setup.bash"; fi
  done
}
ros_env

# Reload right after the commands that change installed packages, so results are
# usable in this shell without checking before every ros2 call.
colcon() {
  command colcon "$@" || return
  if [[ "${1:-}" == build ]]; then
    ros_env
    if [[ "$PWD" != "$ROS_WS" && -f install/setup.bash ]]; then source install/setup.bash; fi
  fi
}
astro-ros-install() {
  sudo -E astro-ros-install "$@" && ros_env
}

# Jazzy replaces ROS_LOCALHOST_ONLY with an explicit discovery range. Unset
# the legacy variable as well so shells in containers created before this
# migration do not emit a warning.
unset ROS_LOCALHOST_ONLY
export ROS_AUTOMATIC_DISCOVERY_RANGE="${ROS_AUTOMATIC_DISCOVERY_RANGE:-SUBNET}"
if [[ -f /workspace/venv/bin/activate ]]; then
  source /workspace/venv/bin/activate
fi

# Terminals use the default DDS transport; the lidar participants opt into
# mhseals_nav/config/fastdds_large_data.xml from their launch files.
unset FASTDDS_BUILTIN_TRANSPORTS
export GZ_SIM_RESOURCE_PATH="${ROS_WS}/src/asv_wave_sim/gz-waves-models/models:${ROS_WS}/src/asv_wave_sim/gz-waves-models/world_models:${ROS_WS}/src/asv_wave_sim/gz-waves-models/worlds${GZ_SIM_RESOURCE_PATH:+:${GZ_SIM_RESOURCE_PATH}}"
export GZ_SIM_SYSTEM_PLUGIN_PATH="${ROS_WS}/install/lib${GZ_SIM_SYSTEM_PLUGIN_PATH:+:${GZ_SIM_SYSTEM_PLUGIN_PATH}}"

alias rviz2="rviz2 -d $ROS_WS/config/dark.rviz --stylesheet $ROS_WS/config/dark.qss"
alias rqt_tf_tree="ros2 run rqt_tf_tree rqt_tf_tree"

help() {
  cat "$HOME/.helper.txt"
}
