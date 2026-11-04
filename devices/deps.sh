#!/usr/bin/env bash
# Host dependencies, declared per repo: each repo's deps.yaml (astro_dock's and src/*/deps.yaml) lists names,
# config/host-deps.yaml maps a name to its package per package manager. No list lives in the setup scripts.
#   deps.sh names              the merged names
#   deps.sh packages [MGR]     the packages for MGR (pacman, apt, dnf, brew; default: this host's)
#   deps.sh missing            the names whose commands are not on PATH
#   deps.sh check              recommend the install command when something is missing (never installs; quiet when complete)
#   deps.sh hooks              install git post-merge/post-checkout hooks that run check in astro_dock and each src/ repo
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
TABLE="$ROOT/config/host-deps.yaml"

manager() {
    local m
    for m in pacman:pacman apt:apt-get dnf:dnf brew:brew; do
        command -v "${m#*:}" >/dev/null 2>&1 && { echo "${m%%:*}"; return; }
    done
    return 1
}

names() {
    local f
    for f in "$ROOT/deps.yaml" "$ROOT"/src/*/deps.yaml; do
        [[ -f $f ]] && sed -n 's/^[[:space:]]*-[[:space:]]*\([A-Za-z0-9.+-]*\).*/\1/p' "$f"
    done | sort -u
}

# field NAME KEY: the table's value for NAME (quotes stripped), empty if none
field() {
    awk -v n="$1" -v k="$2" '$1 == n ":" && /\{/ {
        if (match($0, "[{ ,]" k ": *(\\047[^\\047]*\\047|\\[[^]]*\\]|[^,}]*)")) {
            v = substr($0, RSTART, RLENGTH); sub(/^[{ ,]*[^:]*: */, "", v); gsub(/\047|\[|\]|,/, " ", v)
            gsub(/^ +| +$/, "", v); print v } }' "$TABLE"
}

packages() {
    local mgr="${1:-$(manager)}" n p
    for n in $(names); do
        p="$(field "$n" "$mgr")"
        [[ -n $p ]] || { echo "deps.sh: $n is not in config/host-deps.yaml" >&2; continue; }
        [[ $p == - ]] || printf '%s ' "$p"
    done
    echo
}

missing() {
    local n c mgr
    mgr="$(manager)" || mgr=
    for n in $(names); do
        [[ -z $mgr || $(field "$n" "$mgr") != - ]] || continue   # not a package on this system
        for c in $(field "$n" cmds); do
            command -v "$c" >/dev/null 2>&1 || { echo "$n"; break; }
        done
    done
}

check() {
    local gone
    gone="$(missing | tr '\n' ' ')"
    [[ -n ${gone// /} ]] || return 0
    echo "Missing host tools declared in deps.yaml: ${gone% }" >&2
    echo "Install them: $ROOT/setup.linux.sh --components deps   (macOS: $ROOT/setup.mac.sh)" >&2
}

hooks() {
    local repo dir hook
    for repo in "$ROOT" "$ROOT"/src/*/; do
        git -C "$repo" rev-parse --git-dir >/dev/null 2>&1 || continue
        dir="$(git -C "$repo" rev-parse --path-format=absolute --git-path hooks)"
        for hook in post-merge post-checkout; do
            [[ -e $dir/$hook ]] && { grep -q 'devices/deps.sh' "$dir/$hook" 2>/dev/null || echo "keeping $dir/$hook" >&2; continue; }
            mkdir -p "$dir"
            printf '#!/bin/sh\n# recommends installing newly declared host tools (never installs)\n"%s/devices/deps.sh" check >&2 || true\n' "$ROOT" >"$dir/$hook"
            chmod +x "$dir/$hook"
        done
    done
}

case "${1:-}" in
    names) names ;;
    packages) packages "${2:-}" ;;
    missing) missing ;;
    check) check "${2:-}" ;;
    hooks) hooks ;;
    *) sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; [[ -z ${1:-} ]] || exit 2 ;;
esac
