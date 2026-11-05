#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
selection="${ASTRO_RUN_CONFIG:-}"
if (($#)); then
    [[ $# == 2 && $1 == --config && -n $2 ]] || { echo 'Usage: prebuild.sh [--config FILE]' >&2; exit 2; }
    selection="$2"
fi
hw="$script_dir/../hw.sh"
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
# container is named after its device. docker-compose.yml keeps its base alias
# (ASTRO_ALIAS); Docker adds the actual hostname to /etc/hosts itself.
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
# This workspace also at its host path, where the worktrees' .git files point (their gitdirs live here).
host_ws="$(cd "$script_dir/.." && pwd -P)"
if [[ "$host_ws" =~ ^/[A-Za-z0-9_./@+-]+$ ]]; then
    if [[ -n "$(sed -n '/^  dev:$/,/^  [^ ]/{/^    volumes:$/p;}' "$work_file")" ]]; then
        sed -i.bak "/^  dev:$/,/^  [^ ]/{/^    volumes:$/a\\
      - ${host_ws}:${host_ws}
}" "$work_file"
    else
        sed -i.bak "/^  dev:$/a\\
    volumes:\\
      - ${host_ws}:${host_ws}
" "$work_file"
    fi
fi
# The ROS domain: the vessel's ROS_DOMAIN_ID (its network env, hw.sh env), injected into the adapter
# here with the vessel itself (ASTRO_VESSEL, so the container keeps the vessel this host resolved, also
# off its LAN); else ROS 2's default 0 (docker-compose.yml, run_env.py), which a stale
# adapter, a host without a vessel and a checkout without profiles keep. ASTRO_ROS_DOMAIN_ID and
# ASTRO_VESSEL (prebuild's or Compose's environment) override them. No PyYAML here: macOS has none.
domain_fix="export ASTRO_ROS_DOMAIN_ID=N where the dev container opens (prebuild and Compose read it), or rerun prebuild with ASTRO_VESSEL=NAME (hw.sh --vessel)"
domain_default="ROS domain ${ASTRO_ROS_DOMAIN_ID:-0 (the ROS 2 default)}"
domain=""
vessel=""
reason() { sed -e 's/\x1b\[[0-9;]*m//g' -e '$!d' "$work_file.err"; }
profiles_error=""
if ! profiles_root="$("$hw" whoami --root 2>"$work_file.err")"; then
    profiles_error=1
    echo "WARNING: hw.sh cannot load the profiles ($(reason)); the dev container gets the $domain_default." >&2
elif [[ -z "$profiles_root" ]]; then
    echo "ROS domain: generic mode (no profiles), the $domain_default"
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
# The dev container's names (container, hostname alias, operator config directory): the profile's
# (hw.sh hosts compose: hosts.yaml container:) into .env, which Compose reads for docker-compose.yml's
# variables; without a profile, or one that names none, its generic defaults. Regenerated on each run.
# A failing hw.sh (bad profile, no PyYAML) also leaves the generic names, which would rename this host's
# container and config dir on the next compose up: said, with hw.sh's reason. Generic mode (no profiles) has none.
names=""
if [[ -n "$profiles_root" || -n "$profiles_error" ]] && ! names="$("$hw" hosts compose)"; then
    names=""
    echo "WARNING: hw.sh hosts compose failed (above): the dev container gets docker-compose.yml's generic names" \
        "(container, hostname alias, config dir) unless ASTRO_CONTAINER, ASTRO_ALIAS, ASTRO_CONFIG_DIR are set. Fix it and rerun prebuild." >&2
fi
{ echo '# Written by prebuild.sh from the profile (hw.sh hosts compose); rerun prebuild to change it.'
  [[ -z "$names" ]] || printf '%s\n' "$names"; } > "$script_dir/.env"
# As docker-compose.yml resolves them: the environment, else .env (the profile), else the generic default.
named() { local v; v="$(sed -n "s/^$1=//p" <<<"$names")"; printf '%s' "${!1:-${v:-$2}}"; }
config_dir="$(named ASTRO_CONFIG_DIR astro)"
container="$(named ASTRO_CONTAINER astro_dev)"
# Host operator config dir, bind-mounted by docker-compose.yml (Docker would create it root-owned).
mkdir -p "${HOME}/.config/${config_dir}" "${HOME}/worktrees" "${HOME}/.cache/wt"
[[ -z "$profiles_root" ]] || vessel="$("$hw" whoami 2>/dev/null | sed -n 's/^vessel  *\([a-z0-9][a-z0-9_-]*\) (.*/\1/p')"
# The registry host this machine is (by hostname or MAC; never a --host or generic guess) as ASTRO_HOST,
# for what names the machine inside a container (the operator heartbeat); hw.sh there resolves the same.
astro_host=""
[[ -z "$profiles_root" ]] || astro_host="$("$hw" whoami 2>/dev/null | sed -n 's/^host  *\([A-Za-z0-9][A-Za-z0-9._-]*\) (\(hostname\|MAC\) .*/\1/p')"
if [[ -n "$domain" ]]; then
    [[ "$domain" =~ ^[0-9]+$ ]] && ((domain <= 232)) || { echo "Invalid ROS_DOMAIN_ID in the vessel's network env: $domain" >&2; exit 1; }
    echo "ROS domain $domain (the vessel's network env)${ASTRO_ROS_DOMAIN_ID:+; ASTRO_ROS_DOMAIN_ID=$ASTRO_ROS_DOMAIN_ID overrides it}"
    printf 'ros:\n  domain_id: %s\n' "$domain" > "$work_file.domain"
fi
# With the vessel, its network env and registry (ASTRO_NETWORK_ENV, ASTRO_REGISTRY) as container paths,
# for the processes no shell starts (docker exec without bash, a container without BASH_ENV), when they lie
# in this checkout (the workspace mount carries them). Literal: the host's own values are host paths.
network_env=""
registry=""
if [[ -n "$vessel" ]]; then
    astro_root="$(cd "$script_dir/.." && pwd -P)"
    ws="$(sed -n '/^ *ROS_WS: */{s///p;q;}' "$script_dir/docker-compose.yml")"
    in_ws() { [[ -n "$ws" && "$1" == "$astro_root"/* && "$1" =~ ^[A-Za-z0-9_./@+-]+$ ]] && printf '%s/%s' "$ws" "${1#"$astro_root"/}"; }
    network_env="$(in_ws "$("$hw" env 2>/dev/null)")" || network_env=""
    registry="$(in_ws "$("$hw" whoami --registry 2>/dev/null)")" || registry=""
fi
if [[ -n "$domain$vessel$astro_host" ]]; then
    [[ -z "$vessel" ]] || echo "Vessel $vessel for the dev container${ASTRO_VESSEL:+ (ASTRO_VESSEL=$ASTRO_VESSEL there overrides it)}"
    lines=()
    [[ -z "$astro_host" ]] || lines+=("      ASTRO_HOST: \"\${ASTRO_HOST:-$astro_host}\"")
    [[ -z "$domain" ]] || lines+=("      ROS_DOMAIN_ID: \"\${ASTRO_ROS_DOMAIN_ID:-$domain}\"")
    [[ -z "$vessel" ]] || lines+=("      ASTRO_VESSEL: \"\${ASTRO_VESSEL:-$vessel}\"")
    [[ -z "$network_env" ]] || lines+=("      ASTRO_NETWORK_ENV: \"$network_env\"")
    [[ -z "$registry" ]] || lines+=("      ASTRO_REGISTRY: \"$registry\"")
    lines="$(printf '%s\n' "${lines[@]}")"
    # Into services.dev.environment (a mapping in every adapter), replacing an adapter's own values.
    awk -v lines="$lines" '
        { line[NR] = $0 }
        END {
            for (i = 1; i <= NR; i++) if (line[i] == "  dev:") { start = i; break }
            if (!start) { print "prebuild: the adapter has no services.dev" > "/dev/stderr"; exit 1 }
            for (stop = start + 1; stop <= NR && line[stop] !~ /^([^ ]|  ?[^ ])/; stop++)
                if (line[stop] == "    environment:") env = stop
            for (i = 1; i <= NR; i++) {
                if (i > start && i < stop && line[i] ~ /^      (ROS_DOMAIN_ID|ASTRO_VESSEL|ASTRO_HOST|ASTRO_NETWORK_ENV|ASTRO_REGISTRY):/) continue
                print line[i]
                if (env ? i == env : i == start) {
                    if (!env) print "    environment:"
                    print lines
                }
            }
        }' "$work_file" > "$work_file.bak" && mv "$work_file.bak" "$work_file"
fi
operator_file="${HOME}/.config/${config_dir}/run.yaml"
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
# Pull the dev image when the registry has a newer one than this machine (dev-image.sh: the one resolver
# of the dev image, which the testing preview and sim use too).
"$script_dir/dev-image.sh" --pull >/dev/null || true
# Warn when the running dev container is not built on the image its tag now names (after a
# pull). Dev Containers adds a UID layer on top, so the tag's layers must prefix the container's.
compose=(docker compose -f "$script_dir/docker-compose.yml" -f "$script_dir/docker-compose.override.yml")
running="$(docker inspect --format '{{.Image}}' "$container" 2>/dev/null || true)"
if [[ -n "$running" ]]; then
    expected="$("${compose[@]}" config --images dev 2>/dev/null || true)"
    layers() { docker image inspect --format '{{json .RootFS.Layers}}' "$1" 2>/dev/null || true; }
    tag_layers="$(layers "$expected")"
    [[ -z "$tag_layers" || "$(layers "$running")" == "${tag_layers%]}"* ]] \
        || echo "WARNING: $container is not running the current $expected; recreate it with compose up -d dev." >&2
fi
