#!/usr/bin/env bash
# Checks ros_net's address choice with the kernel route and interface list simulated.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Two interfaces on the boat subnet: Wi-Fi .157 listed first, boat link .2 routed.
cat > "$tmp/sitecustomize.py" <<'PY'
import fcntl, os, socket
ifaces = {'wlan0': '192.168.0.157', 'eth0': '192.168.0.2'}
socket.if_nameindex = lambda: list(enumerate(ifaces, 1))
fcntl.ioctl = lambda fd, req, buf: b'\0' * 20 + socket.inet_aton(ifaces[buf[:5].split(b'\0')[0].decode()]) + b'\0' * 232
def connect(self, addr):
    if os.environ.get('FAKE_NO_ROUTE'):
        raise OSError('unreachable')
    open(os.environ['FAKE_PEER'], 'w').write(addr[0])
socket.socket.connect = connect
socket.socket.getsockname = lambda self: (os.environ['FAKE_SRC'], 0)
PY

sed -n '/^ros_net() {/,/^}/p' "$root/.devcontainer/dev.bashrc" > "$tmp/ros_net.sh"
ros_net() {
  env -i PATH="$PATH" HOME="$tmp" PYTHONPATH="$tmp" XDG_RUNTIME_DIR="$tmp" ROS_WS="$root" \
    FAKE_PEER="$tmp/peer" "$@" bash -c 'ros2() { :; }; . "$1"; ros_net; echo "${ASTRO_DDS_ADDRESS-unset}"' _ "$tmp/ros_net.sh"
}
check() { [[ "$2" == "$3" ]] || { echo "FAIL: $1: got '$2', want '$3'"; exit 1; }; }

check "route source wins over first interface" "$(ros_net FAKE_SRC=192.168.0.2)" 192.168.0.2
check "boat transport ip is the route target" "$(cat "$tmp/peer")" 192.168.0.152
check "override wins" "$(ros_net FAKE_SRC=192.168.0.2 ASTRO_DDS_ADDRESS=192.168.0.157)" 192.168.0.157
check "off-subnet source falls back to scan" "$(ros_net FAKE_SRC=10.0.0.5)" 192.168.0.157
check "no route falls back to scan" "$(ros_net FAKE_SRC=192.168.0.2 FAKE_NO_ROUTE=1)" 192.168.0.157
twice="$(env -i PATH="$PATH" HOME="$tmp" PYTHONPATH="$tmp" XDG_RUNTIME_DIR="$tmp" ROS_WS="$root" FAKE_PEER="$tmp/peer" \
  FAKE_SRC=10.0.0.5 bash -c 'ros2() { :; }; . "$1"; ros_net; export FAKE_SRC=192.168.0.2; ros_net; echo "$ASTRO_DDS_ADDRESS"' _ "$tmp/ros_net.sh")"
check "inherited auto-detected address is re-detected" "$twice" 192.168.0.2
peers() {
  env -i PATH="$PATH" HOME="$tmp" PYTHONPATH="$tmp" XDG_RUNTIME_DIR="$tmp" ROS_WS="$root" \
    FAKE_PEER="$tmp/peer" "$@" bash -c 'ros2() { :; }; . "$1"; ros_net; echo "${ROS_STATIC_PEERS-unset}"' _ "$tmp/ros_net.sh"
}
check "boat hosts but this one are static peers" "$(peers FAKE_SRC=192.168.0.2)" "192.168.0.152;192.168.0.153"
check "a user's static peers win" "$(peers FAKE_SRC=192.168.0.2 ROS_STATIC_PEERS=10.1.1.1)" 10.1.1.1
off="$(env -i PATH="$PATH" HOME="$tmp" PYTHONPATH="$tmp" XDG_RUNTIME_DIR="$tmp" ROS_WS="$root" FAKE_PEER="$tmp/peer" \
  FAKE_SRC=192.168.0.2 bash -c 'ros2() { :; }; . "$1"; ros_net; ASTRO_DDS_SUBNET=10.9.0.0/16; ros_net; echo "${ROS_STATIC_PEERS-unset}"' _ "$tmp/ros_net.sh")"
check "off the boat subnet the derived peers are dropped" "$off" unset
grep -q '<leaseDuration><sec>60</sec>' "$root/config/fastdds.xml" || { echo "FAIL: fastdds.xml lease"; exit 1; }
echo "ros_net ok"
