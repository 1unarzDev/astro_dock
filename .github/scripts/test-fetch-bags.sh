#!/usr/bin/env bash
# fetch_bags.sh must fall back to tar when the remote has no rsync.
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# Fake ssh: ignore the host, report no rsync and run the command locally from $HOME.
mkdir -p "$tmp/src/bag1" "$tmp/bin" "$tmp/repo"
echo data > "$tmp/src/bag1/a.mcap"
cat > "$tmp/bin/ssh" <<'SH'
#!/bin/sh
shift
case "$*" in *"command -v rsync"*) exit 1;; esac
cd "$HOME"; exec sh -c "$*"
SH
chmod +x "$tmp/bin/ssh"

cp "$repo/fetch_bags.sh" "$tmp/repo/"
FETCH_BAGS_SSH="$tmp/bin/ssh" "$tmp/repo/fetch_bags.sh" "host:$tmp/src" got
test "$(cat "$tmp/repo/bags/got/bag1/a.mcap")" = data

# A ~/ remote directory resolves against the remote home (here: HOME=$tmp).
HOME="$tmp" FETCH_BAGS_SSH="$tmp/bin/ssh" "$tmp/repo/fetch_bags.sh" "host:~/src" home
test "$(cat "$tmp/repo/bags/home/bag1/a.mcap")" = data
# A bare ~ is the remote home itself.
HOME="$tmp/src" FETCH_BAGS_SSH="$tmp/bin/ssh" "$tmp/repo/fetch_bags.sh" "host:~" bare
test "$(cat "$tmp/repo/bags/bare/bag1/a.mcap")" = data

# A quote in the remote path must not break the command.
mkdir -p "$tmp/it's"; cp -r "$tmp/src/bag1" "$tmp/it's/"
FETCH_BAGS_SSH="$tmp/bin/ssh" "$tmp/repo/fetch_bags.sh" "host:$tmp/it's" quoted
test "$(cat "$tmp/repo/bags/quoted/bag1/a.mcap")" = data

# A failed fetch leaves no empty destination directory.
! FETCH_BAGS_SSH="$tmp/bin/ssh" "$tmp/repo/fetch_bags.sh" "host:$tmp/missing" failed 2>/dev/null
test ! -e "$tmp/repo/bags/failed"
echo ok
