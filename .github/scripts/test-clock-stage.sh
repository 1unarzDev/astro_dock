#!/usr/bin/env bash
# Dry-run checks for the setup.linux.sh clock stage with a stubbed chrony host.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
stubs="$(mktemp -d)"
trap 'rm -rf "$stubs"' EXIT

# chronyd exists and timesyncd is inactive, so the chrony branch is taken
printf '#!/bin/sh\nexit 0\n' > "$stubs/chronyd"
printf '#!/bin/sh\n[ "$1" = is-active ] && exit 3\nexit 0\n' > "$stubs/systemctl"
chmod +x "$stubs"/*

bash -n "$root/setup.linux.sh"
clock() { PATH="$stubs:$PATH" "$root/setup.linux.sh" --dry-run --non-interactive --components clock 2>&1 || true; }

out="$(ASTRO_NTP_ALLOW=192.168.0.0/24 clock)"
grep -q 'Serves time to 192.168.0.0/24' <<<"$out" || { echo "FAIL: allow line missing"; exit 1; }

out="$(clock)"
! grep -q 'Serves time' <<<"$out" || { echo "FAIL: allow without variable"; exit 1; }
! grep -q 'boat-serve' <<<"$out" || { echo "FAIL: serve file without variable"; exit 1; }

out="$(ASTRO_NTP_SERVER=192.168.0.152 clock)"
grep -q '192.168.0.152' <<<"$out" || { echo "FAIL: server not used"; exit 1; }
# "boat" resolves through config/boat-network.env
read -r server subnet < <(. "$root/config/boat-network.env"; echo "$BOAT_TIME_SERVER $BOAT_SUBNET")
out="$(ASTRO_NTP_SERVER=boat clock)"
grep -q "at: $server\.$" <<<"$out" || { echo "FAIL: boat server not resolved"; exit 1; }
grep -q 'Prefers it' <<<"$out" || { echo "FAIL: local server not preferred"; exit 1; }
python3 "$root/.devcontainer/astro-clock.py" --help >/dev/null
python3 - "$root" <<'EOF'
import importlib.util, sys
spec = importlib.util.spec_from_file_location('clock', sys.argv[1] + '/.devcontainer/astro-clock.py')
clock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clock)
hosts = clock.lan_hosts()
assert hosts[0][0] == 'time server' and len(hosts) >= 2, hosts
EOF
out="$(ASTRO_NTP_ALLOW=boat clock)"
grep -q "Serves time to $subnet " <<<"$out" || { echo "FAIL: boat subnet not resolved"; exit 1; }
echo "clock stage ok"
