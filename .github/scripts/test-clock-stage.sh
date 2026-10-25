#!/usr/bin/env bash
# Checks for the setup.linux.sh clock stage on a stubbed chrony host with a scratch chrony config.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
stubs="$tmp/bin" etc="$tmp/etc"
mkdir -p "$stubs" "$etc/astro-sources"
sources="$etc/astro-sources"

# chronyd exists and timesyncd is inactive, so the chrony branch is taken; sudo runs the
# command as this user, and the clock reports synchronized.
printf '#!/bin/sh\nexit 0\n' > "$stubs/chronyd"
printf '#!/bin/sh\n[ "$1" = is-active ] && exit 3\nexit 0\n' > "$stubs/systemctl"
printf '#!/bin/sh\nexec "$@"\n' > "$stubs/sudo"
printf '#!/bin/sh\necho yes\n' > "$stubs/timedatectl"
chmod +x "$stubs"/*

fail() { echo "FAIL: $*"; exit 1; }
bash -n "$root/setup.linux.sh"
# clock [--dry-run]: run the stage against the scratch config, from a clean astro-sources.
clock() {
    rm -rf "$sources" && mkdir -p "$sources" && : > "$etc/chrony.conf"
    echo 'server old' > "$sources/boat-serve.conf"
    PATH="$stubs:$PATH" ASTRO_CHRONY_CONF="$etc/chrony.conf" \
        "$root/setup.linux.sh" --non-interactive --yes --components clock "$@" 2>&1
}

read -r server subnet < <(. "$root/config/boat-network.env"; echo "$BOAT_TIME_SERVER $BOAT_SUBNET")

# The boat server (ODROID): serves the subnet at local stratum 10, not orphan.
out="$(ASTRO_NTP_ALLOW=boat clock)"
grep -q "Serves time to $subnet " <<<"$out" || fail "boat subnet not resolved"
grep -Fxq 'server time.cloudflare.com iburst' "$sources/boat.sources" || fail "public server missing"
grep -Fxq 'makestep 1 3' "$sources/astro.conf" || fail "makestep missing"
grep -Fxq "allow $subnet" "$sources/astro.conf" || fail "allow missing"
grep -Fxq 'local stratum 10' "$sources/astro.conf" || fail "local stratum missing"
! grep -q orphan "$sources/astro.conf" || fail "orphan still set"
[[ ! -e "$sources/boat-serve.conf" ]] || fail "boat-serve.conf not removed"
grep -Fxq "confdir $sources" "$etc/chrony.conf" || fail "confdir not added"
grep -Fxq "sourcedir $sources" "$etc/chrony.conf" || fail "sourcedir not added"

# A boat client: prefers the boat server, polls it fast, keeps the public fallback.
out="$(ASTRO_NTP_SERVER=boat clock)"
grep -q "at: $server\.$" <<<"$out" || fail "boat server not resolved"
grep -q 'Prefers' <<<"$out" || fail "polling message missing"
grep -Fxq "server $server iburst prefer minpoll 2 maxpoll 4" "$sources/boat.sources" || fail "boat server not preferred"
grep -Fxq 'pool time.cloudflare.com iburst' "$sources/boat.sources" || fail "public fallback missing"
grep -Fxq "allow $subnet" "$sources/astro.conf" || fail "client does not answer the subnet"
! grep -q 'local stratum' "$sources/astro.conf" || fail "client serves at local stratum"

# A public server is neither preferred nor polled fast.
out="$(ASTRO_NTP_SERVER=time.google.com clock)"
! grep -q 'Prefers' <<<"$out" || fail "polling message for a public server"
grep -Fxq 'server time.google.com iburst' "$sources/boat.sources" || fail "public server preferred"

# A private address is preferred like boat.
ASTRO_NTP_SERVER=10.1.2.3 clock >/dev/null
grep -Fxq 'server 10.1.2.3 iburst prefer minpoll 2 maxpoll 4' "$sources/boat.sources" || fail "private server not preferred"

# Defaults: no serving, and a dry run writes nothing.
out="$(clock)"
! grep -q 'Serves time' <<<"$out" || fail "allow without variable"
! grep -q 'allow' "$sources/astro.conf" || fail "allow written without variable"
out="$(ASTRO_NTP_SERVER=10.1.2.3 clock --dry-run)"
grep -q 'Prefers' <<<"$out" || fail "dry run polling message missing"
[[ ! -e "$sources/boat.sources" && -e "$sources/boat-serve.conf" ]] || fail "dry run wrote files"

# Without chrony (timesyncd active) the chrony polling message is not printed.
printf '#!/bin/sh\n[ "$1" = list-unit-files ] && echo systemd-timesyncd.service enabled\nexit 0\n' > "$stubs/systemctl"
out="$(ASTRO_NTP_SERVER=boat clock --dry-run)"
! grep -q 'Prefers' <<<"$out" || fail "polling message on the timesyncd path"
grep -q 'timesyncd.conf.d/60-astro.conf' <<<"$out" || fail "timesyncd path not taken"

python3 "$root/.devcontainer/astro-clock.py" --help >/dev/null
python3 - "$root" <<'EOF'
import importlib.util, sys
spec = importlib.util.spec_from_file_location('clock', sys.argv[1] + '/.devcontainer/astro-clock.py')
clock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clock)
hosts = clock.lan_hosts()
assert hosts[0][0] == 'time server' and len(hosts) >= 2, hosts
EOF
echo "clock stage ok"
