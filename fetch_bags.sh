#!/usr/bin/env bash
# Copy bags from a host into bags/<DEST> (read-only on the remote).
# Uses rsync when the remote has it, otherwise streams a tar over ssh.
# Usage: ./fetch_bags.sh HOST:REMOTE_DIR DEST_NAME
set -euo pipefail

SSH=${FETCH_BAGS_SSH:-ssh}
src=${1:?usage: fetch_bags.sh HOST:REMOTE_DIR DEST_NAME}
dest=${2:?usage: fetch_bags.sh HOST:REMOTE_DIR DEST_NAME}
host=${src%%:*}
dir=${src#*:}
dir=${dir#\~/}   # ssh starts in $HOME, and '~' would not expand once quoted
[ "$dir" = "~" ] && dir=.
out="$(cd "$(dirname "$0")" && pwd)/bags/$dest"

[ -d "$out" ] || created=1
mkdir -p "$out"
# Do not leave an empty destination behind when the fetch fails.
trap '[ $? -eq 0 ] || [ -z "${created:-}" ] || rmdir "$out" 2>/dev/null' EXIT
if $SSH "$host" 'command -v rsync >/dev/null'; then
    rsync -a -e "$SSH" "$host:$dir/" "$out/"
else
    $SSH "$host" "cd $(printf %q "$dir") && tar -cf - ." | tar -xf - -C "$out"
fi
