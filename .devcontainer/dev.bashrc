# This file's directory: the repository's .devcontainer, or /etc/astro in an image
# started without the repository (whose ENV sets ROS_WS).
ASTRO_DIR="${BASH_SOURCE[0]%/*}"
export ROS_WS="${ROS_WS:-${ASTRO_DIR%/.devcontainer}}"

# Load ROS, the image overlays and the built workspace. Later entries take
# precedence: each overlay was built on the ones before it, and the workspace
# overrides installed packages.
ros_env() {
  local prefix
  for prefix in "/opt/ros/${ROS_DISTRO:-jazzy}" /opt/zed_ros2 /opt/velodyne /opt/astro-extra "$ROS_WS/install"; do
    if [[ -f "$prefix/setup.bash" ]]; then
      source "$prefix/setup.bash"
    fi
  done
  # A run.yaml-managed shell keeps the projected transport; an overlay cannot add rivals.
  [[ -z "${ASTRO_RUN_MANAGED:-}" ]] || unset FASTDDS_BUILTIN_TRANSPORTS CYCLONEDDS_URI \
    ROS_LOCALHOST_ONLY ASTRO_DDS_SUBNET ASTRO_DDS_ADDRESS ASTRO_ROS_DOMAIN_ID
}
ros_env

# Reload right after the commands that change installed packages, so results are
# usable in this shell without checking before every ros2 call.
colcon() {
  command colcon "$@" || return
  if [[ "${1:-}" == build ]]; then
    ros_env
    if [[ "$PWD" != "$ROS_WS" && -f install/setup.bash ]]; then source install/setup.bash; fi
  fi
}
astro-ros-install() {
  sudo -E astro-ros-install "$@" && ros_env
}

# Jazzy replaces ROS_LOCALHOST_ONLY with an explicit discovery range. Unset
# the legacy variable as well so shells in containers created before this
# migration do not emit a warning.
if [[ -z "${ASTRO_RUN_MANAGED:-}" ]]; then
  unset ROS_LOCALHOST_ONLY
  export ROS_AUTOMATIC_DISCOVERY_RANGE="${ROS_AUTOMATIC_DISCOVERY_RANGE:-SUBNET}"
fi
if [[ -f /workspace/venv/bin/activate ]]; then
  source /workspace/venv/bin/activate
fi

# Terminals use the default DDS transport; the lidar participants opt into
# mhseals_nav/config/fastdds_large_data.xml from their launch files.
[[ -n "${ASTRO_RUN_MANAGED:-}" ]] || unset FASTDDS_BUILTIN_TRANSPORTS

# Pin DDS to this host's address on the boat subnet. A viewer laptop may also be
# on Wi-Fi, WARP or Docker networks; left to choose, Fast DDS advertises every
# interface and Cyclone binds the default-route one, so data goes nowhere while
# discovery still lists the topics. Detected again by each new shell and by
# rviz2, so a changed adapter or address only needs the process restarted.
# Off the boat subnet (simulation, home) the static profile applies unchanged.
# Set ASTRO_DDS_ADDRESS to force an address. Silent at shell start; ros_net -v
# prints the choice, and the banner shows it too.
ros_net() {
  [[ -z "${ASTRO_RUN_MANAGED:-}" ]] || return 0
  local profile="$ROS_WS/config/fastdds.xml" subnet address pinned boat override peers run="${XDG_RUNTIME_DIR:-/tmp}"
  if [[ ! -d "$run" || ! -w "$run" ]]; then
    # World-writable /tmp: use a private per-user directory, and only if it is ours, a real directory, 0700.
    run="/tmp/astro-$(id -u)"
    mkdir -m 700 "$run" 2>/dev/null
    [[ -d "$run" && ! -L "$run" && -O "$run" && "$(stat -c %a "$run" 2>/dev/null)" == 700 ]] || return 0
  fi
  subnet="${ASTRO_DDS_SUBNET:-$( . "$ROS_WS/config/boat-network.env" 2>/dev/null && echo "$BOAT_SUBNET")}"
  [[ "${ROS_DISTRO:-jazzy}" == jazzy && -f "$profile" ]] || return 0
  # A user-set ASTRO_DDS_ADDRESS wins; the address ros_net itself exported
  # (remembered in __ASTRO_DDS_AUTO) is not an override and is detected again.
  # Otherwise the kernel's source address toward the boat
  # (a host with Wi-Fi and the boat link both on the subnet must use the route's
  # one), else the first interface primary address (SIOCGIFADDR) in the subnet;
  # secondaries such as a bridge's own are skipped; the image has no iproute2.
  boat="$( . "$ROS_WS/config/boat-network.env" 2>/dev/null && echo "$BOAT_TRANSPORT_IP")"
  override="${ASTRO_DDS_ADDRESS:-}"
  [[ "$override" != "${__ASTRO_DDS_AUTO:-}" ]] || override=
  address="${override:-$(python3 -c '
import fcntl, ipaddress, socket, struct, sys
net = ipaddress.ip_network(sys.argv[1], strict=False)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect((sys.argv[2], 9))
    ip = ipaddress.ip_address(s.getsockname()[0])
    if ip in net:
        print(ip); sys.exit()
except (OSError, ValueError):
    pass
for _, name in socket.if_nameindex():
    try:
        raw = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name.encode()[:15]))
    except OSError:
        continue
    ip = ipaddress.ip_address(socket.inet_ntoa(raw[20:24]))
    if ip in net:
        print(ip); break' "$subnet" "$boat" 2>/dev/null)}"
  # Loopback stays allowed so localhost-only discovery, whose peers are 127.0.0.1,
  # still works; Fast DDS never announces loopback beside a whitelisted address.
  if [[ -n "$address" ]]; then
    pinned="$run/astro-fastdds-$address.xml"
    python3 - "$profile" "$pinned" "$address" <<'EOF' || return 1
import sys, xml.etree.ElementTree as ET
ns = 'http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles'
ET.register_namespace('', ns)
tree = ET.parse(sys.argv[1])
for udp in tree.iter(f'{{{ns}}}transport_descriptor'):
    if udp.findtext(f'{{{ns}}}type') == 'UDPv4':
        allow = ET.SubElement(udp, f'{{{ns}}}interfaceWhiteList')
        for address in (sys.argv[3], '127.0.0.1'):
            ET.SubElement(allow, f'{{{ns}}}address').text = address
tree.write(sys.argv[2], xml_declaration=True, encoding='UTF-8')
EOF
    profile="$pinned"
    export CYCLONEDDS_URI="<CycloneDDS><Domain><General><Interfaces><NetworkInterface address=\"$address\"/></Interfaces></General></Domain></CycloneDDS>"
    # The boat hosts (the gateway and every reserved address) are static peers: discovery
    # reaches them by unicast even when the radio drops multicast. Multicast stays on, so
    # other hosts still find each other. A user-set ROS_STATIC_PEERS wins, as for the address.
    peers="$( . "$ROS_WS/config/boat-network.env" 2>/dev/null
      for host in "$GATEWAY_IP" ${STATIC_LEASES:-}; do
        host="${host##*=}"; [[ -z "$host" || "$host" == "$address" ]] || printf '%s;' "$host"
      done)"
    if [[ -z "${ROS_STATIC_PEERS:-}" || "$ROS_STATIC_PEERS" == "${__ASTRO_DDS_PEERS:-}" ]]; then
      export ROS_STATIC_PEERS="${peers%;}" __ASTRO_DDS_PEERS="${peers%;}"
    fi
  else
    unset CYCLONEDDS_URI
    [[ "${ROS_STATIC_PEERS:-}" != "${__ASTRO_DDS_PEERS:-}" ]] || unset ROS_STATIC_PEERS __ASTRO_DDS_PEERS
  fi
  # The ros2 CLI daemon is shared by every shell and keeps the transport it
  # started with: restart it when the address or the peers differ from the last seen.
  local state="$run/astro-dds-address-$(id -u)"
  if [[ "$(cat "$state" 2>/dev/null)" != "$address ${ROS_STATIC_PEERS:-}" ]]; then
    ros2 daemon stop >/dev/null 2>&1 || true
    printf '%s' "$address ${ROS_STATIC_PEERS:-}" > "$state" 2>/dev/null || true
  fi
  export ASTRO_DDS_ADDRESS="$address"
  [[ -n "$override" ]] || export __ASTRO_DDS_AUTO="$address"
  export FASTRTPS_DEFAULT_PROFILES_FILE="$profile" FASTDDS_DEFAULT_PROFILES_FILE="$profile"
  [[ "${1:-}" != -v ]] || echo "DDS: ${address:-all interfaces (not on ${subnet:-the boat subnet})}"
  return 0
}
ros_net
export GZ_SIM_RESOURCE_PATH="${ROS_WS}/src/asv_wave_sim/gz-waves-models/models:${ROS_WS}/src/asv_wave_sim/gz-waves-models/world_models:${ROS_WS}/src/asv_wave_sim/gz-waves-models/worlds${GZ_SIM_RESOURCE_PATH:+:${GZ_SIM_RESOURCE_PATH}}"
export GZ_SIM_SYSTEM_PLUGIN_PATH="${ROS_WS}/install/lib${GZ_SIM_SYSTEM_PLUGIN_PATH:+:${GZ_SIM_SYSTEM_PLUGIN_PATH}}"

unalias rviz2 2>/dev/null || true
rviz2() {
  if [[ -n "${ASTRO_RUN_MANAGED:-}" ]]; then
    command rviz2 -d "$ROS_WS/config/dark.rviz" --stylesheet "$ROS_WS/config/dark.qss" "$@"
    return
  fi
  local middleware="${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}"
  ros_net >/dev/null
  # On the boat subnet ros_net already pins Fast DDS to one interface, the boat
  # one, so RViz stays on it. Elsewhere Cyclone keeps a viewer with Ethernet,
  # Wi-Fi and several addresses on one interface.
  if [[ -z "$ASTRO_DDS_ADDRESS" && "${ROS_DISTRO:-}" == jazzy && -f "/opt/ros/jazzy/lib/librmw_cyclonedds_cpp.so" ]]; then
    middleware=rmw_cyclonedds_cpp
  fi
  RMW_IMPLEMENTATION="$middleware" command rviz2 -d "$ROS_WS/config/dark.rviz" --stylesheet "$ROS_WS/config/dark.qss" "$@"
}
alias rqt_tf_tree="ros2 run rqt_tf_tree rqt_tf_tree"

# Browse the commands every workspace package describes in its help.yaml. A command
# confirmed there runs in this shell, with its functions, and lands in the history.
help() {
  local out line
  out="$(mktemp)" || return
  ASTRO_HELP_OUT="$out" python3 -I "$ASTRO_DIR/astro-help.py" "$@"
  line="$(<"$out")"
  rm -f "$out"
  [[ -n "$line" ]] || return 0
  history -s "$line"
  eval "$line"
}

[[ ! -f "$ASTRO_DIR/astro-banner.sh" ]] || source "$ASTRO_DIR/astro-banner.sh"

