#!/usr/bin/env bash
# Install packages by distro-independent name on every Astro image.
# A ROS package name (nav2_bringup) installs ros-$ROS_DISTRO-nav2-bringup; any other
# name installs from apt as-is; ROS packages without a deb are built from source.
set -eo pipefail
if [[ $# == 0 || "$1" == --help ]]; then
    echo 'Usage: sudo -E astro-ros-install [--source] NAME [NAME ...]'
    echo '       sudo -E astro-ros-install --file LIST   (one name per line, # comments)'
    echo 'NAME is a ROS package, an apt package, or pip:SPEC for the /workspace/venv.'
    echo 'Source installs live in /opt/astro-extra; container shells reload after this command.'
    echo 'Each install is recorded in .devcontainer/installed.local.txt; for persistent images'
    echo 'move its names to packages.txt, jetson-packages.txt or requirements.txt.'
    exit 0
fi
[[ $EUID == 0 ]] || { echo 'Run with sudo -E.' >&2; exit 1; }
force_source=false
from_file=false
if [[ "$1" == --source ]]; then force_source=true; shift; fi
if [[ "$1" == --file ]]; then
    from_file=true
    names=()
    while read -r name _; do
        [[ -z "$name" ]] || names+=("$name")
    done < <(sed 's/#.*//' "${2:?--file needs a path}")
else
    names=("$@")
fi
(( ${#names[@]} )) || { echo 'Specify at least one package.' >&2; exit 2; }
ros_name='^[a-z][a-z0-9_]*$'
pips=()
for name in "${names[@]}"; do
    if [[ "$name" == pip:* ]]; then
        [[ "$name" =~ ^pip:[A-Za-z0-9][][A-Za-z0-9.,_\<\>=!~*+-]*$ ]] || { echo "Invalid pip spec: $name" >&2; exit 2; }
        pips+=("${name#pip:}")
    else
        [[ "$name" =~ ^[a-z0-9][a-z0-9.+_-]*$ ]] || { echo "Invalid package name: $name" >&2; exit 2; }
    fi
done
mapfile -t names < <(printf '%s\n' "${names[@]}" | grep -v '^pip:')
exec 9>/var/lock/astro-ros-install.lock
flock 9

# What each name became, printed (and recorded for interactive installs) on success.
declare -A how
order=()
note() { how[$1]="$2"; order+=("$1"); }
summarize() {
    local name record="${ROS_WS:-}/.devcontainer/installed.local.txt" target=packages.txt added=false
    [[ -f /etc/nv_tegra_release ]] && target=jetson-packages.txt
    for name in "${order[@]}"; do echo "✓ $name · ${how[$name]}"; done
    if $from_file || [[ ! -d "${ROS_WS:-}/.devcontainer" ]]; then return; fi
    for name in "${order[@]}"; do
        [[ "${how[$name]}" != installed ]] || continue
        awk -v n="$name" '$1 == n { found = 1 } END { exit !found }' "$record" 2>/dev/null && continue
        printf '%s  # %s %s %s %s\n' "$name" "$(date +%F)" "$HOSTNAME" "$ROS_DISTRO" "${how[$name]%% *}" >> "$record"
        added=true
        [[ "$name" == pip:* ]] && echo "Keep it: add \"${name#pip:}\" to .devcontainer/requirements.txt and rebuild." \
            || echo "Keep it: add \"$name\" to .devcontainer/$target and rebuild."
    done
    $added || return 0
    [[ -z "${SUDO_UID:-}" ]] || chown "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$record" 2>/dev/null || true
    echo "Recorded in $record (gitignored)."
}

if (( ${#pips[@]} )); then
    [[ -x /workspace/venv/bin/pip ]] || { echo 'pip: installs need /workspace/venv.' >&2; exit 1; }
    # As the calling user, so the venv stays theirs.
    if [[ -n "${SUDO_UID:-}" ]]; then runuser -u "$(id -nu "$SUDO_UID")" -- /workspace/venv/bin/pip install "${pips[@]}"
    else /workspace/venv/bin/pip install "${pips[@]}"; fi
    for name in "${pips[@]}"; do note "pip:$name" 'pip /workspace/venv'; done
fi
if (( ${#names[@]} == 0 )); then summarize; exit 0; fi
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
        note "$name" installed
    elif ! $force_source && apt-cache show "$deb" >/dev/null 2>&1; then
        debs+=("$deb")
        ros_packages+=("$name")
        note "$name" "deb $deb"
    elif ! $force_source && apt-cache show "$name" >/dev/null 2>&1; then
        debs+=("$name")
        note "$name" "deb $name"
    elif [[ "$name" =~ $ros_name ]]; then
        missing+=("$name")
        ros_packages+=("$name")
        note "$name" 'source → /opt/astro-extra'
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
for package in "${ros_packages[@]}"; do ros2 pkg prefix "$package" >/dev/null; done
summarize
