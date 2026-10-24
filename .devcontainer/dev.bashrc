# Selected startup must refuse before overlays or the requested child command.
# Exit also when sourced: a failed BASH_ENV return alone would run bash -c.
if [[ -n "${ASTRO_RUN_GENERATION:-}" ]]; then
  __astro_generation="$ASTRO_RUN_GENERATION"
  __astro_frozen="$(astro-run-config --verify "$__astro_generation")" || exit 2
  astro_restore() {
    local key value frozen
    frozen="$(astro-run-config --verify "$__astro_generation" --restore)" || exit 2
    while IFS=$'\t' read -r key value; do export "$key=$value"; done <<< "$frozen"
  }
  astro_restore
fi

export ROS_WS="${ROS_WS:-$HOME/roboboat_ws}"

# Load ROS, the image overlays and the built workspace. Later entries take
# precedence: each overlay was built on the ones before it, and the workspace
# overrides installed packages.
ros_env() {
  local prefix
  for prefix in "/opt/ros/${ROS_DISTRO:-jazzy}" /opt/zed_ros2 /opt/velodyne /opt/astro-extra "$ROS_WS/install"; do
    if [[ -f "$prefix/setup.bash" ]]; then source "$prefix/setup.bash"; fi
  done
  [[ -z "${__astro_generation:-}" ]] || astro_restore
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
if [[ -z "${__astro_generation:-}" ]]; then
  unset ROS_LOCALHOST_ONLY
  export ROS_AUTOMATIC_DISCOVERY_RANGE="${ROS_AUTOMATIC_DISCOVERY_RANGE:-SUBNET}"
fi
if [[ -f /workspace/venv/bin/activate ]]; then
  source /workspace/venv/bin/activate
fi

# Terminals use the default DDS transport; the lidar participants opt into
# mhseals_nav/config/fastdds_large_data.xml from their launch files.
[[ -n "${__astro_generation:-}" ]] || unset FASTDDS_BUILTIN_TRANSPORTS

# Pin DDS to this host's address on the boat subnet. A viewer laptop may also be
# on Wi-Fi, WARP or Docker networks; left to choose, Fast DDS advertises every
# interface and Cyclone binds the default-route one, so data goes nowhere while
# discovery still lists the topics. Detected again by each new shell and by
# rviz2, so a changed adapter or address only needs the process restarted.
# Off the boat subnet (simulation, home) the static profile applies unchanged.
ros_net() {
  [[ -z "${__astro_generation:-}" ]] || return 0
  local profile="$ROS_WS/config/fastdds.xml" subnet address pinned
  subnet="${ASTRO_DDS_SUBNET:-$( . "$ROS_WS/config/boat-network.env" 2>/dev/null && echo "$BOAT_SUBNET")}"
  [[ "${ROS_DISTRO:-jazzy}" == jazzy && -f "$profile" ]] || return 0
  # Each interface's primary address (SIOCGIFADDR), so secondaries such as a
  # bridge's own are skipped; the image has no iproute2.
  address="$(python3 -c '
import fcntl, ipaddress, socket, struct, sys
net = ipaddress.ip_network(sys.argv[1], strict=False)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for _, name in socket.if_nameindex():
    try:
        raw = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name.encode()[:15]))
    except OSError:
        continue
    ip = ipaddress.ip_address(socket.inet_ntoa(raw[20:24]))
    if ip in net:
        print(ip); break' "$subnet" 2>/dev/null)"
  if [[ -n "$address" ]]; then
    pinned="${XDG_RUNTIME_DIR:-/tmp}/astro-fastdds-$address.xml"
    python3 - "$profile" "$pinned" "$address" <<'EOF' || return 1
import sys, xml.etree.ElementTree as ET
ns = 'http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles'
ET.register_namespace('', ns)
tree = ET.parse(sys.argv[1])
for udp in tree.iter(f'{{{ns}}}transport_descriptor'):
    if udp.findtext(f'{{{ns}}}type') == 'UDPv4':
        allow = ET.SubElement(ET.SubElement(udp, f'{{{ns}}}interfaceWhiteList'), f'{{{ns}}}address')
        allow.text = sys.argv[3]
tree.write(sys.argv[2], xml_declaration=True, encoding='UTF-8')
EOF
    profile="$pinned"
    export CYCLONEDDS_URI="<CycloneDDS><Domain><General><Interfaces><NetworkInterface address=\"$address\"/></Interfaces></General></Domain></CycloneDDS>"
  else
    unset CYCLONEDDS_URI
  fi
  # The ros2 CLI daemon is shared by every shell and keeps the transport it
  # started with: restart it when the address differs from the last one seen.
  local state="${XDG_RUNTIME_DIR:-/tmp}/astro-dds-address-$(id -u)"
  if [[ "$(cat "$state" 2>/dev/null)" != "$address" ]]; then
    ros2 daemon stop >/dev/null 2>&1 || true
    printf '%s' "$address" > "$state" 2>/dev/null || true
  fi
  export ASTRO_DDS_ADDRESS="$address"
  export FASTRTPS_DEFAULT_PROFILES_FILE="$profile" FASTDDS_DEFAULT_PROFILES_FILE="$profile"
  [[ $- == *i* ]] && echo "DDS: ${address:-all interfaces (not on ${subnet:-the boat subnet})}"
  return 0
}
ros_net
export GZ_SIM_RESOURCE_PATH="${ROS_WS}/src/asv_wave_sim/gz-waves-models/models:${ROS_WS}/src/asv_wave_sim/gz-waves-models/world_models:${ROS_WS}/src/asv_wave_sim/gz-waves-models/worlds${GZ_SIM_RESOURCE_PATH:+:${GZ_SIM_RESOURCE_PATH}}"
export GZ_SIM_SYSTEM_PLUGIN_PATH="${ROS_WS}/install/lib${GZ_SIM_SYSTEM_PLUGIN_PATH:+:${GZ_SIM_SYSTEM_PLUGIN_PATH}}"

unalias rviz2 2>/dev/null || true
rviz2() {
  if [[ -n "${__astro_generation:-}" ]]; then
    astro_restore
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

help() {
  cat "$HOME/.helper.txt"
}

[[ -z "${__astro_generation:-}" ]] || astro_restore
