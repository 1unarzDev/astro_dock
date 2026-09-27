#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: boat-validate.sh [--promote [TAG]] REPOSITORY@sha256:DIGEST

Pull and validate an immutable Jetson candidate digest on the boat. With
--promote, rotate the repository's jetson-jp6-jazzy release ring.
An explicit TAG bypasses the release ring. Docker credentials must already be
available to Docker for promotion.
EOF
}

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
family=jetson-jp6-jazzy
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
grep -q '^# R36 ' /etc/nv_tegra_release || {
    echo 'This validation path requires JetPack 6 / L4T R36.' >&2
    exit 1
}
docker pull "$image"

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
    -v /dev:/dev \
    -v /tmp/argus_socket:/tmp/argus_socket \
    "$image" sleep infinity >/dev/null

step "Checking identity, ROS, CUDA, and ZED installations"
inside 'test "$(id -un)" = roboboat && test "$(id -gn)" = roboboat && sudo -n true'
inside 'source /opt/ros/jazzy/setup.bash && ros2 doctor --report'
inside 'source /opt/ros/jazzy/setup.bash && ros2 pkg prefix topic_tools'
inside 'source /opt/astro-setup.bash && ros2 pkg prefix velodyne_driver'
inside 'source /opt/ros/jazzy/setup.bash && ros2 pkg prefix vision_msgs'
inside 'test -d /usr/local/zed && test -f /opt/zed_ros2/setup.bash'
inside 'test -x /usr/local/zed/tools/ZED_Diagnostic'
step "Checking non-root CUDA and live camera data through the shared validator"
"$script_dir/validate-jetson-runtime.sh" "$container"

step "Candidate passed boat hardware validation"

if $promote; then
    if [[ -z "$promote_tag" ]]; then
        promote_tag="${repository}:${family}"
    fi
    if [[ "$promote_tag" == "${repository}:${family}" ]]; then
        step "Rotating validated Jetson release history"
        "$script_dir/registry-retention.py" --repository "$repository" \
            rotate "$family" "$image"
    else
        step "Promoting $image to $promote_tag without rotating the Jetson ring"
        docker buildx imagetools create --tag "$promote_tag" "$image"
    fi
    promoted=true
fi
