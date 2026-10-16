#!/usr/bin/env bash
# Install packages by distro-independent name on every Astro image (Foxy or Jazzy).
# A ROS package name (nav2_bringup) installs ros-$ROS_DISTRO-nav2-bringup; any other
# name installs from apt as-is; ROS packages without a deb are built from source.
set -eo pipefail
if [[ $# == 0 || "$1" == --help ]]; then
    echo 'Usage: sudo -E astro-ros-install [--source] NAME [NAME ...]'
    echo '       sudo -E astro-ros-install --file LIST   (one name per line, # comments)'
    echo 'Source installs live in /opt/astro-extra; container shells reload after this command.'
    echo 'For persistent images add names to packages.txt or jetson-packages.txt.'
    exit 0
fi
[[ $EUID == 0 ]] || { echo 'Run with sudo -E.' >&2; exit 1; }
force_source=false
if [[ "$1" == --source ]]; then force_source=true; shift; fi
if [[ "$1" == --file ]]; then
    mapfile -t names < <(sed 's/#.*//; s/[[:space:]]//g; /^$/d' "${2:?--file needs a path}")
else
    names=("$@")
fi
(( ${#names[@]} )) || { echo 'Specify at least one package.' >&2; exit 2; }
ros_name='^[a-z][a-z0-9_]*$'
for name in "${names[@]}"; do
    [[ "$name" =~ ^[a-z0-9][a-z0-9.+_-]*$ ]] || { echo "Invalid package name: $name" >&2; exit 2; }
done
exec 9>/var/lock/astro-ros-install.lock
flock 9
export ROS_HOME=/var/cache/astro-ros
load_ros() {
    # Base images without ROS (e.g. the CUDA image) get it from this install.
    for overlay in "/opt/ros/${ROS_DISTRO:?ROS_DISTRO must be set}" /opt/zed_ros2 /opt/velodyne /opt/astro-extra; do
        if [[ -f "$overlay/setup.bash" ]]; then source "$overlay/setup.bash"; fi
    done
}
load_ros
apt-get update
debs=()
ros_packages=()
missing=()
for name in "${names[@]}"; do
    deb="ros-${ROS_DISTRO}-${name//_/-}"
    if [[ "$name" =~ $ros_name ]] && ros2 pkg prefix "$name" >/dev/null 2>&1; then
        ros_packages+=("$name")
    elif ! $force_source && apt-cache show "$deb" >/dev/null 2>&1; then
        debs+=("$deb")
        ros_packages+=("$name")
    elif ! $force_source && apt-cache show "$name" >/dev/null 2>&1; then
        debs+=("$name")
    elif [[ "$name" =~ $ros_name ]]; then
        missing+=("$name")
        ros_packages+=("$name")
    else
        echo "No ROS or apt package named $name" >&2; exit 1
    fi
done
if (( ${#debs[@]} )); then
    apt-get install -y --no-install-recommends "${debs[@]}"
    load_ros
fi
if (( ${#missing[@]} )); then
    apt-get install -y --no-install-recommends python3-rosinstall-generator python3-vcstool
    if [[ ! -f /etc/ros/rosdep/sources.list.d/20-default.list ]]; then rosdep init; fi
    rosdep update --include-eol-distros --rosdistro "$ROS_DISTRO"
    work="$(mktemp -d /tmp/astro-ros-install.XXXXXX)"
    echo "Source build workspace (retained on failure): $work"
    mkdir -p "$work/src"
    mapfile -t installed < <(ros2 pkg list)
    rosinstall_generator "${missing[@]}" --rosdistro "$ROS_DISTRO" --deps \
        --format repos --exclude "${installed[@]}" > "$work/request.repos"
    vcs import "$work/src" < "$work/request.repos"
    vcs export --exact "$work/src" > "$work/resolved.repos"
    rosdep install --from-paths "$work/src" --ignore-src --rosdistro "$ROS_DISTRO" \
        --skip-keys "${installed[*]}" -y
    (cd "$work" && CMAKE_BUILD_PARALLEL_LEVEL="${ASTRO_BUILD_JOBS:-2}" \
        colcon build --merge-install --install-base /opt/astro-extra \
        --executor sequential --cmake-args -DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=Release)
    mkdir -p /opt/astro-extra/manifests
    install -m 0644 "$work/resolved.repos" "/opt/astro-extra/manifests/$(basename "$work").repos"
    source /opt/astro-extra/setup.bash
    # Delete only this command's successful temporary workspace.
    rm -rf "$work"
fi
for package in "${ros_packages[@]}"; do ros2 pkg prefix "$package"; done
