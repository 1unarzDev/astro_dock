#!/usr/bin/env bash
set -euo pipefail
container="${1:-roboboat_dev}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
docker exec "$container" bash -c 'test "$(id -un)" = roboboat && sudo -n true'
docker exec -i "$container" python3 - <<'PY'
import ctypes
cuda = ctypes.CDLL('libcuda.so.1')
count = ctypes.c_int()
assert cuda.cuInit(0) == 0, 'CUDA initialization failed'
assert cuda.cuDeviceGetCount(ctypes.byref(count)) == 0
assert count.value > 0, 'No CUDA devices'
print('PASS: CUDA initialized as normal user; devices:', count.value)
PY
docker exec "$container" bash -c 'compgen -G "/dev/video*" >/dev/null; for device in /dev/video*; do test -r "$device" && test -w "$device" || exit 1; done'
# Scope the camera launch to a temporary script/process, not other ROS nodes.
docker cp "$root/camera-probe.py" "$container:/tmp/astro-camera-probe.py"
docker exec "$container" bash -c '
    set -e
    source /opt/ros/${ROS_DISTRO}/setup.bash
    source /opt/zed_ros2/setup.bash
    ros2 launch zed_wrapper zed_camera.launch.py camera_model:=zed2i camera_name:=astro_validation >/tmp/astro-zed-launch.log 2>&1 &
    launch_pid=$!
    # Background jobs can inherit SIGINT ignored from noninteractive bash.
    trap "kill -TERM $launch_pid 2>/dev/null || true; wait $launch_pid || true" EXIT
    python3 /tmp/astro-camera-probe.py || { cat /tmp/astro-zed-launch.log; exit 1; }
'
