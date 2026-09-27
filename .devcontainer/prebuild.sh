#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
cp "$override_file" "${script_dir}/docker-compose.override.yml"
if [[ "$os_type" == jetson ]]; then
    sed -i.bak "s/jetson-jp6-jazzy/${jetson_family}/g" "${script_dir}/docker-compose.override.yml"
    if [[ "$jetson_family" == jetson-jp5-foxy ]]; then
        sed -i.bak 's/ASTRO_ROS_DOMAIN_ID:-42/ASTRO_ROS_DOMAIN_ID:-142/' "${script_dir}/docker-compose.override.yml"
    fi
fi
host_name="${ASTRO_HOSTNAME:-$(uname -n)}"
host_name="${host_name%%.*}"
[[ "$host_name" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || {
    echo "Invalid host name: $host_name" >&2; exit 1;
}
# Inject into the generated adapter only. Keep the base roboboat alias for
# existing callers; Docker adds the actual hostname to /etc/hosts itself.
sed -i.bak "/^  dev:$/a\\
    hostname: ${host_name}
" "${script_dir}/docker-compose.override.yml"
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
" "${script_dir}/docker-compose.override.yml"
    fi
fi
rm -f "${script_dir}/docker-compose.override.yml.bak"
echo "Selected ${os_type} adapter: $override_file"
