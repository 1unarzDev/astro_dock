#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

DRY_RUN=false
ASSUME_YES=false
NON_INTERACTIVE=false
COMPONENTS="clock,docker,gpu,devices,devtools,compose"
OS_ID=unknown
OS_LIKE=""
OS_VERSION=""
OS_CODENAME=""
ARCH="$(uname -m)"
IMMUTABLE=false
RUNTIME=docker
HAS_NVIDIA=false
COMPLETED=()
SKIPPED=()

usage() {
    cat <<'EOF'
Usage: ./setup.linux.sh [options]

Options:
  --dry-run                 Print commands without running them
  --components LIST         clock,docker,gpu,devices,devtools,compose,gateway,transport
  --non-interactive         Disable prompts (requires --yes)
  --yes                     Approve the overall plan and every stage
  -h, --help                Show this help
EOF
}

while (($#)); do
    case "$1" in
        --dry-run) DRY_RUN=true ;;
        --yes) ASSUME_YES=true ;;
        --non-interactive) NON_INTERACTIVE=true ;;
        --components)
            shift
            [[ $# -gt 0 ]] || { echo "--components requires a value" >&2; exit 2; }
            COMPONENTS="$1"
            ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if $NON_INTERACTIVE && ! $ASSUME_YES && ! $DRY_RUN; then
    echo "--non-interactive requires --yes (or --dry-run)" >&2
    exit 2
fi

has_component() {
    [[ ",$COMPONENTS," == *",$1,"* ]]
}

quote_command() {
    printf '  $'
    printf ' %q' "$@"
    printf '\n'
}

run() {
    quote_command "$@"
    $DRY_RUN || "$@"
}

run_shell() {
    quote_command bash -c "$1"
    $DRY_RUN || bash -c "$1"
}

approve() {
    local prompt="$1"
    $DRY_RUN && return 0
    $ASSUME_YES && return 0
    $NON_INTERACTIVE && return 1
    local answer
    read -r -p "$prompt [y/N] " answer
    [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]
}

heading() {
    printf '\n== %s ==\n' "$1"
}

detect_platform() {
    [[ -r /etc/os-release ]] || { echo "Cannot read /etc/os-release" >&2; exit 1; }
    # shellcheck disable=SC1091
    source /etc/os-release
    OS_ID="${ID:-unknown}"
    OS_LIKE="${ID_LIKE:-}"
    OS_VERSION="${VERSION_ID:-}"
    OS_CODENAME="${VERSION_CODENAME:-}"

    if command -v rpm-ostree >/dev/null 2>&1 || command -v bootc >/dev/null 2>&1 \
        || [[ "$OS_ID" =~ ^(coreos|fedora-coreos|fedora-iot)$ ]] \
        || [[ -e /run/ostree-booted ]]; then
        IMMUTABLE=true
        RUNTIME=podman
    elif [[ "$OS_ID" == "ubuntu-core" ]] || { command -v snap >/dev/null 2>&1 && snap list core >/dev/null 2>&1; }; then
        IMMUTABLE=true
        RUNTIME=docker
    fi

    if command -v nvidia-smi >/dev/null 2>&1 || { [[ -r /proc/device-tree/compatible ]] && grep -aqi tegra /proc/device-tree/compatible; }; then
        HAS_NVIDIA=true
    fi
}

show_probe() {
    heading "Detected host"
    printf 'OS:          %s %s\n' "$OS_ID" "$OS_VERSION"
    printf 'OS family:   %s\n' "${OS_LIKE:-$OS_ID}"
    printf 'Architecture:%s\n' " $ARCH"
    printf 'Immutable:   %s\n' "$IMMUTABLE"
    printf 'Runtime:     %s\n' "$RUNTIME"
    printf 'NVIDIA:      %s\n' "$HAS_NVIDIA"
    printf 'Components:  %s\n' "$COMPONENTS"
    command -v rpm-ostree >/dev/null 2>&1 && rpm-ostree status || true
    command -v bootc >/dev/null 2>&1 && bootc status || true
    [[ -r /proc/device-tree/model ]] && { printf 'Board:       '; tr -d '\0' < /proc/device-tree/model; printf '\n'; }
    return 0
}

install_docker_debian() {
    run sudo install -m 0755 -d /etc/apt/keyrings
    run_shell "curl -fsSL https://download.docker.com/linux/${OS_ID}/gpg | sudo gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg"
    run sudo chmod a+r /etc/apt/keyrings/docker.gpg
    local codename="${OS_CODENAME:-$(. /etc/os-release && echo "${VERSION_CODENAME:-}")}"
    local repo="deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${OS_ID} ${codename} stable"
    run_shell "printf '%s\\n' '$repo' | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null"
    run sudo apt-get update
    run sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

install_docker_rpm() {
    local repo_os=centos
    [[ "$OS_ID" == fedora ]] && repo_os=fedora
    run sudo dnf -y install dnf-plugins-core
    if dnf config-manager --help 2>&1 | grep -q add-repo; then
        run sudo dnf config-manager --add-repo "https://download.docker.com/linux/${repo_os}/docker-ce.repo"
    else
        run sudo dnf config-manager addrepo --from-repofile="https://download.docker.com/linux/${repo_os}/docker-ce.repo"
    fi
    run sudo dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

stage_docker() {
    heading "Container runtime"
    if $IMMUTABLE; then
        if command -v podman >/dev/null 2>&1; then
            echo "Immutable rpm-ostree/bootc host: using the existing rootful Podman installation."
            RUNTIME=podman
        elif command -v docker >/dev/null 2>&1; then
            echo "Immutable host: using the existing Docker installation."
            RUNTIME=docker
        else
            echo "No container runtime is installed. Install it through the OS image/snap configuration; this script will not mutate an immutable root." >&2
            SKIPPED+=(docker)
            return
        fi
        COMPLETED+=(docker)
        return
    fi

    echo "This stage installs Docker Engine from Docker's official repository and enables it."
    approve "Run the container-runtime stage?" || { SKIPPED+=(docker); return; }
    if [[ "$OS_ID" == arch ]] || [[ "$OS_LIKE" == *arch* ]]; then
        run sudo pacman -Syu --needed --noconfirm docker docker-buildx docker-compose
    elif [[ "$OS_ID" =~ ^(ubuntu|debian)$ ]] || [[ "$OS_LIKE" == *debian* ]]; then
        install_docker_debian
    elif [[ "$OS_ID" == fedora ]] || [[ "$OS_LIKE" == *rhel* ]] || [[ "$OS_ID" =~ ^(rhel|rocky|almalinux)$ ]]; then
        install_docker_rpm
    else
        echo "Unsupported mutable distribution: $OS_ID ($OS_LIKE)" >&2
        SKIPPED+=(docker)
        return
    fi
    run sudo systemctl enable --now docker
    run sudo groupadd -f docker
    run sudo usermod -aG docker "$USER"
    COMPLETED+=(docker)
}

stage_gpu() {
    heading "NVIDIA container runtime"
    if ! $HAS_NVIDIA; then
        echo "No NVIDIA GPU was detected; skipping toolkit installation."
        SKIPPED+=(gpu)
        return
    fi
    if $IMMUTABLE; then
        echo "NVIDIA support on an immutable host must be part of its OS image/runtime configuration; no driver or toolkit changes will be made."
        SKIPPED+=(gpu)
        return
    fi
    echo "This installs NVIDIA Container Toolkit only. It does not install or replace the host GPU driver."
    approve "Install NVIDIA Container Toolkit?" || { SKIPPED+=(gpu); return; }
    if [[ "$OS_ID" == arch ]] || [[ "$OS_LIKE" == *arch* ]]; then
        run sudo pacman -Syu --needed --noconfirm nvidia-container-toolkit
    elif [[ "$OS_ID" =~ ^(ubuntu|debian)$ ]] || [[ "$OS_LIKE" == *debian* ]]; then
        run_shell "curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg"
        run_shell "curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null"
        run sudo apt-get update
        run sudo apt-get install -y nvidia-container-toolkit
    else
        run_shell "curl -fsSL https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo | sudo tee /etc/yum.repos.d/nvidia-container-toolkit.repo >/dev/null"
        run sudo dnf -y install nvidia-container-toolkit
    fi
    run sudo nvidia-ctk runtime configure --runtime="$RUNTIME"
    [[ "$RUNTIME" == docker ]] && run sudo systemctl restart docker
    COMPLETED+=(gpu)
}

stage_devices() {
    heading "Robot device access"
    echo "This installs scoped CubePilot/ZED/camera udev rules and adds $USER to dialout, video, and render when those groups exist."
    echo "It never recursively changes /dev. Containers remain privileged and bind the complete /dev tree."
    $IMMUTABLE && echo "The rules are written to /etc only if the host permits it; otherwise bake them into the immutable OS image."
    approve "Install device rules and group memberships?" || { SKIPPED+=(devices); return; }
    local rules_file
    rules_file="$(mktemp)"
    printf '%s\n' \
        'SUBSYSTEM=="tty", ATTRS{idVendor}=="2dae", GROUP="dialout", MODE="0666", TAG+="uaccess"' \
        'SUBSYSTEM=="usb", ATTR{idVendor}=="2b03", GROUP="video", MODE="0666", TAG+="uaccess"' \
        'SUBSYSTEM=="video4linux", GROUP="video", MODE="0666", TAG+="uaccess"' \
        > "$rules_file"
    run sudo install -m 0644 "$rules_file" /etc/udev/rules.d/99-astro-robotics.rules
    rm -f "$rules_file"
    if [[ -r "$SCRIPT_DIR/.devcontainer/99-z-astro-gpu.rules" ]] && [[ -r /proc/device-tree/compatible ]] \
        && grep -aqi tegra /proc/device-tree/compatible; then
        run sudo install -m 0644 "$SCRIPT_DIR/.devcontainer/99-z-astro-gpu.rules" /etc/udev/rules.d/99-z-astro-gpu.rules
    fi
    if [[ -r "$SCRIPT_DIR/.devcontainer/99-z-astro-zed.rules" ]]; then
        run sudo install -m 0644 "$SCRIPT_DIR/.devcontainer/99-z-astro-zed.rules" /etc/udev/rules.d/99-z-astro-zed.rules
    fi
    for group in dialout video render; do
        getent group "$group" >/dev/null 2>&1 && run sudo usermod -aG "$group" "$USER"
    done
    command -v udevadm >/dev/null 2>&1 && run sudo udevadm control --reload-rules
    command -v udevadm >/dev/null 2>&1 && run sudo udevadm trigger
    # Show attached robot devices and their access so problems are visible now.
    local device
    for device in /dev/serial/by-id/* /dev/ttyACM* /dev/video*; do
        if [[ -e "$device" ]]; then
            stat -Lc '  %n  %U:%G  %A' "$device"
        fi
    done
    COMPLETED+=(devices)
}

stage_devtools() {
    heading "Developer command-line tools"
    echo "This installs Git, curl, jq, Python, and basic USB/device diagnostics. It does not install VS Code, Node, or clone repositories."
    $IMMUTABLE && { echo "Skipping package mutation on immutable host."; SKIPPED+=(devtools); return; }
    approve "Install host developer tools?" || { SKIPPED+=(devtools); return; }
    if [[ "$OS_ID" == arch ]] || [[ "$OS_LIKE" == *arch* ]]; then
        run sudo pacman -Syu --needed --noconfirm ca-certificates curl git jq lsof python usbutils
    elif [[ "$OS_ID" =~ ^(ubuntu|debian)$ ]] || [[ "$OS_LIKE" == *debian* ]]; then
        run sudo apt-get update
        run sudo apt-get install -y ca-certificates curl git jq lsof python3 usbutils
    else
        run sudo dnf -y install ca-certificates curl git jq lsof python3 usbutils
    fi
    COMPLETED+=(devtools)
}

stage_compose() {
    heading "Compose selection"
    echo "This selects the repository's host adapter; it does not build or pull an image."
    approve "Select the Compose adapter now?" || { SKIPPED+=(compose); return; }
    run bash "$SCRIPT_DIR/.devcontainer/prebuild.sh"
    COMPLETED+=(compose)
}

stage_clock() {
    local servers=(${ASTRO_NTP_SERVER:-time.cloudflare.com}) server file config service
    heading "Automatic host time synchronization"
    echo "Points the host's existing NTP client (chrony or systemd-timesyncd) at: ${servers[*]}."
    echo "Containers share the host clock. Set ASTRO_NTP_SERVER only when a local time server is available."
    for server in "${servers[@]}"; do
        [[ "$server" =~ ^[a-zA-Z0-9][a-zA-Z0-9.:-]*$ ]] || { echo "Invalid NTP server: $server" >&2; exit 2; }
    done
    approve "Enable host time synchronization?" || { SKIPPED+=(clock); return; }
    file="$(mktemp)"
    if command -v chronyd >/dev/null && ! systemctl is-active --quiet systemd-timesyncd; then
        config=/etc/chrony.conf service=chronyd
        [[ -f /etc/chrony/chrony.conf ]] && config=/etc/chrony/chrony.conf service=chrony
        [[ -f "$config" ]] || { echo "Cannot locate the chrony configuration." >&2; exit 1; }
        printf 'server %s iburst\n' "${servers[@]}" > "$file"
        run sudo install -D -m 0644 "$file" /etc/chrony/astro-sources/boat.sources
        grep -Fxq 'sourcedir /etc/chrony/astro-sources' "$config" \
            || run sudo sh -c "printf '\nsourcedir /etc/chrony/astro-sources\n' >> '$config'"
    elif systemctl list-unit-files systemd-timesyncd.service --no-legend 2>/dev/null | grep -q timesyncd; then
        service=systemd-timesyncd
        # FallbackNTP is ignored while NTP= is set, so list public peers explicitly.
        printf '[Time]\nNTP=%s time.cloudflare.com time.google.com\nFallbackNTP=time.cloudflare.com\nPollIntervalMinSec=16\nPollIntervalMaxSec=64\n' \
            "${servers[*]}" > "$file"
        run sudo install -D -m 0644 "$file" /etc/systemd/timesyncd.conf.d/60-astro.conf
    else
        rm -f "$file"
        echo "Install chrony or systemd-timesyncd, then rerun this stage." >&2
        exit 1
    fi
    rm -f "$file"
    run sudo systemctl enable --now "$service"
    run sudo systemctl restart "$service"
    if ! $DRY_RUN; then
        for _ in $(seq 30); do
            [[ "$(timedatectl show -p NTPSynchronized --value)" == yes ]] && break
            sleep 1
        done
        [[ "$(timedatectl show -p NTPSynchronized --value)" == yes ]] \
            || echo "NTP is not synchronized yet; check the network before installing packages." >&2
    fi
    COMPLETED+=(clock)
}

stage_gateway() {
    local subnet gateway_ip unit hook device
    read -r subnet gateway_ip < <(. "$SCRIPT_DIR/config/boat-network.env"; printf '%s %s\n' "$BOAT_SUBNET" "$GATEWAY_IP")
    heading "Boat LAN internet gateway"
    echo "Forwards and NATs $subnet through this host's normal routing (WARP when connected),"
    echo "persisted as astro-gateway.service. Run it only on the device that serves as the boat gateway."
    [[ "$subnet" =~ ^[0-9.]+/[0-9]+$ ]] || { echo "Invalid BOAT_SUBNET in config/boat-network.env: $subnet" >&2; exit 2; }
    approve "Share this host's internet with $subnet?" || { SKIPPED+=(gateway); return; }
    # The boat routes internet back through us; accepting its default route loops.
    # A NetworkManager dispatcher claims the gateway address on whichever link joins the boat LAN.
    if command -v nmcli >/dev/null 2>&1 && [[ "$(nmcli -g RUNNING general 2>/dev/null)" == running ]]; then
        hook="$(mktemp)"
        sed -e "s|@SUBNET@|$subnet|g" -e "s|@GATEWAY@|$gateway_ip|g" -e "s|@PREFIX@|${subnet#*/}|g" > "$hook" <<'EOF'
#!/bin/sh
# Claim the boat gateway address on whichever wired link joins the boat LAN.
case "$2" in up|dhcp4-change) ;; *) exit 0 ;; esac
ip -4 -o addr show dev "$1" to @SUBNET@ | grep -q . || exit 0
uuid=$(nmcli -g GENERAL.CON-UUID device show "$1") && [ -n "$uuid" ] || exit 0
nmcli -g ipv4.addresses connection show uuid "$uuid" | grep -q "@GATEWAY@/" &&
    [ "$(nmcli -g ipv4.never-default connection show uuid "$uuid")" = yes ] && exit 0
nmcli connection modify uuid "$uuid" +ipv4.addresses @GATEWAY@/@PREFIX@ ipv4.never-default yes ipv6.never-default yes
nmcli device reapply "$1"
EOF
        run sudo install -m 0755 "$hook" /etc/NetworkManager/dispatcher.d/90-astro-gateway
        rm -f "$hook"
        for device in $(nmcli -g DEVICE device); do
            run sudo /etc/NetworkManager/dispatcher.d/90-astro-gateway "$device" up
        done
    fi
    unit="$(mktemp)"
    # Rules name no interfaces except docker0, which is never a target for the boat LAN.
    # They go in DOCKER-USER when Docker owns FORWARD. Doubled dollars are systemd's escape.
    cat > "$unit" <<EOF
[Unit]
Description=Share this host's internet with the boat LAN ($subnet)
After=network-pre.target docker.service iptables.service nftables.service
PartOf=docker.service iptables.service

[Service]
Type=oneshot
RemainAfterExit=yes
# Traffic follows the host's routing, so it uses WARP when connected and the
# default uplink otherwise. Each rule is added once.
ExecStart=/bin/sh -c 'c=FORWARD; iptables -nL DOCKER-USER >/dev/null 2>&1 && c=DOCKER-USER; r() { t=\$\$1; k=\$\$2; shift 2; iptables -t \$\$t -C \$\$k "\$\$@" 2>/dev/null || iptables -t \$\$t -I \$\$k 1 "\$\$@"; }; r filter \$\$c -s $subnet ! -o docker0 -j ACCEPT; r filter \$\$c -d $subnet -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT; r nat POSTROUTING -s $subnet ! -d $subnet ! -o docker0 -j MASQUERADE'
# Keep the boat LAN on the main table ahead of WARP's rule at 9999.
ExecStart=/bin/sh -c 'ip rule show priority 9000 | grep -q "to $subnet" || ip rule add priority 9000 to $subnet lookup main'

[Install]
WantedBy=multi-user.target
EOF
    run sudo install -m 0644 "$unit" /etc/systemd/system/astro-gateway.service
    rm -f "$unit"
    if $DRY_RUN; then
        quote_command sudo tee /etc/sysctl.d/90-astro-gateway.conf
    else
        echo net.ipv4.ip_forward=1 | sudo tee /etc/sysctl.d/90-astro-gateway.conf >/dev/null
    fi
    run sudo sysctl -q -p /etc/sysctl.d/90-astro-gateway.conf
    run sudo systemctl daemon-reload
    run sudo systemctl enable astro-gateway.service
    run sudo systemctl restart astro-gateway.service
    COMPLETED+=(gateway)
}

stage_transport() {
    local version=1.10.1 zip_sha=fdb64d942d4b6beccbe9b1f8a359a01a2fca95a023c27535bf97b233603fad5f
    local host distro subnet transport_ip work config unit
    host="$(uname -n)"
    case "$host" in
        blastoise*|squirtle*) host="${host%%[-.]*}" ;;
        *) echo "No transport config for host $host; expected blastoise or squirtle." >&2; exit 2 ;;
    esac
    read -r subnet transport_ip < <(. "$SCRIPT_DIR/config/boat-network.env"; printf '%s %s\n' "$BOAT_SUBNET" "$BOAT_TRANSPORT_IP")
    # Foxy only on JetPack 5; every other host runs Jazzy.
    distro=jazzy
    grep -qs 'R35' /etc/nv_tegra_release && distro=foxy
    heading "ROS 2 zenoh transport bridge"
    echo "Installs zenoh-bridge-ros2dds $version and ros-transport.service for $host (ROS $distro)."
    echo "It cross-connects the boat and Jetson DDS domains for the listed /transport topics only."
    [[ "$ARCH" == aarch64 ]] || { echo "The pinned bridge build is aarch64 only." >&2; exit 2; }
    approve "Install the transport bridge?" || { SKIPPED+=(transport); return; }
    work="$(mktemp -d)"
    config="$work/config.json5"
    unit="$work/ros-transport.service"
    sed "s/@BOAT_TRANSPORT_IP@/$transport_ip/" "$SCRIPT_DIR/config/ros-transport/$host.json5" > "$config"
    # Cyclone matches a network address with a zero host part against the interfaces,
    # so the bridge follows the boat LAN whichever address DHCP hands out.
    cat > "$unit" <<EOF
[Unit]
Description=ROS 2 zenoh transport bridge
After=network-online.target
Wants=network-online.target

[Service]
User=roboboat
Environment=ROS_DISTRO=$distro
Environment='CYCLONEDDS_URI=<CycloneDDS><Domain><General><Interfaces><NetworkInterface address="${subnet%/*}"/></Interfaces></General></Domain></CycloneDDS>'
ExecStart=/usr/local/lib/ros-transport/zenoh-bridge-ros2dds -c /etc/ros-transport/config.json5
Restart=always

[Install]
WantedBy=multi-user.target
EOF
    run curl -fsSL -o "$work/bridge.zip" "https://github.com/eclipse-zenoh/zenoh-plugin-ros2dds/releases/download/$version/zenoh-plugin-ros2dds-$version-aarch64-unknown-linux-gnu-standalone.zip"
    run_shell "echo '$zip_sha  $work/bridge.zip' | sha256sum -c -"
    run python3 -m zipfile -e "$work/bridge.zip" "$work/bridge"
    run sudo install -D -m 0755 "$work/bridge/zenoh-bridge-ros2dds" /usr/local/lib/ros-transport/zenoh-bridge-ros2dds
    run sudo install -D -m 0644 "$config" /etc/ros-transport/config.json5
    run sudo install -m 0644 "$unit" /etc/systemd/system/ros-transport.service
    rm -rf "$work"
    run sudo systemctl daemon-reload
    run sudo systemctl enable ros-transport.service
    run sudo systemctl restart ros-transport.service
    COMPLETED+=(transport)
}

main() {
    detect_platform
    show_probe
    echo
    echo "No commands have been run. Each selected stage will explain and preview its commands."
    approve "Continue with this setup plan?" || { echo "Cancelled."; exit 0; }

    has_component clock && stage_clock
    has_component docker && stage_docker
    has_component gpu && stage_gpu
    has_component devices && stage_devices
    has_component devtools && stage_devtools
    has_component compose && stage_compose
    has_component gateway && stage_gateway
    has_component transport && stage_transport

    heading "Summary"
    $DRY_RUN && printf 'Planned:   %s\n' "${COMPLETED[*]:-none}" || printf 'Completed: %s\n' "${COMPLETED[*]:-none}"
    printf 'Skipped:   %s\n' "${SKIPPED[*]:-none}"
    if [[ " ${COMPLETED[*]} " == *" docker " || " ${COMPLETED[*]} " == *" devices " ]]; then
        echo "Log out and back in before relying on new group memberships."
    fi
    echo "Rerun ./setup.linux.sh --components devices to reapply and list device access."
}

main
