if [[ -f /opt/ros/jazzy/setup.bash ]]; then
  source /opt/ros/jazzy/setup.bash
fi
if [[ -f /workspace/venv/bin/activate ]]; then
  source /workspace/venv/bin/activate
fi
if [[ -f /opt/zed_ros2/setup.bash ]]; then
  source /opt/zed_ros2/setup.bash
fi

export ROS_WS="${ROS_WS:-$HOME/roboboat_ws}"
export GZ_SIM_RESOURCE_PATH="${ROS_WS}/src/asv_wave_sim/gz-waves-models/models:${ROS_WS}/src/asv_wave_sim/gz-waves-models/world_models:${ROS_WS}/src/asv_wave_sim/gz-waves-models/worlds${GZ_SIM_RESOURCE_PATH:+:${GZ_SIM_RESOURCE_PATH}}"
export GZ_SIM_SYSTEM_PLUGIN_PATH="${ROS_WS}/install/lib${GZ_SIM_SYSTEM_PLUGIN_PATH:+:${GZ_SIM_SYSTEM_PLUGIN_PATH}}"

alias rviz2="rviz2 -d $ROS_WS/config/dark.rviz --stylesheet $ROS_WS/config/dark.qss"
alias rqt_tf_tree="ros2 run rqt_tf_tree rqt_tf_tree"

if [[ -f "$ROS_WS/install/setup.bash" ]]; then
  source "$ROS_WS/install/setup.bash"
fi

help() {
  cat "$HOME/.helper.txt"
}
