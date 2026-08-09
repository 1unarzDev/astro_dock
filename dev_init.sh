#!/usr/bin/env bash
set -e

cd ~/roboboat_ws
colcon build
source /opt/ros/jazzy/setup.bash
source install/setup.bash
echo "[dev_init] Dev environment ready"
