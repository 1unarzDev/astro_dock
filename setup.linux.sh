#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=false
ASSUME_YES=false
NON_INTERACTIVE=false
COMPONENTS="docker,gpu,devices,devtools,compose"
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
  --components LIST         docker,gpu,devices,devtools,compose
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
    for group in dialout video render; do
        getent group "$group" >/dev/null 2>&1 && run sudo usermod -aG "$group" "$USER"
    done
    command -v udevadm >/dev/null 2>&1 && run sudo udevadm control --reload-rules
    command -v udevadm >/dev/null 2>&1 && run sudo udevadm trigger
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
    run bash .devcontainer/prebuild.sh
    COMPLETED+=(compose)
}

main() {
    detect_platform
    show_probe
    echo
    echo "No commands have been run. Each selected stage will explain and preview its commands."
    approve "Continue with this setup plan?" || { echo "Cancelled."; exit 0; }

    has_component docker && stage_docker
    has_component gpu && stage_gpu
    has_component devices && stage_devices
    has_component devtools && stage_devtools
    has_component compose && stage_compose

    heading "Summary"
    $DRY_RUN && printf 'Planned:   %s\n' "${COMPLETED[*]:-none}" || printf 'Completed: %s\n' "${COMPLETED[*]:-none}"
    printf 'Skipped:   %s\n' "${SKIPPED[*]:-none}"
    if [[ " ${COMPLETED[*]} " == *" docker " || " ${COMPLETED[*]} " == *" devices " ]]; then
        echo "Log out and back in before relying on new group memberships."
    fi
    echo "Run .devcontainer/device-diagnostics.sh to inspect Cube/ZED access."
}

main
