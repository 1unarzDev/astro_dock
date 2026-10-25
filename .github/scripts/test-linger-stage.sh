#!/usr/bin/env bash
# Checks that the jetson stage enables linger for the login user, and only when it is off.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
fail() { echo "FAIL: $*"; exit 1; }
bash -n "$root/setup.linux.sh"

sed -n '/^stage_jetson() {/,/^}/p' "$root/setup.linux.sh" | grep -qx '    ensure_linger' || fail "jetson stage does not call ensure_linger"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/bin"
log="$tmp/log"
printf '#!/bin/sh\necho "loginctl $*" >> "%s"\n[ "$1" = show-user ] && echo "$LINGER"\nexit 0\n' "$log" > "$tmp/bin/loginctl"
printf '#!/bin/sh\nexec "$@"\n' > "$tmp/bin/sudo"
chmod +x "$tmp/bin"/*

# Load only the helpers, with a real run.
DRY_RUN=false
quote_command() { :; }
eval "$(sed -n '/^run() {/,/^}/p;/^ensure_linger() {/,/^}/p' "$root/setup.linux.sh")"
export PATH="$tmp/bin:$PATH"
USER=tester

LINGER=no ensure_linger >/dev/null
grep -qx 'loginctl enable-linger tester' "$log" || fail "linger not enabled for \$USER"
: > "$log"
LINGER=yes ensure_linger >/dev/null
! grep -q enable-linger "$log" || fail "linger reissued when already enabled"
echo "linger stage ok"
