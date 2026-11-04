#!/usr/bin/env bash
set -euo pipefail

echo "Starting macOS installation..."

# Check for Homebrew
if ! command -v brew &> /dev/null; then
    echo "Homebrew not found. Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add brew to PATH for the session
    eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv 2>/dev/null)"
else
    echo "Homebrew already installed."
fi

echo "Installing packages..."
brew install --cask docker visual-studio-code
# The host tools every repo declares in its deps.yaml, as brew packages (devices/deps.sh).
brew_deps=$("$(dirname "$0")/devices/deps.sh" packages brew)
# shellcheck disable=SC2086
brew install $brew_deps

# Start Docker Desktop if not running
if ! pgrep -x "Docker" > /dev/null; then
    echo "Starting Docker Desktop..."
    open -a Docker
    # Wait until Docker is running
    while ! docker system info > /dev/null 2>&1; do
        echo "Waiting for Docker to start..."
        sleep 5
    done
fi

# Install VSCode extensions
if [[ -f .vscode/extensions.json ]]; then
    echo "Installing VSCode extensions..."
    extensions=$(jq -r '.recommendations[]' .vscode/extensions.json)
    for extension in $extensions; do
        if code --list-extensions | grep -q "$extension"; then
            echo "Extension '$extension' is already installed, skipping..."
        else
            echo "Installing '$extension'..."
            code --install-extension "$extension" || echo "Failed to install $extension"
        fi
    done
else
    echo "No .vscode/extensions.json found. Skipping extensions."
fi

echo "Adding VSCode port forwarding configuration..."
VSC_DIR="$HOME/Library/Application Support/Code/User"
VSC_CONFIG="$VSC_DIR/settings.json"
mkdir -p "$VSC_DIR"
touch "$VSC_CONFIG"
hjson -j $VSC_CONFIG > $VSC_CONFIG.tmp \
    && mv $VSC_CONFIG.tmp $VSC_CONFIG \
    && jq '.["remote.autoForwardPorts"] = false' $VSC_CONFIG > $VSC_CONFIG.tmp \
    && mv $VSC_CONFIG.tmp $VSC_CONFIG

echo "Running prebuild script..."
bash .devcontainer/prebuild.sh

echo "macOS setup complete!"