#!/usr/bin/env bash
# Common deployment path for both generations, including offline docker load.
set -euo pipefail
image="${1:?Usage: deploy-jetson.sh IMAGE [WORKSPACE]}"
workspace="${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
[[ -d "$workspace" ]] || { echo 'Workspace must exist.' >&2; exit 2; }
workspace="$(cd "$workspace" && pwd)"
if grep -q '^# R35 ' /etc/nv_tegra_release; then
    jetpack=5
elif grep -q '^# R36 ' /etc/nv_tegra_release; then
    jetpack=6
else
    echo 'Unsupported host: expected JetPack 5/R35 or JetPack 6/R36.' >&2; exit 1
fi
if ! docker image inspect "$image" >/dev/null 2>&1; then docker pull "$image"; fi
actual="$(docker image inspect --format '{{index .Config.Labels "org.astro.jetpack"}}' "$image")"
[[ "$actual" == "$jetpack" ]] || {
    echo "Image JetPack label '$actual' does not match host JetPack $jetpack." >&2; exit 1;
}
[[ "$(docker image inspect --format '{{.Architecture}}' "$image")" == arm64 ]] || exit 1
if docker container inspect roboboat_dev >/dev/null 2>&1; then
    echo 'roboboat_dev already exists; inspect and explicitly remove/rename it before redeploying.' >&2
    exit 1
fi
groups=()
for group in video render; do
    gid="$(getent group "$group" | cut -d: -f3 || true)"
    [[ -z "$gid" ]] || groups+=(--group-add "$gid")
done
docker run -d --name roboboat_dev --hostname "$(uname -n)" \
    --restart unless-stopped --runtime nvidia --privileged \
    --network host --ipc host --shm-size 2g --user roboboat "${groups[@]}" \
    -e ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-42}" \
    -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all \
    -v /dev:/dev -v /tmp/argus_socket:/tmp/argus_socket \
    -v "${workspace}:/home/roboboat/roboboat_ws" \
    -v "astro_zed_jp${jetpack}_settings:/usr/local/zed/settings" \
    -v "astro_zed_jp${jetpack}_resources:/usr/local/zed/resources" \
    "$image" sleep infinity
docker exec roboboat_dev bash -ic 'id; test "$(id -un)" = roboboat; ros2 pkg prefix zed_wrapper'
echo 'Container running. Enter: docker exec -it roboboat_dev bash'
echo 'Validate GPU and camera with .devcontainer/validate-jetson-runtime.sh roboboat_dev'
