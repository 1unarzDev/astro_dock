#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# The active profile's network env (config/profiles/<profile>/profile.env; for RoboBoat config/boat-network.env).
network_env() { "$SCRIPT_DIR/hw.sh" env; }

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
  --components LIST         clock,docker,gpu,devices,devtools,compose,gateway,boat,jetson,operator-key
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

    echo "This stage installs Docker Engine from Docker's official repository unless Docker and Compose"
    echo "are already present, enables it, and adds $USER to the docker group."
    approve "Run the container-runtime stage?" || { SKIPPED+=(docker); return; }
    if docker compose version >/dev/null 2>&1; then
        echo "Docker and Compose are installed; keeping them."
    elif [[ "$OS_ID" == arch ]] || [[ "$OS_LIKE" == *arch* ]]; then
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
    echo "This installs NVIDIA Container Toolkit unless present and registers the nvidia runtime."
    echo "It does not install or replace the host GPU driver."
    approve "Install NVIDIA Container Toolkit?" || { SKIPPED+=(gpu); return; }
    local before
    if command -v nvidia-ctk >/dev/null 2>&1; then
        echo "NVIDIA Container Toolkit is installed; keeping it."
    elif [[ -e /etc/apt/sources.list.d/nvidia-l4t-apt-source.list ]]; then
        # JetPack's own apt source carries the toolkit build matched to L4T.
        run sudo apt-get update
        run sudo apt-get install -y nvidia-container-toolkit
    elif [[ "$OS_ID" == arch ]] || [[ "$OS_LIKE" == *arch* ]]; then
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
    before="$(cat /etc/docker/daemon.json 2>/dev/null || true)"
    run sudo nvidia-ctk runtime configure --runtime="$RUNTIME"
    # Restarting Docker restarts running containers, so only do it for a new runtime entry.
    if [[ "$RUNTIME" == docker && "$before" != "$(cat /etc/docker/daemon.json 2>/dev/null || true)" ]]; then
        run sudo systemctl restart docker
    fi
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

# Avahi, so NAME.local resolves; a boat host advertises only its IPv4 boat address (the boat
# LAN has no routable IPv6, and link-local AAAA answers hang clients), a dev host resolves.
ensure_mdns() {
    local role="$1" conf=/etc/avahi/avahi-daemon.conf
    if ! command -v avahi-daemon >/dev/null 2>&1 \
        || [[ -z "$(find /lib* /usr/lib* -maxdepth 2 -name libnss_mdns4_minimal.so.2 -print -quit 2>/dev/null)" ]]; then
        if [[ "$OS_ID" == arch ]] || [[ "$OS_LIKE" == *arch* ]]; then
            run sudo pacman -S --needed --noconfirm avahi nss-mdns
        elif [[ "$OS_ID" =~ ^(ubuntu|debian)$ ]] || [[ "$OS_LIKE" == *debian* ]]; then
            run sudo apt-get install -y avahi-daemon libnss-mdns
        else
            run sudo dnf -y install avahi nss-mdns
        fi
    fi
    run sudo systemctl enable --now avahi-daemon
    if [[ "$role" == boat ]] && ! { grep -qx 'use-ipv6=no' "$conf" && grep -qx 'publish-aaaa-on-ipv4=no' "$conf"; }; then
        run sudo sed -i -e 's/^#\?use-ipv6=.*/use-ipv6=no/' -e 's/^#\?publish-aaaa-on-ipv4=.*/publish-aaaa-on-ipv4=no/' "$conf"
        run sudo systemctl restart avahi-daemon
    fi
    if ! grep -Eq '^hosts:.*\bmdns' /etc/nsswitch.conf; then
        run sudo cp -n /etc/nsswitch.conf /etc/nsswitch.conf.astro-bak
        run sudo sed -i 's/^\(hosts:.*\bfiles\)/\1 mdns4_minimal [NOTFOUND=return]/' /etc/nsswitch.conf
    fi
}

stage_devtools() {
    heading "Developer command-line tools"
    echo "This installs Git, curl, jq, Python, basic USB/device diagnostics, and mDNS (.local) resolution."
    echo "It does not install VS Code, Node, or clone repositories."
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
    ensure_mdns dev
    COMPLETED+=(devtools)
}

stage_compose() {
    heading "Compose selection"
    echo "This selects the repository's host adapter; it does not build or pull an image."
    approve "Select the Compose adapter now?" || { SKIPPED+=(compose); return; }
    run bash "$SCRIPT_DIR/.devcontainer/prebuild.sh"
    COMPLETED+=(compose)
}

stage_operator_key() {
    heading "Operator ssh key"
    echo "The TUI starts and reads parts on the other boat hosts over ssh with ~/.config/mhseals/ssh/id_ed25519."
    echo "Creates it if missing and installs it on each registry host (./hw.sh hosts keys) the TUI cannot reach yet."
    approve "Provision the operator key on the boat hosts?" || { SKIPPED+=(operator-key); return; }
    run "$SCRIPT_DIR/hw.sh" hosts keys || { echo "Some hosts were not reached; rerun when they are up." >&2; SKIPPED+=(operator-key); return; }
    COMPLETED+=(operator-key)
}

stage_clock() {
    local servers allow="${ASTRO_NTP_ALLOW:-}" server file config service boat_server boat_subnet peers=""
    local chrony=false boat=false local_servers=() sources=/etc/chrony/astro-sources operators=() stratum=true
    # "boat" names the boat time server and subnet from the profile network env.
    read -r boat_server boat_subnet < <(. "$(network_env)"; printf '%s %s\n' "$BOAT_TIME_SERVER" "$BOAT_SUBNET")
    read -r -a operators < <(. "$(network_env)"; printf '%s\n' "${BOAT_OPERATOR_TIME_SERVERS:-}")
    servers=(${ASTRO_NTP_SERVER:-time.cloudflare.com})
    # A client of the boat server also answers the boat subnet, so astro-clock.py sees every host.
    [[ "${servers[*]}" == boat ]] && servers=("$boat_server") peers="$boat_subnet" boat=true
    # The boat server also follows an operator laptop that has internet time; "operator" serves the
    # subnet from such a laptop, only while it is itself synchronized (no local stratum).
    [[ "$allow" == boat ]] && allow="$boat_subnet"
    [[ "$allow" == operator ]] && allow="$boat_subnet" stratum=false
    # Only a time server on the boat (or another private address) is preferred and polled fast.
    for server in "${servers[@]}"; do
        if $boat || [[ "$server" =~ ^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.) ]]; then
            local_servers+=("$server")
        fi
    done
    # Listed after the loop: an operator laptop is a source, never preferred over the internet.
    [[ "${ASTRO_NTP_ALLOW:-}" == boat ]] && servers=("${operators[@]}" "${servers[@]}")
    command -v chronyd >/dev/null && ! systemctl is-active --quiet systemd-timesyncd && chrony=true
    # Serving time or following the boat server needs chrony (the boat query asks chrony): install it if missing and replace systemd-timesyncd with it.
    local switch=false
    if { [[ -n "$allow" ]] || $boat; } && ! $IMMUTABLE; then
        switch=true chrony=true
    fi
    heading "Automatic host time synchronization"
    $switch && echo "Installs chrony if missing and disables systemd-timesyncd, which cannot serve time or answer the boat query."
    echo "Points the host's existing NTP client (chrony or systemd-timesyncd) at: ${servers[*]}."
    echo "Containers share the host clock. Set ASTRO_NTP_SERVER only when a local time server is available."
    if $chrony && ((${#local_servers[@]})); then
        echo "Prefers ${local_servers[*]} and polls it every 4-16 s; public servers stay as the fallback away from it."
    fi
    if [[ ! -e /sys/class/rtc/rtc0 ]] || grep -qs Jetson /proc/device-tree/model; then
        echo "No battery-backed clock here: it boots at the last saved time and steps on the first sync."
    fi
    for server in "${servers[@]}"; do
        [[ "$server" =~ ^[a-zA-Z0-9][a-zA-Z0-9.:-]*$ ]] || { echo "Invalid NTP server: $server" >&2; exit 2; }
    done
    if [[ -n "$allow" ]]; then
        [[ "$allow" =~ ^[0-9a-fA-F.:/]+$ ]] || { echo "Invalid ASTRO_NTP_ALLOW: $allow" >&2; exit 2; }
        if $stratum; then
            echo "Serves time to $allow (chrony allow) and keeps serving at local stratum 10 when the internet is gone."
        else
            echo "Serves time to $allow (chrony allow) while this host is synchronized."
        fi
    fi
    approve "Enable host time synchronization?" || { SKIPPED+=(clock); return; }
    if $switch; then
        if ! command -v chronyd >/dev/null && [[ ! -x /usr/sbin/chronyd ]]; then
            if [[ "$OS_ID" == arch ]] || [[ "$OS_LIKE" == *arch* ]]; then
                run sudo pacman -S --needed --noconfirm chrony
            elif [[ "$OS_ID" =~ ^(ubuntu|debian)$ ]] || [[ "$OS_LIKE" == *debian* ]]; then
                run sudo apt-get update
                run sudo apt-get install -y chrony
            else
                run sudo dnf -y install chrony
            fi
        fi
        if systemctl is-enabled --quiet systemd-timesyncd 2>/dev/null || systemctl is-active --quiet systemd-timesyncd; then
            run sudo systemctl disable --now systemd-timesyncd
        fi
    fi
    file="$(mktemp)"
    if $chrony; then
        config=/etc/chrony.conf service=chronyd
        [[ -f /etc/chrony/chrony.conf ]] && config=/etc/chrony/chrony.conf service=chrony
        # Tests point the stage at a scratch configuration; its directory holds astro-sources.
        [[ -z "${ASTRO_CHRONY_CONF:-}" ]] || config="$ASTRO_CHRONY_CONF" sources="${ASTRO_CHRONY_CONF%/*}/astro-sources"
        [[ -f "$config" ]] || $DRY_RUN || { echo "Cannot locate the chrony configuration." >&2; exit 1; }
        : > "$file"
        for server in "${servers[@]}"; do
            if [[ " ${local_servers[*]} " == *" $server "* ]]; then
                printf 'server %s iburst prefer minpoll 2 maxpoll 4\n' "$server" >> "$file"
            else
                printf 'server %s iburst\n' "$server" >> "$file"
            fi
        done
        [[ -z "${ASTRO_NTP_SERVER:-}" ]] || printf 'pool time.cloudflare.com iburst\n' >> "$file"
        run sudo install -D -m 0644 "$file" "$sources/boat.sources"
        # Step (not slew) the first 3 updates after a start, so a cold boot converges at once.
        printf 'makestep 1 3\n' > "$file"
        # The single boat server keeps serving with no upstream. Not orphan: it can hold off
        # serving for about 300 s after boot while it looks for orphan peers.
        [[ -z "$allow" ]] || printf 'allow %s\n' "$allow" >> "$file"
        [[ -z "$allow" ]] || ! $stratum || printf 'local stratum 10\n' >> "$file"
        [[ -z "$peers" || "$peers" == "$allow" ]] || printf 'allow %s\n' "$peers" >> "$file"
        run sudo install -D -m 0644 "$file" "$sources/astro.conf"
        run sudo rm -f "$sources/boat-serve.conf"
        grep -Fxq "confdir $sources" "$config" \
            || run sudo sh -c "printf '\nconfdir $sources\n' >> '$config'"
        grep -Fxq "sourcedir $sources" "$config" \
            || run sudo sh -c "printf '\nsourcedir $sources\n' >> '$config'"
    elif [[ -n "$allow" ]]; then
        echo "ASTRO_NTP_ALLOW needs chrony; systemd-timesyncd cannot serve time." >&2
        exit 1
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
    # The retired astro_clock container (chronyd as PID 1, host network) loops on its stale
    # /run/chronyd.pid after a power loss and holds UDP 123 against the host chrony: remove it.
    if $chrony && command -v docker >/dev/null && sudo docker container inspect astro_clock >/dev/null 2>&1; then
        run sudo docker rm -f astro_clock
    fi
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
    echo "Offsets of the LAN hosts: .devcontainer/astro-clock.py"
    COMPLETED+=(clock)
}

stage_gateway() {
    local subnet gateway_ip unit hook device
    read -r subnet gateway_ip < <(. "$(network_env)"; printf '%s %s\n' "$BOAT_SUBNET" "$GATEWAY_IP")
    heading "Boat LAN internet gateway"
    echo "Forwards and NATs $subnet through this host's normal routing (WARP when connected),"
    echo "persisted as astro-gateway.service. Run it only on the device that serves as the boat gateway."
    [[ "$subnet" =~ ^[0-9.]+/[0-9]+$ ]] || { echo "Invalid BOAT_SUBNET in $(network_env): $subnet" >&2; exit 2; }
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

stage_boat() {
    local subnet gateway_ip leases lease candidate type device="" address uuid mac reserved="" saved session_device session_ip
    read -r subnet gateway_ip leases < <(. "$(network_env)"; printf '%s %s %s\n' "$BOAT_SUBNET" "$GATEWAY_IP" "$STATIC_LEASES")
    heading "Boat LAN computer"
    echo "Moves the wired boat link (the one addressed in $subnet) to the router's DHCP reservation"
    echo "under a 120 s auto-revert, turns Wi-Fi off once that link is up and this session is not"
    echo "on Wi-Fi, advertises NAME.local over IPv4 only, and raises net.core.rmem_max to the 4 MiB"
    echo "that config/fastdds.xml requests."
    approve "Configure this host as a boat LAN computer?" || { SKIPPED+=(boat); return; }
    if command -v nmcli >/dev/null 2>&1 && [[ "$(nmcli -g RUNNING general 2>/dev/null)" == running ]]; then
        while IFS=: read -r candidate type; do
            [[ "$type" == ethernet ]] || continue
            address="$(ip -4 -o addr show dev "$candidate" to "$subnet" | awk '{print $4; exit}')"
            [[ -n "$address" ]] && { device="$candidate"; break; }
        done < <(nmcli -g DEVICE,TYPE device)
    else
        echo "NetworkManager is not running; configure wired DHCP and Wi-Fi off in the OS image." >&2
    fi
    session_device=""
    if [[ -n "${SSH_CONNECTION:-}" ]]; then
        read -r _ _ session_ip _ <<<"$SSH_CONNECTION"
        session_device="$(ip -o addr show | awk -v a="$session_ip" 'index($4, a "/") == 1 {print $2; exit}')"
    fi
    if [[ -z "$device" ]]; then
        echo "No wired link is on $subnet; leaving the network unchanged."
    elif ip -4 -o addr show dev "$device" to "$subnet" | grep -q " $gateway_ip/"; then
        echo "$device holds the gateway address $gateway_ip; leaving the gateway's network unchanged."
        device=""
    else
        [[ "$session_device" == "$device" ]] \
            && echo "This session runs over $device; run on the console or under systemd-run/tmux so a dropped link cannot stop the setup." >&2
        uuid="$(nmcli -g GENERAL.CON-UUID device show "$device")"
        mac="$(cat "/sys/class/net/$device/address")"
        for lease in $leases; do
            [[ "${lease%%=*}" == "${mac,,}" ]] && reserved="${lease##*=}"
        done
        if [[ "$(nmcli -g ipv4.method,ipv4.addresses connection show uuid "$uuid" | tr '\n' ' ')" == "auto  " ]]; then
            echo "$device already takes DHCP (${address%/*})."
        else
            # Revert to the saved profile unless the reservation is confirmed within 120 s.
            saved="$(nmcli -t -f ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns connection show uuid "$uuid" \
                | sed -E 's/^([^:]*):(.*)$/\1 "\2"/' | tr '\n' ' ')"
            run sudo systemd-run --collect --unit astro-boat-revert --on-active=120 \
                sh -c "nmcli connection modify $uuid $saved && nmcli device reapply $device"
            run sudo nmcli connection modify "$uuid" ipv4.method auto ipv4.addresses "" ipv4.gateway "" ipv4.dns "" connection.autoconnect yes
            run sudo nmcli device reapply "$device"
            if ! $DRY_RUN; then
                for _ in $(seq 30); do
                    address="$(ip -4 -o addr show dev "$device" dynamic to "$subnet" | awk '{print $4; exit}')"
                    [[ -n "$address" && ( -z "$reserved" || "${address%/*}" == "$reserved" ) ]] && break
                    address=""; sleep 2
                done
                [[ -n "$address" ]] || { echo "No DHCP reservation on $device; the profile reverts in 120 s." >&2; exit 1; }
            fi
            run sudo systemctl stop astro-boat-revert.timer
        fi
        [[ -z "$reserved" ]] && echo "Add $mac to STATIC_LEASES in $(network_env) for a fixed address." >&2
    fi
    if [[ -z "$device" ]]; then
        echo "Leaving Wi-Fi on: no wired boat link."
    elif [[ -n "$session_device" && "$(nmcli -g GENERAL.TYPE device show "$session_device" 2>/dev/null)" == wifi ]]; then
        echo "Leaving Wi-Fi on: this session uses $session_device; rerun over the wired link."
    else
        # NetworkManager persists the radio state across reboots.
        run sudo nmcli radio wifi off
    fi
    ensure_mdns boat
    if $DRY_RUN; then
        quote_command sudo tee /etc/sysctl.d/90-astro-dds.conf
    else
        echo net.core.rmem_max=4194304 | sudo tee /etc/sysctl.d/90-astro-dds.conf >/dev/null
    fi
    run sudo sysctl -q -p /etc/sysctl.d/90-astro-dds.conf
    COMPLETED+=(boat)
}

# Without linger, logind's RemoveIPC deletes this user's /dev/shm (the Fast DDS segments of an
# ipc=host container) when the last login session closes.
ensure_linger() {
    [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" == yes ]] && return
    echo "Enabling linger for $USER so closing the last SSH session keeps /dev/shm."
    run sudo loginctl enable-linger "$USER"
}

stage_jetson() {
    local mode swap=/swapfile size=16G
    heading "Jetson host"
    if [[ ! -r /proc/device-tree/compatible ]] || ! grep -aqi tegra /proc/device-tree/compatible; then
        echo "Not a Jetson; skipping."
        SKIPPED+=(jetson)
        return
    fi
    # Prefer the Super power mode where the board has one.
    mode="$(sed -n 's/.*POWER_MODEL ID=\([0-9]*\) NAME=MAXN_SUPER .*/\1/p' /etc/nvpmodel.conf 2>/dev/null || true)"
    [[ -n "$mode" ]] || mode="$(sed -n 's/.*POWER_MODEL ID=\([0-9]*\) NAME=MAXN .*/\1/p' /etc/nvpmodel.conf 2>/dev/null || true)"
    echo "Sets the persistent nvpmodel power mode ${mode:-?} (MAXN), adds a $size swap file at $swap,"
    echo "and boots to the headless multi-user target (the desktop stops at the next boot)."
    [[ "$mode" =~ ^[0-9]+$ ]] || { echo "No MAXN power mode in /etc/nvpmodel.conf." >&2; exit 1; }
    approve "Configure this Jetson for headless full-power use?" || { SKIPPED+=(jetson); return; }
    # nvpmodel keeps the mode across boots; answer no to its reboot prompt and report it instead.
    if [[ "$(nvpmodel -q 2>/dev/null | sed -n 2p)" != "$mode" ]]; then
        run_shell "echo no | sudo nvpmodel -m $mode"
    fi
    if ! grep -q "^$swap " /proc/swaps; then
        [[ -e "$swap" ]] || run sudo fallocate -l "$size" "$swap"
        run sudo chmod 600 "$swap"
        run sudo mkswap "$swap"
        run sudo swapon "$swap"
    fi
    grep -q "^$swap " /etc/fstab || run sudo sh -c "printf '%s none swap sw 0 0\n' '$swap' >> /etc/fstab"
    run sudo systemctl set-default multi-user.target
    ensure_linger
    $DRY_RUN || nvpmodel -q 2>/dev/null | head -1
    COMPLETED+=(jetson)
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
    has_component boat && stage_boat
    has_component jetson && stage_jetson
    has_component operator-key && stage_operator_key

    heading "Summary"
    $DRY_RUN && printf 'Planned:   %s\n' "${COMPLETED[*]:-none}" || printf 'Completed: %s\n' "${COMPLETED[*]:-none}"
    printf 'Skipped:   %s\n' "${SKIPPED[*]:-none}"
    if [[ " ${COMPLETED[*]} " == *" docker " || " ${COMPLETED[*]} " == *" devices " ]]; then
        echo "Log out and back in before relying on new group memberships."
    fi
    echo "Rerun ./setup.linux.sh --components devices to reapply and list device access."
}

main
