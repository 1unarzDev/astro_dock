#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Current images' ~/.bashrc loads dev.bashrc already; older images still need it.
grep -q 'dev\.bashrc' "$HOME/.bashrc" 2>/dev/null \
    || printf '\nsource "%s"\n' "$repo_root/.devcontainer/dev.bashrc" >> "$HOME/.bashrc"

if [[ "${ASTRO_ROSDEP_INSTALL:-0}" == "1" && -d "$repo_root/src" ]]; then
    rosdep install --from-paths "$repo_root/src" --ignore-src -y
else
    echo "[postcreate] Skipping rosdep install; set ASTRO_ROSDEP_INSTALL=1 when manifests change."
fi

echo "[postcreate] Workspace ready."
