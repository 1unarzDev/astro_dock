#!/usr/bin/env bash
# hw.sh build [--ws DIR] [--container NAME] [--dry-run] [-- COLCON_ARGS...]: the workspace's one colcon build.
#   Builds the workspace DIR (a path inside the container; default with --container: its $ROS_WS) in
#   container NAME (default: in place, e.g. a dev container shell). The same steps as hosts apply's and
#   hot's build: the workspace's colcon_defaults.yaml (copied into DIR when DIR has none, e.g. a preview
#   snapshot), its environment (DIR/.devcontainer/dev.bashrc, else /opt/ros/$ROS_DISTRO alone), stale
#   symlink-install links pruned, then colcon build --symlink-install.
#   From clean when the container's image or the colcon defaults changed since DIR's last build (a pulled
#   image leaves CMake caches on its old /opt/ros libraries), or when build/ depends on an underlay file
#   (AMENT_PREFIX_PATH outside DIR) that is gone; ASTRO_BUILD_CLEAN_UNSTAMPED=1 also cleans a
#   build/ that no stamp vouches for (a throwaway workspace; never the dev or boat workspaces). From clean
#   builds the whole workspace and drops COLCON_ARGS (a --packages-select would leave the rest uninstalled).
# Sourced (hosts apply, hot), it only defines hw_build_body: the script that runs in the container.
BUILD_HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

# hw_build_body: the build, for `bash -s -- COLCON_ARGS` in the container. Reads ASTRO_BUILD_WS (else $ROS_WS),
# ASTRO_BUILD_IMAGE (the container's image id; none: no image check, no stamp), ASTRO_BUILD_DEFAULTS
# (colcon_defaults.yaml for a workspace without one), ASTRO_BUILD_CLEAN_UNSTAMPED and ASTRO_BUILD_TAIL (the
# lines of colcon's output shown, default 20; 0: all of it).
hw_build_body() {
    cat <<'EOF'
ws=${ASTRO_BUILD_WS:-${ROS_WS:-}}
[[ -n $ws && -d $ws ]] || { echo "build: no workspace '$ws' in this container (hw.sh build --ws DIR)" >&2; exit 2; }
cd "$ws"
[[ -f colcon_defaults.yaml || -z ${ASTRO_BUILD_DEFAULTS:-} ]] || printf '%s\n' "$ASTRO_BUILD_DEFAULTS" > colcon_defaults.yaml
if [[ -f .devcontainer/dev.bashrc ]]; then
    source .devcontainer/dev.bashrc >/dev/null 2>&1 || true
else
    [[ -n ${ROS_DISTRO:-} && -f /opt/ros/$ROS_DISTRO/setup.bash ]] \
        || { echo "build: the image names no ROS_DISTRO with /opt/ros/<distro>/setup.bash" >&2; exit 2; }
    source "/opt/ros/$ROS_DISTRO/setup.bash"
fi
set -o pipefail
stamp="image ${ASTRO_BUILD_IMAGE:-} defaults $(cksum < colcon_defaults.yaml 2>/dev/null | cut -d' ' -f1)"
old=$(cat .astro-build 2>/dev/null || true) clean=''
if [[ -n ${ASTRO_BUILD_IMAGE:-} && -d build ]]; then
    if [[ -n $old && $old != "$stamp" ]]; then
        echo "build: the image or colcon defaults changed since the last build ($old); building from clean"
        clean=1
    elif [[ -z $old && ${ASTRO_BUILD_CLEAN_UNSTAMPED:-0} == 1 ]]; then
        echo "build: no stamp vouches for build/; building from clean"
        clean=1
    fi
fi
# A build/ that links against underlay files gone from this environment (a container recreated on a newer image,
# an in-place build no image stamp saw change) fails with "No rule to make target <underlay>/lib/libX.so.N"
# (11-04): build from clean. The underlay is AMENT_PREFIX_PATH outside the workspace.
if [[ -d build && -z $clean ]]; then
    gone=''
    while IFS= read -r -d: prefix; do
        [[ -n $prefix && $prefix == /* && $prefix != "$PWD"/* ]] || continue
        gone=$(find build -name build.make -path '*/CMakeFiles/*' -exec grep -ohE ": ${prefix//./\\.}/[^ :]+$" {} + \
                   2>/dev/null | cut -c3- | sort -u | while IFS= read -r f; do [[ -e $f ]] || { echo "$f"; break; }; done)
        [[ -z $gone ]] || break
    done <<< "${AMENT_PREFIX_PATH:-}:"
    if [[ -n $gone ]]; then
        echo "build: build/ depends on $gone, which this environment no longer has; building from clean"
        clean=1
    fi
fi
# From clean is the whole workspace: a package selection (hot's --packages-select) would leave every other
# package without install/, the running stack's included, under a stamp that vouches for all of it.
if [[ -n $clean ]]; then
    rm -rf build install log
    (($# == 0)) || echo "build: building the whole workspace, not only: $*"
    set --
fi
echo "pruned $(find build install -xtype l 2>/dev/null | wc -l) stale links"
find build install -xtype l -delete 2>/dev/null || true
if [[ ${ASTRO_BUILD_TAIL:-20} == 0 ]]; then colcon build --symlink-install "$@" || exit
else colcon build --symlink-install "$@" 2>&1 | tail -n "${ASTRO_BUILD_TAIL:-20}" || exit; fi
[[ -z ${ASTRO_BUILD_IMAGE:-} ]] || printf '%s\n' "$stamp" > .astro-build
EOF
}

[[ ${BASH_SOURCE[0]} == "$0" ]] || return 0
set -euo pipefail

die() { printf 'hw.sh build: %s\n' "$*" >&2; exit 2; }
ws='' container='' dry=false
while (($#)); do
    case $1 in
        --ws) [[ -n ${2:-} ]] || die '--ws requires a directory'; ws=$2; shift 2 ;;
        --container) [[ -n ${2:-} ]] || die '--container requires a name'; container=$2; shift 2 ;;
        --dry-run) dry=true; shift ;;
        -h|--help) sed -n '2,12s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"; exit 0 ;;
        --) shift; break ;;
        *) die "unknown argument $1 (hw.sh build [--ws DIR] [--container NAME] [-- COLCON_ARGS])" ;;
    esac
done
[[ -n $ws || -n $container ]] || die 'hw.sh build --ws DIR | --container NAME [-- COLCON_ARGS]'
defaults=$(cat "$BUILD_HERE/../colcon_defaults.yaml")
envs=(${ws:+-e "ASTRO_BUILD_WS=$ws"} -e "ASTRO_BUILD_DEFAULTS=$defaults"
      -e "ASTRO_BUILD_CLEAN_UNSTAMPED=${ASTRO_BUILD_CLEAN_UNSTAMPED:-0}" -e "ASTRO_BUILD_TAIL=${ASTRO_BUILD_TAIL:-20}")
if [[ -n $container ]]; then
    image=$(docker inspect --format '{{.Image}}' "$container" 2>/dev/null) \
        || die "no container $container (start it first)"
    $dry && { echo "would build ${ws:-\$ROS_WS} in $container (image $image): colcon build --symlink-install $*"; exit 0; }
    hw_build_body | docker exec -i "${envs[@]}" -e "ASTRO_BUILD_IMAGE=$image" "$container" bash -s -- "$@"
else
    $dry && { echo "would build $ws here: colcon build --symlink-install $*"; exit 0; }
    ASTRO_BUILD_WS=$ws ASTRO_BUILD_DEFAULTS=$defaults bash -c "$(hw_build_body)" build "$@"
fi
