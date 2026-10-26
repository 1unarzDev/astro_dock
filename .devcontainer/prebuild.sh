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
if [[ "$os_type" == jetson ]] && ! grep -q '^# R36 ' /etc/nv_tegra_release; then
    echo 'Unsupported JetPack: expected JetPack 6 / L4T R36 (JetPack 5 is on legacy/jp5-foxy).' >&2; exit 1
fi
if [[ -n "$selection" && "$os_type" != linux ]]; then
    echo 'Selected YAML supports Linux static off-boat only.' >&2; exit 2
fi
# Stage every derivation; selected validation never damages the active adapter.
work_file="$(mktemp "${script_dir}/.adapter.XXXXXX")"
trap 'rm -f "$work_file" "$work_file.bak" "$work_file.domain" "$work_file.err" "$script_dir/docker-compose.override.yml.new"' EXIT
cp "$override_file" "$work_file"
host_name="${ASTRO_HOSTNAME:-$(uname -n)}"
host_name="${host_name%%.*}"
[[ "$host_name" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || {
    echo "Invalid host name: $host_name" >&2; exit 1;
}
# Inject into the generated adapter only, for every service it defines, so each
# container is named after its device. Keep the base roboboat alias for existing
# callers; Docker adds the actual hostname to /etc/hosts itself.
sed -i.bak -E "/^  (dev):$/a\\
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
# The ROS domain: the vessel's ROS_DOMAIN_ID (its network env, hw.sh env), injected into the adapter
# here; else the fleet default of docker-compose.yml and config/run.yaml, which a stale adapter, a
# host without a vessel and a checkout without the profiles submodule keep. ASTRO_ROS_DOMAIN_ID
# (prebuild's or Compose's environment) overrides either. No PyYAML here: macOS has none.
hw="$script_dir/../hw.sh"
domain_fix="export ASTRO_ROS_DOMAIN_ID=N where the dev container opens (prebuild and Compose read it), or rerun prebuild with ASTRO_VESSEL=NAME (hw.sh --vessel)"
domain_default="ROS domain ${ASTRO_ROS_DOMAIN_ID:-of the fleet default (docker-compose.yml, config/run.yaml)}"
domain=""
reason() { sed -e 's/\x1b\[[0-9;]*m//g' -e '$!d' "$work_file.err"; }
if ! profiles_root="$("$hw" whoami --root 2>"$work_file.err")"; then
    echo "WARNING: hw.sh cannot load the profiles ($(reason)); the dev container gets the $domain_default." >&2
elif [[ -z "$profiles_root" ]]; then
    if [[ -z "${ASTRO_PROFILES:-}" ]] && grep -qs '^\[submodule "profiles"\]' "$script_dir/../.gitmodules"; then
        {
            echo "WARNING: ======================================================================"
            echo "WARNING: The profiles submodule is declared but not initialised: no vessel network env,"
            echo "WARNING: so this dev container gets the $domain_default, not a vessel's own."
            echo "WARNING: Run git submodule update --init profiles and rerun prebuild, or $domain_fix."
            echo "WARNING: ======================================================================"
        } >&2
    else
        echo "ROS domain: generic mode (no profile root), the $domain_default"
    fi
elif ! domain="$("$hw" env ROS_DOMAIN_ID 2>"$work_file.err")"; then
    domain=""
    if grep -q 'is not a key' "$work_file.err"; then
        echo "ROS domain: the vessel's network env names no ROS_DOMAIN_ID, so the $domain_default"
    else
        {
            echo "WARNING: This host resolves no vessel (see ./hw.sh whoami): $(reason)"
            echo "WARNING: The dev container gets the $domain_default. An operator laptop off the boat LAN or"
            echo "WARNING: prebuilt with --host and no vessel: $domain_fix."
        } >&2
    fi
fi
if [[ -n "$domain" ]]; then
    [[ "$domain" =~ ^[0-9]+$ ]] && ((domain <= 232)) || { echo "Invalid ROS_DOMAIN_ID in the vessel's network env: $domain" >&2; exit 1; }
    echo "ROS domain $domain (the vessel's network env)${ASTRO_ROS_DOMAIN_ID:+; ASTRO_ROS_DOMAIN_ID=$ASTRO_ROS_DOMAIN_ID overrides it}"
    printf 'ros:\n  domain_id: %s\n' "$domain" > "$work_file.domain"
    # Into services.dev.environment (a mapping in every adapter), replacing an adapter's own value.
    awk -v value='${ASTRO_ROS_DOMAIN_ID:-'"$domain"'}' '
        { line[NR] = $0 }
        END {
            for (i = 1; i <= NR; i++) if (line[i] == "  dev:") { start = i; break }
            if (!start) { print "prebuild: the adapter has no services.dev" > "/dev/stderr"; exit 1 }
            for (stop = start + 1; stop <= NR && line[stop] !~ /^([^ ]|  ?[^ ])/; stop++)
                if (line[stop] == "    environment:") env = stop
            for (i = 1; i <= NR; i++) {
                if (i > start && i < stop && line[i] ~ /^      ROS_DOMAIN_ID:/) continue
                print line[i]
                if (env ? i == env : i == start) {
                    if (!env) print "    environment:"
                    print "      ROS_DOMAIN_ID: \"" value "\""
                }
            }
        }' "$work_file" > "$work_file.bak" && mv "$work_file.bak" "$work_file"
fi
operator_file="${HOME}/.config/mhseals/run.yaml"
if [[ "$os_type" == linux && ( -n "$selection" || -f "$operator_file" ) ]]; then
    files=(--file "$script_dir/../config/run.yaml")
    [[ ! -f "$work_file.domain" ]] || files+=(--file "$work_file.domain")
    [[ ! -f "$operator_file" ]] || files+=(--file "$operator_file")
    [[ -z "$selection" ]] || files+=(--file "$selection")
    python3 -I "$script_dir/run_env.py" "${files[@]}" --adapter "$work_file" \
        --output "$script_dir/docker-compose.override.yml.new"
    mv "$script_dir/docker-compose.override.yml.new" "$script_dir/docker-compose.override.yml"
else
    mv "$work_file" "$script_dir/docker-compose.override.yml"
fi
echo "Selected ${os_type} adapter: $override_file"
# Warn when the running dev container is not built on the image its tag now names (after a
# pull). Dev Containers adds a UID layer on top, so the tag's layers must prefix the container's.
running="$(docker inspect --format '{{.Image}}' roboboat_dev 2>/dev/null || true)"
if [[ -n "$running" ]]; then
    expected="$(docker compose -f "$script_dir/docker-compose.yml" -f "$script_dir/docker-compose.override.yml" \
        config --images dev 2>/dev/null || true)"
    layers() { docker image inspect --format '{{json .RootFS.Layers}}' "$1" 2>/dev/null || true; }
    tag_layers="$(layers "$expected")"
    [[ -z "$tag_layers" || "$(layers "$running")" == "${tag_layers%]}"* ]] \
        || echo "WARNING: roboboat_dev is not running the current $expected; recreate it with compose up -d dev." >&2
fi
