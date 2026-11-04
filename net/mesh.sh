#!/usr/bin/env bash
# A private WARP mesh on your own Cloudflare account: cx's mesh util (docs/mesh.md in the cx repository).
set -euo pipefail

usage() {   # builtins only: --help runs with no PATH
    printf '%s\n' \
        'Usage: net/mesh.sh join URL | leave URL     put this machine on a mesh, or take it off (curl and sh, no cx)' \
        '       net/mesh.sh setup|invite|devices|revoke|teardown [ARGS]   own a mesh (python3; cx mesh when cx is installed)' \
        '  join  URL is what the mesh owner sent: <endpoint>/<code>. It installs WARP if needed and prints the ssh line.' \
        '  leave URL is the mesh endpoint; leaving restores the previous network state.' \
        '  The owner commands take --json; setup takes --name, --domain and --dry-run, and reads CLOUDFLARE_API_TOKEN.' \
        '  Without cx, the util comes from CX_MESH_SOURCE (default: the main branch of cx), cached under' \
        '  ~/.cache/cx-mesh-util.'
}

case ${1:-} in
    '' | -h | --help) usage; exit 0 ;;
    join | leave)
        [[ -n ${2:-} ]] || { usage >&2; exit 2; }
        url=${2%/}
        [[ $url == http* ]] || url=https://$url
        [[ $1 == join ]] || url=${url%/leave}/leave
        curl -fsSL "$url" | sh
        exit
        ;;
esac

if command -v cx >/dev/null 2>&1 && cx mesh --help >/dev/null 2>&1; then
    exec cx mesh "$@"
fi
src=${CX_MESH_SOURCE:-https://github.com/1unarzDev/cx/archive/refs/heads/main.tar.gz}
dir=${XDG_CACHE_HOME:-$HOME/.cache}/cx-mesh-util
if [[ ! -f $dir/mesh/mesh.py || -n ${CX_MESH_REFRESH:-} ]]; then
    tmp=$(mktemp -d)
    curl -fsSL "$src" | tar -xz -C "$tmp" --strip-components=1 --wildcards '*/mesh/*' || { rm -rf "$tmp"; exit 1; }
    mkdir -p "$dir"
    rm -rf "$dir/mesh"
    mv "$tmp/mesh" "$dir/mesh"
    rm -rf "$tmp"
fi
exec python3 "$dir/mesh/mesh.py" "$@"
