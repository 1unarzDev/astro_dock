#!/usr/bin/env bash
# hw.sh overlay and hw.sh pyenv: test a worktree in place. The dev container mounts ~/worktrees and
# ~/.cache/wt at their host paths (and this workspace at its own, for the worktrees' .git links).
#   overlay.sh overlay PATH [--base REF] [--all] [--dry-run]
#       colcon-builds PATH's packages touched against REF (default origin/main; all of them with --all or
#       when git cannot tell) into PATH/install, an overlay on the workspace install the shell has sourced:
#       symlink-install, ccache for CMake packages when installed. Prints the line to source.
#   overlay.sh pyenv PATH
#       the PYTHONPATH that runs PATH's pure-Python packages (ament_python) from source, no colcon:
#       PYTHONPATH=$(./hw.sh pyenv PATH) python3 -m pytest PATH/...
set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
die() { printf '%s\n' "$*" >&2; exit 1; }

# all_packages ROOT: every package.xml under ROOT, repo-relative (no build/install/log or dot dirs).
all_packages() {
    (cd "$1" && find . -mindepth 1 \( -name build -o -name install -o -name log -o -name '.*' \) -prune \
        -o -name package.xml -print | sed 's|^\./||')
}

# touched ROOT BASE: the files changed since ROOT's merge base with BASE, and the untracked ones.
touched() {
    local base
    base=$(git -C "$1" merge-base "$2" HEAD 2>/dev/null) || return 1
    git -C "$1" diff --name-only "$base" -- && git -C "$1" ls-files -o --exclude-standard
}

overlay() {
    local root='' base=origin/main all=false dry=false files lines line pkgs=() cmake=false
    while (($#)); do
        case $1 in
            --base) [[ -n ${2:-} ]] || die '--base requires a ref'; base=$2; shift 2 ;;
            --all) all=true; shift ;;
            --dry-run) dry=true; shift ;;
            -h|--help) sed -n '4,8s/^# \{0,3\}//p' "${BASH_SOURCE[0]}"; exit 0 ;;
            -*) die "Unknown option $1" ;;
            *) [[ -z $root ]] || die 'One PATH only'; root=$1; shift ;;
        esac
    done
    [[ -n $root && -d $root ]] || die 'Usage: hw.sh overlay PATH [--base REF] [--all] [--dry-run]'
    root=$(cd -- "$root" && pwd -P)
    if ! $all && ! files=$(touched "$root" "$base"); then
        echo "git cannot tell what $root changed against $base: building all its packages" >&2
        all=true
    fi
    $all && files=$(all_packages "$root")
    lines=$(python3 -I "$HERE/field.py" packages "$root" <<<"$files")
    while IFS=$'\t' read -r line dir; do
        [[ -n $line ]] || continue
        pkgs+=("$line")
        [[ ! -f $dir/CMakeLists.txt ]] || cmake=true
    done <<<"$lines"
    if ((${#pkgs[@]} == 0)); then
        echo "No package of $root changed against $base: the workspace install serves it as is."
        return 0
    fi
    local cmd=(colcon --log-base "$root/log" build --base-paths "$root" --build-base "$root/build"
               --install-base "$root/install" --merge-install --symlink-install --packages-select "${pkgs[@]}")
    if $cmake && command -v ccache >/dev/null; then
        export CMAKE_C_COMPILER_LAUNCHER=ccache CMAKE_CXX_COMPILER_LAUNCHER=ccache
        echo 'ccache: on'
    fi
    echo "overlay: ${cmd[*]}"
    if ! $dry; then
        command -v colcon >/dev/null || die 'No colcon here: run hw.sh overlay in the dev container.'
        [[ -n ${AMENT_PREFIX_PATH:-} ]] || die 'No underlay sourced: source the workspace install first.'
        (cd "$root" && "${cmd[@]}")
    fi
    echo "source $root/install/setup.bash"
}

pyenv() {
    [[ $# == 1 && -d $1 ]] || die 'Usage: hw.sh pyenv PATH'
    local root dirs=() manifest
    root=$(cd -- "$1" && pwd -P)
    while read -r manifest; do
        [[ -n $manifest ]] || continue
        grep -q '<build_type>ament_python</build_type>' "$root/$manifest" && dirs+=("$root/$(dirname "$manifest")")
    done <<<"$(all_packages "$root")"
    ((${#dirs[@]})) || dirs=("$root")
    local IFS=:
    printf '%s\n' "${dirs[*]}${PYTHONPATH:+:$PYTHONPATH}"
}

case ${1:-} in
    overlay|pyenv) "$@" ;;
    *) die 'Usage: overlay.sh overlay PATH [--base REF] [--all] [--dry-run] | overlay.sh pyenv PATH' ;;
esac
