#!/usr/bin/env bash
# dev-image.sh [--pull] [--local]: the dev container's image, as the Compose config prebuild wrote names it
# (docker compose ... config --images dev; hw.sh image runs this).
#   --pull   first pull it when the registry has a newer one than this machine (prebuild runs this; offline or
#            ASTRO_NO_PULL=1: keep the local one)
#   --local  print the local image to run: the newest one built on that tag (Dev Containers' UID variant, its
#            layers extending the tag's), else the tag itself; fails, naming the fix, when neither is here
set -euo pipefail
dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pull=false local=false
for arg; do
    case $arg in --pull) pull=true ;; --local) local=true ;;
        *) echo 'Usage: dev-image.sh [--pull] [--local]' >&2; exit 2 ;; esac
done
override="$dir/docker-compose.override.yml"
[[ -f $override ]] || { echo "dev-image: no $override yet; run $dir/prebuild.sh" >&2; exit 2; }
tag="$(docker compose -f "$dir/docker-compose.yml" -f "$override" config --images dev 2>/dev/null | head -n 1 || true)"
[[ -n $tag ]] || { echo "dev-image: the Compose config names no dev image (docker compose -f $dir/docker-compose.yml -f $override config)" >&2; exit 2; }
# Pull the dev image when the registry has a newer one than this machine (devcontainer up never pulls
# an image it already has; a months-old local tag kept stations on an old CLI, 11-04). Offline: keep it.
# Without buildx (a separate package on some distros) or the registry's digest, docker pull itself checks
# (it downloads only what changed); the digest was unread silently, so a stale tag stayed (11-04).
if $pull && [[ $tag == */* && "${ASTRO_NO_PULL:-0}" != 1 ]]; then
    local_digest="$(docker image inspect --format '{{range .RepoDigests}}{{.}} {{end}}' "$tag" 2>/dev/null || true)"
    remote_digest="$(timeout 20 docker buildx imagetools inspect "$tag" 2>/dev/null | awk '/^Digest:/ {print $2; exit}' || true)"
    if [[ -z "$remote_digest" ]]; then
        echo "Checking the registry for a newer $tag with docker pull (no digest from docker buildx imagetools); set ASTRO_NO_PULL=1 to skip." >&2
        docker pull -q "$tag" >&2 || echo "WARNING: could not reach the registry for $tag; keeping the local image." >&2
    elif [[ "$local_digest" != *"$remote_digest"* ]]; then
        echo "Pulling the newer $tag (registry $remote_digest); set ASTRO_NO_PULL=1 to skip." >&2
        docker pull -q "$tag" >&2 || echo "WARNING: could not pull $tag; keeping the local image." >&2
    fi
fi
$local || { printf '%s\n' "$tag"; exit 0; }
layers() { docker image inspect --format '{{json .RootFS.Layers}}' "$@" 2>/dev/null || true; }
base="$(layers "$tag")"
[[ -n $base ]] || { echo "dev-image: $tag is not on this machine; pull it: $dir/prebuild.sh (or docker pull $tag)" >&2; exit 2; }
best="$tag" best_created=''
while read -r id created rest; do
    [[ $rest != "$base" && $rest == "${base%]}",* ]] || continue
    [[ $created > $best_created ]] || continue
    best="$id" best_created="$created"
done < <(docker images -q --no-trunc | sort -u | xargs -r docker image inspect \
            --format '{{.Id}} {{.Created}} {{json .RootFS.Layers}}' 2>/dev/null)
if [[ $best != "$tag" ]]; then
    name="$(docker image inspect --format '{{index .RepoTags 0}}' "$best" 2>/dev/null || true)"
    best="${name:-$best}"
fi
printf '%s\n' "$best"
