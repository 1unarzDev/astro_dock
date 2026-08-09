#!/usr/bin/env bash
set -euo pipefail

image="${1:?Usage: jetson-smoke.sh IMAGE}"
container=astro_jetson_smoke
cleanup() { docker rm -f "$container" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker pull "$image"
docker run -d --name "$container" \
    --privileged --network host --ipc host \
    --runtime nvidia --shm-size 2g \
    -v /dev:/dev -v /tmp/argus_socket:/tmp/argus_socket \
    "$image" sleep infinity

docker exec "$container" bash -lc 'source /opt/ros/jazzy/setup.bash && ros2 doctor --report'
docker exec "$container" bash -lc 'test "$(id -un)" = roboboat && test "$(id -gn)" = roboboat && sudo -n true'
docker exec "$container" bash -lc 'test -d /usr/local/zed && test -f /opt/zed_ros2/setup.bash'
docker exec "$container" bash -lc 'test -x /usr/local/zed/tools/ZED_Diagnostic'
docker exec "$container" bash -lc 'compgen -G "/dev/serial/by-id/*Cube*" >/dev/null'
docker exec "$container" bash -lc 'for node in /dev/video*; do test -r "$node" && test -w "$node"; done'
docker exec "$container" bash -lc '
  source /opt/ros/jazzy/setup.bash
  source /opt/zed_ros2/setup.bash
  ros2 launch zed_wrapper zed_camera.launch.py camera_model:=zed2i >/tmp/zed-launch.log 2>&1 &
  launch_pid=$!
  trap "kill $launch_pid 2>/dev/null || true" EXIT
  for _ in $(seq 1 30); do
    ros2 topic list | grep -q "/zed/zed_node/left/image_rect_color" && break
    sleep 1
  done
  timeout 20 ros2 topic hz /zed/zed_node/left/image_rect_color >/tmp/zed-rate.log 2>&1 || test $? -eq 124
  grep -q "average rate" /tmp/zed-rate.log || { cat /tmp/zed-launch.log; cat /tmp/zed-rate.log; exit 1; }
'
