#!/usr/bin/env bash
set -euo pipefail

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

for i in $(seq 0 $((count - 1))); do
    name=$(yq e ".[$i].name" "$CONFIG_FILE")
    path=$(yq e ".[$i].path" "$CONFIG_FILE")
    url=$(yq e ".[$i].url" "$CONFIG_FILE")
    branch=$(yq e ".[$i].branch" "$CONFIG_FILE")

    read -r -p "Clone $name? (y/N): " answer
    if [[ "$answer" =~ ^[Yy]$ ]]; then
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
