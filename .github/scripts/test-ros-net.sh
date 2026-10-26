#!/usr/bin/env bash
# Checks ros_net's address choice with the kernel route and interface list simulated.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"
net="$root/tests/data/profiles/a/network.env"   # fixture vessel a
trap 'rm -rf "$tmp"' EXIT

# Two interfaces on the vessel subnet (fixture vessel a): Wi-Fi .157 listed first, wired link .2 routed.
cat > "$tmp/sitecustomize.py" <<'PY'
import fcntl, os, socket
ifaces = {'wlan0': '10.0.1.157', 'eth0': '10.0.1.2'}
socket.if_nameindex = lambda: list(enumerate(ifaces, 1))
fcntl.ioctl = lambda fd, req, buf: b'\0' * 20 + socket.inet_aton(ifaces[buf[:5].split(b'\0')[0].decode()]) + b'\0' * 232
def connect(self, addr):
    if os.environ.get('FAKE_NO_ROUTE'):
        raise OSError('unreachable')
    open(os.environ['FAKE_PEER'], 'w').write(addr[0])
socket.socket.connect = connect
socket.socket.getsockname = lambda self: (os.environ['FAKE_SRC'], 0)
PY

sed -n -e '/^__astro_network_env() {/,/^}/p' -e '/^__astro_profiles_missing() {/,/^}/p' -e '/^__ASTRO_PROFILES_HINT=/p' \
  -e '/^ros_net() {/,/^}/p' "$root/.devcontainer/dev.bashrc" > "$tmp/ros_net.sh"
ros_net() {
  env -i PATH="$PATH" HOME="$tmp" PYTHONPATH="$tmp" XDG_RUNTIME_DIR="$tmp" ROS_WS="$root" ASTRO_NETWORK_ENV="$net" \
    FAKE_PEER="$tmp/peer" "$@" bash -c 'ros2() { :; }; . "$1"; ros_net; echo "${ASTRO_DDS_ADDRESS-unset}"' _ "$tmp/ros_net.sh"
}
check() { [[ "$2" == "$3" ]] || { echo "FAIL: $1: got '$2', want '$3'"; exit 1; }; }

check "route source wins over first interface" "$(ros_net FAKE_SRC=10.0.1.2)" 10.0.1.2
check "the transport ip is the route target" "$(cat "$tmp/peer")" 10.0.1.10
check "override wins" "$(ros_net FAKE_SRC=10.0.1.2 ASTRO_DDS_ADDRESS=10.0.1.157)" 10.0.1.157
check "off-subnet source falls back to scan" "$(ros_net FAKE_SRC=10.0.0.5)" 10.0.1.157
check "no route falls back to scan" "$(ros_net FAKE_SRC=10.0.1.2 FAKE_NO_ROUTE=1)" 10.0.1.157
old="$tmp/old-names.env"                          # BOAT_SUBNET, BOAT_TRANSPORT_IP: read for one release
sed -E 's/^LAN_SUBNET=/BOAT_SUBNET=/; s/^TRANSPORT_IP=/BOAT_TRANSPORT_IP=/' "$net" > "$old"
rm -f "$tmp/peer"
check "old key names: the subnet" "$(ros_net FAKE_SRC=10.0.1.2 ASTRO_NETWORK_ENV="$old")" 10.0.1.2
check "old key names: the transport ip" "$(cat "$tmp/peer" 2>/dev/null)" 10.0.1.10
twice="$(env -i PATH="$PATH" HOME="$tmp" PYTHONPATH="$tmp" XDG_RUNTIME_DIR="$tmp" ROS_WS="$root" ASTRO_NETWORK_ENV="$net" FAKE_PEER="$tmp/peer" \
  FAKE_SRC=10.0.0.5 bash -c 'ros2() { :; }; . "$1"; ros_net; export FAKE_SRC=10.0.1.2; ros_net; echo "$ASTRO_DDS_ADDRESS"' _ "$tmp/ros_net.sh")"
check "inherited auto-detected address is re-detected" "$twice" 10.0.1.2
peers() {
  env -i PATH="$PATH" HOME="$tmp" PYTHONPATH="$tmp" XDG_RUNTIME_DIR="$tmp" ROS_WS="$root" ASTRO_NETWORK_ENV="$net" \
    FAKE_PEER="$tmp/peer" "$@" bash -c 'ros2() { :; }; . "$1"; ros_net; echo "${ROS_STATIC_PEERS-unset}"' _ "$tmp/ros_net.sh"
}
check "the vessel hosts but this one are static peers" "$(peers FAKE_SRC=10.0.1.2)" "10.0.1.10;10.0.1.11"
check "a user's static peers win" "$(peers FAKE_SRC=10.0.1.2 ROS_STATIC_PEERS=10.1.1.1)" 10.1.1.1
off="$(env -i PATH="$PATH" HOME="$tmp" PYTHONPATH="$tmp" XDG_RUNTIME_DIR="$tmp" ROS_WS="$root" ASTRO_NETWORK_ENV="$net" FAKE_PEER="$tmp/peer" \
  FAKE_SRC=10.0.1.2 bash -c 'ros2() { :; }; . "$1"; ros_net; ASTRO_DDS_SUBNET=10.9.0.0/16; ros_net; echo "${ROS_STATIC_PEERS-unset}"' _ "$tmp/ros_net.sh")"
check "off the vessel subnet the derived peers are dropped" "$off" unset
grep -q '<leaseDuration><sec>60</sec>' "$root/config/fastdds.xml" || { echo "FAIL: fastdds.xml lease"; exit 1; }

# A clone pulled without the profiles submodule: generic mode. ros_net -v and a person's shell
# name the fix, an agent's shell stays silent, and ASTRO_NETWORK_ENV is never exported empty.
generic="$tmp/generic"
mkdir -p "$generic/profiles"
git -C "$root" ls-files -z | grep -zv '^profiles$' | tar -C "$root" --null -T - -cf - | tar -C "$generic" -xf -
hint='git submodule update --init profiles'
gen() {
  env -i PATH="$PATH" HOME="$tmp" PYTHONPATH="$tmp" XDG_RUNTIME_DIR="$tmp" ROS_WS="$generic" FAKE_PEER="$tmp/peer" \
    FAKE_SRC=10.0.1.2 "$@" bash -c 'ros2() { :; }; . "$1"; ros_net -v; env | grep -c "^ASTRO_NETWORK_ENV=" || true' _ "$tmp/ros_net.sh" 2>&1
}
out="$(gen)"
grep -qF "$hint" <<<"$out" || { echo "FAIL: ros_net -v without the profiles submodule: $out"; exit 1; }
check "no empty ASTRO_NETWORK_ENV exported" "$(tail -1 <<<"$out")" 0
check "an inherited empty ASTRO_NETWORK_ENV is dropped" "$(gen ASTRO_NETWORK_ENV= | tail -1)" 0
! grep -qF "$hint" <<<"$(ros_net FAKE_SRC=10.0.1.2 2>&1)" || { echo "FAIL: warned with the submodule present"; exit 1; }
{ cat "$tmp/ros_net.sh"; echo ". '$root/.devcontainer/astro-banner.sh'"; } > "$tmp/rc.sh"
shell() {
  env -i PATH="$PATH" HOME="$tmp" TERM=xterm ROS_WS="$generic" ASTRO_BANNER=0 "$@" \
    script -qec "bash --rcfile '$tmp/rc.sh' -i" /dev/null <<<exit 2>&1
}
[[ $(shell | grep -cF "$hint") == 1 ]] || { echo "FAIL: a person's shell did not get one warning line"; exit 1; }
! shell CLAUDECODE=1 | grep -qF "$hint" || { echo "FAIL: an agent shell got the warning"; exit 1; }
echo "ros_net ok"
