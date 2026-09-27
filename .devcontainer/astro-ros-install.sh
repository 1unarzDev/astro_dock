#!/usr/bin/env bash
# Same interface on JP5/Foxy and JP6/Jazzy: binary first, source fallback.
set -eo pipefail
if [[ $# == 0 || "$1" == --help ]]; then
    echo 'Usage: sudo -E astro-ros-install [--source] ROS_PACKAGE [ROS_PACKAGE ...]'
    echo 'Installs available distro debs; resolves/builds missing source dependencies.'
    echo 'Source installs live in /opt/astro-extra; open a new shell afterward.'
    echo 'For persistent images add names to jetson-jp5-packages.txt or jetson-jp6-packages.txt.'
    exit 0
fi
[[ $EUID == 0 ]] || { echo 'Run with sudo -E.' >&2; exit 1; }
force_source=false
if [[ "$1" == --source ]]; then force_source=true; shift; fi
(( $# )) || { echo 'Specify at least one ROS package.' >&2; exit 2; }
exec 9>/var/lock/astro-ros-install.lock
flock 9
export ROS_HOME=/var/cache/astro-ros
for package in "$@"; do
    [[ "$package" =~ ^[a-z][a-z0-9_]*$ ]] || { echo "Invalid ROS package: $package" >&2; exit 2; }
done
source "/opt/ros/${ROS_DISTRO:?ROS_DISTRO must be set}/setup.bash"
for overlay in /opt/zed_ros2 /opt/velodyne /opt/astro-extra; do
    if [[ -f "$overlay/setup.bash" ]]; then source "$overlay/setup.bash"; fi
done
apt-get update
missing=()
for package in "$@"; do
    if ros2 pkg prefix "$package" >/dev/null 2>&1; then continue; fi
    deb="ros-${ROS_DISTRO}-${package//_/-}"
    if ! $force_source && apt-cache show "$deb" >/dev/null 2>&1; then
        apt-get install -y --no-install-recommends "$deb"
    else
        missing+=("$package")
    fi
done
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
for package in "$@"; do ros2 pkg prefix "$package"; done
