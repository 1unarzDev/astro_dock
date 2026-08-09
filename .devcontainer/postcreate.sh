#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ensure_source_line() {
    local source_file="$1"
    local target_file="$2"
    local line="source \"${source_file}\""
    grep -Fqx "$line" "$target_file" 2>/dev/null || printf '\n%s\n' "$line" >> "$target_file"
}

ensure_source_line "$repo_root/.devcontainer/dev.bashrc" "$HOME/.bashrc"
ln -sfn "$repo_root/.devcontainer/dev.helper.txt" "$HOME/.helper.txt"

if [[ "${ASTRO_ROSDEP_INSTALL:-0}" == "1" && -d "$repo_root/src" ]]; then
    mapfile -t skip_keys < <(sed '/^[[:space:]]*$/d' "$repo_root/.devcontainer/package-ignore.txt")
    rosdep_args=(install --from-paths "$repo_root/src" --ignore-src -y)
    if ((${#skip_keys[@]})); then
        rosdep_args+=(--skip-keys "${skip_keys[*]}")
    fi
    rosdep "${rosdep_args[@]}"
else
    echo "[postcreate] Skipping rosdep install; set ASTRO_ROSDEP_INSTALL=1 when manifests change."
fi

echo "[postcreate] Workspace ready."
