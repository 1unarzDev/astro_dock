#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
selection="${ASTRO_RUN_CONFIG:-}"
if (($#)); then
    [[ $# == 2 && $1 == --config && -n $2 ]] || { echo 'Usage: prebuild.sh [--config FILE]' >&2; exit 2; }
    selection="$2"
fi
# Host operator config dir, bind-mounted by docker-compose.yml (Docker would create it root-owned).
mkdir -p "${HOME}/.config/mhseals"
os_type=linux

if [[ "$(uname -s)" == Darwin ]]; then
    os_type=mac
elif [[ -n "${WSL_DISTRO_NAME:-}" ]]; then
    os_type=windows
elif [[ "$(uname -s)" == Linux ]]; then
    arch="$(uname -m)"
    if command -v rpm-ostree >/dev/null 2>&1 || command -v bootc >/dev/null 2>&1 || [[ -e /run/ostree-booted ]]; then
        os_type=immutable
    elif [[ "$arch" == aarch64 ]] && [[ -r /proc/device-tree/model ]] && grep -aqi jetson /proc/device-tree/model; then
        os_type=jetson
    elif [[ "$arch" == aarch64 ]] && { [[ -f /etc/rpi-issue ]] || { [[ -r /proc/device-tree/model ]] && grep -aqi raspberry /proc/device-tree/model; }; }; then
        os_type=rpi
    elif command -v nvidia-smi >/dev/null 2>&1; then
        os_type=nvidia
    fi
else
    echo "Unsupported host kernel: $(uname -s)" >&2
    exit 1
fi

override_file="${script_dir}/docker-compose.override.${os_type}.yml"
[[ -f "$override_file" ]] || { echo "Missing Compose adapter: $override_file" >&2; exit 1; }
if [[ "$os_type" == jetson ]]; then
    if grep -q '^# R35 ' /etc/nv_tegra_release; then
        jetson_family=jetson-jp5-foxy
    elif grep -q '^# R36 ' /etc/nv_tegra_release; then
        jetson_family=jetson-jp6-jazzy
    else
        echo 'Unsupported JetPack: expected L4T R35 or R36.' >&2; exit 1
    fi
fi
if [[ -n "$selection" && "$os_type" != linux ]]; then
    echo 'Selected YAML supports Linux static off-boat only.' >&2; exit 2
fi
# Stage every derivation; selected validation never damages the active adapter.
work_file="$(mktemp "${script_dir}/.adapter.XXXXXX")"
trap 'rm -f "$work_file" "$work_file.bak" "$script_dir/docker-compose.override.yml.new"' EXIT
cp "$override_file" "$work_file"
if [[ "$os_type" == jetson ]]; then
    sed -i.bak "s/jetson-jp6-jazzy/${jetson_family}/g" "$work_file"
    if [[ "$jetson_family" == jetson-jp5-foxy ]]; then
        sed -i.bak 's/ASTRO_ROS_DOMAIN_ID:-42/ASTRO_ROS_DOMAIN_ID:-142/' "$work_file"
    fi
fi
host_name="${ASTRO_HOSTNAME:-$(uname -n)}"
host_name="${host_name%%.*}"
[[ "$host_name" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || {
    echo "Invalid host name: $host_name" >&2; exit 1;
}
# Inject into the generated adapter only, for every service it defines, so each
# container is named after its device. Keep the base roboboat alias for existing
# callers; Docker adds the actual hostname to /etc/hosts itself.
sed -i.bak -E "/^  (dev|lidar-relay):$/a\\
    hostname: ${host_name}\\
    extra_hosts: [\"${host_name}:127.0.1.1\"]
" "$work_file"
if command -v getent >/dev/null; then
    device_gids=()
    for group in dialout video render gpio; do
        device_gid="$(getent group "$group" | cut -d: -f3 || true)"
        [[ "$device_gid" =~ ^[0-9]+$ ]] && device_gids+=("$device_gid")
    done
    if ((${#device_gids[@]})); then
        gid_list="$(IFS=,; echo "${device_gids[*]}")"
        sed -i.bak "/^  dev:$/a\\
    group_add: [${gid_list}]
" "$work_file"
    fi
fi
operator_file="${HOME}/.config/mhseals/run.yaml"
if [[ "$os_type" == linux && ( -n "$selection" || -f "$operator_file" ) ]]; then
    files=(--file "$script_dir/../config/run.yaml")
    [[ ! -f "$operator_file" ]] || files+=(--file "$operator_file")
    [[ -z "$selection" ]] || files+=(--file "$selection")
    python3 -I "$script_dir/run_env.py" "${files[@]}" --adapter "$work_file" \
        --output "$script_dir/docker-compose.override.yml.new"
    mv "$script_dir/docker-compose.override.yml.new" "$script_dir/docker-compose.override.yml"
else
    mv "$work_file" "$script_dir/docker-compose.override.yml"
fi
echo "Selected ${os_type} adapter: $override_file"
