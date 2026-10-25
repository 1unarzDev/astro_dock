#!/usr/bin/env bash
# Clone repositories from optional_repos.yaml, asking for each one.
#   --profile NAME   clone every entry listing NAME in its profiles without asking, then build
#                    them (here when colcon is on PATH, else inside the running roboboat_dev).
set -euo pipefail

PROFILE=""
case "${1:-}" in
    --profile) [[ -n "${2:-}" ]] || { echo "--profile requires a name" >&2; exit 2; }; PROFILE="$2" ;;
    "") ;;
    *) echo "usage: $0 [--profile NAME]" >&2; exit 2 ;;
esac

if ! command -v yq &> /dev/null; then
    echo "Installing yq..."
    VERSION="v4.50.1"
    PLATFORM=$(uname | tr '[:upper:]' '[:lower:]')
    ARCH=$(uname -m)
    case "$ARCH" in
        x86_64) ARCH="amd64" ;;
        aarch64) ARCH="arm64" ;;
        armv7l) ARCH="arm" ;;
        *) echo "Unsupported arch: $ARCH" >&2; exit 1 ;;
    esac
    
    YQ_URL="https://github.com/mikefarah/yq/releases/download/${VERSION}/yq_${PLATFORM}_${ARCH}"
    YQ_TMP="$(mktemp)"
    trap 'rm -f "$YQ_TMP"' EXIT

    if command -v curl >/dev/null 2>&1; then
        curl --fail --location --show-error --silent "$YQ_URL" --output "$YQ_TMP"
    elif command -v wget >/dev/null 2>&1; then
        wget --quiet "$YQ_URL" --output-document="$YQ_TMP"
    else
        echo "Installing yq requires either curl or wget." >&2
        exit 1
    fi

    chmod +x "$YQ_TMP"
    "$YQ_TMP" --version >/dev/null
    sudo install -m 0755 "$YQ_TMP" /usr/local/bin/yq
    rm -f "$YQ_TMP"
    trap - EXIT
fi

BASE_DIR=$(git rev-parse --show-toplevel)
CONFIG_FILE="$BASE_DIR/optional_repos.yaml"
IGNORE_FILE="$BASE_DIR/.gitignore"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "YAML config file not found: $CONFIG_FILE"
    exit 1
fi


# iterate over each YAML entry
count=$(yq e 'length' "$CONFIG_FILE")
selected=()

for i in $(seq 0 $((count - 1))); do
    name=$(yq e ".[$i].name" "$CONFIG_FILE")
    path=$(yq e ".[$i].path" "$CONFIG_FILE")
    url=$(yq e ".[$i].url" "$CONFIG_FILE")
    branch=$(yq e ".[$i].branch" "$CONFIG_FILE")

    if [[ -n "$PROFILE" ]]; then
        yq e ".[$i].profiles // [] | .[]" "$CONFIG_FILE" | grep -qxF "$PROFILE" && answer=y || answer=n
    else
        read -r -p "Clone $name? (y/N): " answer
    fi
    if [[ "$answer" =~ ^[Yy]$ ]]; then
        selected+=("$name")
        target="$BASE_DIR/$path"
        if [ ! -d "$target" ]; then
            echo "Cloning $name into $target..."
            git clone --branch "$branch" "$url" "$target"
        else
            echo "$path already exists, skipping."
        fi
        if ! grep -qxF "${path}/" "$IGNORE_FILE" 2>/dev/null; then
            echo "${path}/" >> "$IGNORE_FILE"
            echo "Added ${path}/ to .gitignore"
        fi
    fi
done

[[ -n "$PROFILE" ]] || exit 0
[[ ${#selected[@]} -gt 0 ]] || { echo "No repository lists profile $PROFILE." >&2; exit 1; }
build="colcon build --symlink-install --packages-up-to ${selected[*]}"
if command -v colcon >/dev/null 2>&1; then
    (cd "$BASE_DIR" && $build)
elif [[ "$(docker inspect -f '{{.State.Running}}' roboboat_dev 2>/dev/null)" == true ]]; then
    # BASH_ENV gives docker exec's bash the ROS environment.
    docker exec roboboat_dev bash -c "cd \"\$ROS_WS\" && $build"
else
    echo "Start roboboat_dev, then build inside it: $build"
fi
