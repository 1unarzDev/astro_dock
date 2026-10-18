#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: boat-validate.sh [--promote [TAG]] REPOSITORY@sha256:DIGEST
       boat-validate.sh --container NAME

Pull and validate an immutable Jetson candidate digest on the boat. With
--promote, rotate the matching JetPack 5 or 6 release ring.
An explicit TAG bypasses the release ring. Docker credentials must already be
available to Docker for promotion. --container checks non-root CUDA and live
camera images in an already running container, such as roboboat_dev.
EOF
}

# Non-root CUDA, camera device access, and three real ZED images.
validate_runtime() {
    local container="$1"
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
    # The probe reads stdin while bash -c runs a private camera launch; killing
    # its process group keeps a camera process from holding the device.
    docker exec -i "$container" bash -c '
        set -e
        source /opt/ros/${ROS_DISTRO}/setup.bash
        source /opt/zed_ros2/setup.bash
        setsid ros2 launch zed_wrapper zed_camera.launch.py camera_model:=zed2i camera_name:=astro_validation >/tmp/astro-zed-launch.log 2>&1 &
        launch_pid=$!
        trap "kill -TERM -- -$launch_pid 2>/dev/null || true; wait $launch_pid || true" EXIT
        python3 - || { cat /tmp/astro-zed-launch.log; exit 1; }
    ' <<'PY'
import time
import rclpy
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image

rclpy.init()
node = rclpy.create_node('astro_camera_probe')
received, stamps, subscriptions = [], set(), {}
deadline = time.monotonic() + 90


def receive(message):
    stamp = (message.header.stamp.sec, message.header.stamp.nanosec)
    if stamp not in stamps and message.width > 0 and message.height > 0 and message.data:
        stamps.add(stamp)
        received.append((message.width, message.height, message.encoding))


try:
    while time.monotonic() < deadline and len(received) < 3:
        for topic, types in node.get_topic_names_and_types():
            if (not subscriptions and topic.startswith('/astro_validation/')
                    and ('/rgb/' in topic or '/left/' in topic)
                    and 'sensor_msgs/msg/Image' in types):
                subscriptions[topic] = node.create_subscription(
                    Image, topic, receive, qos_profile_sensor_data)
                print('Subscribing:', topic, flush=True)
        rclpy.spin_once(node, timeout_sec=0.2)
    if len(received) < 3:
        raise SystemExit('FAIL: fewer than three nonempty camera images in 90 seconds')
    print('PASS: received real images:', received[:3], flush=True)
finally:
    node.destroy_node()
    rclpy.shutdown()
PY
}

if [[ "${1:-}" == "--container" ]]; then
    [[ $# -eq 2 ]] || { usage >&2; exit 2; }
    validate_runtime "$2"
    exit 0
fi

promote=false
promote_tag=""
if [[ "${1:-}" == "--promote" ]]; then
    promote=true
    shift
    if [[ "${1:-}" == *:* && "${2:-}" == *:* ]]; then
        promote_tag="$1"
        shift
    fi
fi

image="${1:-}"
[[ -n "$image" ]] || { usage >&2; exit 2; }
[[ $# -eq 1 ]] || { usage >&2; exit 2; }
[[ "$image" =~ ^[^@]+@sha256:[0-9a-f]{64}$ ]] || {
    echo "Use the immutable repository@sha256:digest from the Actions summary." >&2
    exit 2
}

container="astro_boat_validate_$$"
repository="${image%@*}"
case "$(head -1 /etc/nv_tegra_release)" in
    '# R35 '*) family=jetson-jp5-foxy; distro=foxy; jetpack=5 ;;
    '# R36 '*) family=jetson-jp6-jazzy; distro=jazzy; jetpack=6 ;;
    *) echo 'Validation requires JetPack 5 / L4T R35 or 6 / R36.' >&2; exit 1 ;;
esac
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
lease_active=false
promoted=false

cleanup() {
    docker rm -f "$container" >/dev/null 2>&1 || true
    if $lease_active && ! $promoted; then
        if docker buildx imagetools inspect "${repository}:${family}" >/dev/null 2>&1; then
            echo "[boat] Releasing candidate lease back to the stable Jetson image"
            docker buildx imagetools create --tag "${repository}:${family}-check" \
                "${repository}:${family}" >/dev/null || true
        else
            echo "[boat] No stable Jetson tag exists; retaining jetson-check as the candidate lease"
        fi
    fi
}
trap cleanup EXIT

step() { printf '\n[boat] %s\n' "$*"; }
inside() { docker exec "$container" bash -lc "$1"; }

step "Pulling immutable candidate $image"
# Serialize camera access even when validation is launched outside Actions.
exec 9>/tmp/astro-jetson-validation.lock
flock -n 9 || { echo 'Another camera validation is running.' >&2; exit 1; }
docker pull "$image"

image_jetpack="$(docker image inspect --format '{{index .Config.Labels "org.astro.jetpack"}}' "$image")"
[[ "$image_jetpack" == "$jetpack" ]] || {
    echo "Image JetPack $image_jetpack does not match host JetPack $jetpack." >&2
    exit 1
}

if $promote; then
    step "Protecting the candidate digest during hardware validation"
    docker buildx imagetools create --tag "${repository}:${family}-check" "$image" >/dev/null
    lease_active=true
fi

architecture="$(docker image inspect --format '{{.Architecture}}' "$image")"
[[ "$architecture" == arm64 ]] || {
    echo "Expected an arm64 image, found: $architecture" >&2
    exit 1
}

step "Starting privileged hardware validation container"
docker run -d --name "$container" \
    --hostname "$(uname -n)" \
    --privileged --network host --ipc host --pid host \
    --runtime nvidia --shm-size 2g \
    -e ROS_DOMAIN_ID=223 -e ROS_LOCALHOST_ONLY=1 \
    -v /dev:/dev \
    -v /tmp/argus_socket:/tmp/argus_socket \
    "$image" sleep infinity >/dev/null

step "Checking identity, ROS, CUDA, and ZED installations"
inside 'test "$(id -un)" = roboboat && test "$(id -gn)" = roboboat && sudo -n true'
inside "source /opt/ros/${distro}/setup.bash && ros2 doctor --report"
if [[ "$jetpack" == 6 ]]; then
    inside 'source /opt/astro-setup.bash && ros2 pkg prefix topic_tools'
    inside 'source /opt/astro-setup.bash && ros2 pkg prefix velodyne_driver'
    inside 'source /opt/astro-setup.bash && ros2 pkg prefix vision_msgs'
    inside 'test -x /usr/local/zed/tools/ZED_Diagnostic'
fi
inside "source /opt/ros/${distro}/setup.bash && source /opt/zed_ros2/setup.bash && ros2 pkg prefix zed_wrapper && ros2 pkg prefix compressed_image_transport && ros2 pkg prefix rosbridge_server"
inside 'test -d /usr/local/zed && test -f /opt/zed_ros2/setup.bash'
# The headless JP5 SDK omits GUI tools; CUDA and live frames are checked below.
step "Checking non-root CUDA and live camera data"
validate_runtime "$container"

step "Candidate passed boat hardware validation"

if $promote; then
    if [[ -z "$promote_tag" ]]; then
        promote_tag="${repository}:${family}"
    fi
    if [[ "$promote_tag" == "${repository}:${family}" ]]; then
        step "Rotating validated Jetson release history"
        "$script_dir/../.github/scripts/registry-retention.py" --repository "$repository" \
            rotate "$family" "$image"
    else
        step "Promoting $image to $promote_tag without rotating the Jetson ring"
        docker buildx imagetools create --tag "$promote_tag" "$image"
    fi
    promoted=true
fi
