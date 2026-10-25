#!/usr/bin/env bash
# Verify, snapshot or apply the Ubiquiti airMAX link between the gateway laptop and the boat.
set -Eeuo pipefail

PROGRAM=${0##*/}
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
STATE_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/astro_dock/ubiquiti
REVERT_SECONDS=300
# --via ships the env ahead of this script, so it is already defined there.
# shellcheck source=config/boat-network.env
[[ -n ${BOAT_SUBNET:-} ]] || source "$SCRIPT_DIR/config/boat-network.env"

info() { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Usage: $PROGRAM verify [--via HOST]
       $PROGRAM snapshot bullet|rocket
       $PROGRAM apply bullet|rocket

verify    Read-only: ping each radio, show ARP entries, and send one UBNT
          discovery probe (UDP 10001) from this host's boat-LAN address.
          --via runs it on HOST over SSH (e.g. the gateway laptop).
snapshot  Copy the radio's /tmp/system.cfg to $STATE_DIR.
apply     Snapshot, then set only the UBNT_<RADIO>_CFG keys from
          config/boat-network.env. A timer on the radio restores the snapshot
          and reboots after ${REVERT_SECONDS}s unless you type KEEP.
Run snapshot/apply from a host on the boat LAN. SSH asks for the radio password.
EOF
}

radio_var() { local name="UBNT_${1^^}_$2"; printf '%s' "${!name:-}"; }

verify() {
    local name ip mac src
    for name in bullet rocket; do
        ip=$(radio_var "$name" IP) mac=$(radio_var "$name" MAC)
        [[ -n $ip ]] || { warn "$name: UBNT_${name^^}_IP is unset"; continue; }
        if ping -c 1 -W 1 "$ip" >/dev/null 2>&1; then info "$name $ip answers ping"; else warn "$name $ip does not answer ping"; fi
        ip neigh show "$ip" | grep -qi "${mac:-unset}" && info "$name ARP matches $mac" \
            || warn "$name ARP: $(ip neigh show "$ip" | head -1)"
    done
    for ip in "$ROUTER_IP" "$BOAT_TRANSPORT_IP"; do
        ping -c 1 -W 1 "$ip" >/dev/null 2>&1 && info "$ip reachable across the link" || warn "$ip unreachable"
    done
    src=$(ip -4 route get "$ROUTER_IP" | sed -n 's/.* src \([0-9.]*\).*/\1/p')
    [[ -n $src ]] || die "No route to the boat LAN from this host"
    command -v python3 >/dev/null || die "python3 is required for the discovery probe"
    info "Expected: SSID ${UBNT_SSID:-?}, rocket ${UBNT_ROCKET_ROLE:-?}, bullet ${UBNT_BULLET_ROLE:-?}"
    info "UBNT discovery from $src (one probe, 3 s):"
    python3 -I - "$src" <<'PY'
import socket, struct, sys, time
NAMES = {0x03: "firmware", 0x0B: "hostname", 0x0C: "model", 0x0D: "ssid", 0x14: "model_full", 0x16: "version"}
MODES = {2: "station", 3: "access point"}
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
s.bind((sys.argv[1], 0))  # a bound source sends 255.255.255.255 out of that interface
s.sendto(b"\x01\x00\x00\x00", ("255.255.255.255", 10001))
s.settimeout(0.5)
seen, end = set(), time.time() + 3
while time.time() < end:
    try:
        data, (peer, _) = s.recvfrom(2048)
    except socket.timeout:
        continue
    if peer in seen or len(data) < 4 or data[0] != 1:
        continue
    seen.add(peer)
    out, i = {"reply_from": peer}, 4
    while i + 3 <= len(data):
        t, n = data[i], struct.unpack(">H", data[i + 1:i + 3])[0]
        v, i = data[i + 3:i + 3 + n], i + 3 + n
        if t == 0x02 and n == 10:
            out.setdefault("addresses", []).append("%s %s" % (v[:6].hex(":"), socket.inet_ntoa(v[6:])))
        elif t == 0x01 and n == 6:
            out["mac"] = v.hex(":")
        elif t == 0x0E and n == 1:
            out["wireless_mode"] = "%d (%s)" % (v[0], MODES.get(v[0], "other"))
        elif t in NAMES:
            out[NAMES[t]] = v.decode(errors="replace")
    for k, v in out.items():
        print("  %-14s %s" % (k, ", ".join(v) if isinstance(v, list) else v))
    print()
if not seen:
    print("  no replies")
PY
}

ssh_radio() {
    local ip=$1; shift
    mkdir -p -m 700 "$STATE_DIR"
    ssh -o ConnectTimeout=8 -o UserKnownHostsFile="$STATE_DIR/known_hosts" -o StrictHostKeyChecking=accept-new \
        -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa \
        "${UBNT_USER:-ubnt}@$ip" "$@"
}

snapshot() {
    local name=$1 ip file
    ip=$(radio_var "$name" IP)
    [[ -n $ip ]] || die "UBNT_${name^^}_IP is unset"
    file="$STATE_DIR/$name-$(date +%Y%m%d-%H%M%S).cfg"
    (umask 077; ssh_radio "$ip" 'cat /tmp/system.cfg' </dev/null >"$file") || { rm -f -- "$file"; die "Snapshot failed"; }
    grep -q '^wireless\.' "$file" || die "$file does not look like an airOS system.cfg"
    info "Saved $file (contains secrets; keep it out of the repo)" >&2
    printf '%s\n' "$file"
}

apply() {
    local name=$1 ip pairs pair key value file new answer changed=0
    ip=$(radio_var "$name" IP) pairs=$(radio_var "$name" CFG)
    [[ -n $pairs ]] || die "UBNT_${name^^}_CFG lists no keys; nothing to apply"
    file=$(snapshot "$name")
    new=$(mktemp) && trap 'rm -f -- "$new"' EXIT
    cp -- "$file" "$new"
    for pair in $pairs; do
        key=${pair%%=*} value=${pair#*=}
        [[ $key =~ ^[A-Za-z0-9_.]+$ && $pair == *=* ]] || die "Bad UBNT_${name^^}_CFG entry: $pair"
        grep -q "^$key=" "$new" || die "$key is not in the radio's config; only existing keys are changed"
        grep -qx "$key=$value" "$new" && continue
        printf '  %s: %s -> %s\n' "$key" "$(sed -n "s/^$key=//p" "$new")" "$value"
        awk -v k="$key" -v v="$value" -F= '$1 == k { print k "=" v; next } { print }' "$new" >"$new.tmp" && mv -- "$new.tmp" "$new"
        changed=1
    done
    ((changed)) || { info "$name already matches; nothing to apply"; return; }
    read -r -p "Apply to $name at $ip with a ${REVERT_SECONDS}s auto-revert? Type APPLY: " answer
    [[ $answer == APPLY ]] || { info "Cancelled; nothing changed"; return; }
    # The revert job restores the snapshot to RAM and flash, then reboots.
    ssh_radio "$ip" "cat >/tmp/astro-new.cfg || exit 1
        cp /tmp/system.cfg /tmp/astro-revert.cfg || exit 1
        sh -c 'trap \"\" HUP; sleep $REVERT_SECONDS; cp /tmp/astro-revert.cfg /tmp/system.cfg; cfgmtd -w -p /etc/; reboot' </dev/null >/dev/null 2>&1 &
        echo \$! >/tmp/astro-revert.pid
        cp /tmp/astro-new.cfg /tmp/system.cfg && /usr/etc/rc.d/rc.softrestart save" <"$new" >/dev/null \
        || warn "The session dropped during apply; the radio reverts on its own in ${REVERT_SECONDS}s"
    info "Applied. Check the link now (another terminal: $PROGRAM verify)."
    read -r -p "Type KEEP within ${REVERT_SECONDS}s to keep it; anything else lets it revert: " answer
    [[ $answer == KEEP ]] || { info "Leaving the timer to restore $file"; return; }
    ssh_radio "$ip" 'kill "$(cat /tmp/astro-revert.pid)" && cfgmtd -w -p /etc/ && rm -f /tmp/astro-revert.*' </dev/null \
        || die "Could not cancel the revert; the radio will restore $file"
    info "Kept and saved to flash."
}

case ${1:-} in
    verify)
        if [[ ${2:-} == --via ]]; then
            [[ -n ${3:-} ]] || die "--via requires a host"
            ssh "$3" 'bash -s -- verify' < <(cat "$SCRIPT_DIR/config/boat-network.env" "$0")
        else
            verify
        fi
        ;;
    snapshot) [[ ${2:-} =~ ^(bullet|rocket)$ ]] || die "Name a radio: bullet or rocket"; snapshot "$2" >/dev/null ;;
    apply) [[ ${2:-} =~ ^(bullet|rocket)$ ]] || die "Name a radio: bullet or rocket"; apply "$2" ;;
    -h|--help|'') usage ;;
    *) die "Unknown command: $1" ;;
esac
