#!/usr/bin/env bash
# Verify, snapshot or apply the Velodyne VLP16 settings in config/boat-network.env over its HTTP API.
set -Eeuo pipefail

PROGRAM=${0##*/}
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
STATE_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/astro_dock/lidar
# shellcheck source=config/boat-network.env
source "$SCRIPT_DIR/config/boat-network.env"
# LIDAR_JUMP may come from the ignored .env (see .env.example); a set variable wins.
if [[ -z ${LIDAR_JUMP+x} && -r $SCRIPT_DIR/.env ]]; then
    LIDAR_JUMP=$(sed -n 's/^LIDAR_JUMP=//p' "$SCRIPT_DIR/.env" | tail -1)
fi

info() { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Usage: $PROGRAM verify|snapshot|apply

verify    Read-only: GET the sensor's settings, status and info, print them,
          and list each LIDAR_* value in config/boat-network.env that differs.
snapshot  Save settings, status and info as one JSON file under $STATE_DIR.
apply     Snapshot, show the differing keys, and after you type APPLY POST
          only those, save, and verify. Netmask/gateway go last (save + reset).
Runs from a boat-LAN host, or through LIDAR_JUMP (e.g. tranquility) over SSH.
EOF
}

# env key|settings.json key|endpoint|form key. LIDAR_IP is checked, never posted.
MAP='LIDAR_IP|net.addr||
LIDAR_DATA_HOST|host.addr|setting/host|addr
LIDAR_DATA_PORT|host.dport|setting/host|dport
LIDAR_TELEMETRY_PORT|host.tport|setting/host|tport
LIDAR_RPM|rpm|setting|rpm
LIDAR_RETURNS|returns|setting|returns
LIDAR_FOV_START|fov.start|setting/fov|start
LIDAR_FOV_END|fov.end|setting/fov|end
LIDAR_NETMASK|net.mask|setting/net|mask
LIDAR_GATEWAY|net.gateway|setting/net|gateway'

[[ -n ${LIDAR_IP:-} ]] || die "LIDAR_IP is unset in config/boat-network.env"

# lidar_http PATH [FORM]: GET, or POST FORM, against the sensor (via LIDAR_JUMP if set).
lidar_http() {
    local cmd=(curl -fsS -m 5 "http://$LIDAR_IP/cgi/$1")
    [[ -z ${2:-} ]] || cmd+=(--data "$2")
    if [[ -n ${LIDAR_JUMP:-} ]]; then
        ssh -o BatchMode=yes -o ConnectTimeout=8 "$LIDAR_JUMP" "${cmd[*]@Q}" </dev/null
    else
        "${cmd[@]}"
    fi
}

# Flatten settings.json to key=value lines (rpm=600, host.addr=...).
flatten() {
    python3 -I -c '
import json, sys
def walk(prefix, value):
    if isinstance(value, dict):
        for key, item in value.items():
            walk(prefix + key + ".", item)
    else:
        print("%s=%s" % (prefix[:-1], value))
walk("", json.load(sys.stdin))'
}

settings() { lidar_http settings.json | flatten || die "No settings from the lidar at $LIDAR_IP${LIDAR_JUMP:+ via $LIDAR_JUMP}"; }
live() { sed -n "s/^$(sed 's/\./\\./g' <<<"$2")=//p" <<<"$1" | head -1; }

# Prints "env|json|endpoint|form|live|want" for each key that differs.
diffs() {
    local flat=$1 env json endpoint form want have
    while IFS='|' read -r env json endpoint form; do
        want=${!env:-}
        [[ -n $want ]] || continue
        have=$(live "$flat" "$json")
        [[ $have == "$want" ]] || printf '%s|%s|%s|%s|%s|%s\n' "$env" "$json" "$endpoint" "$form" "$have" "$want"
    done <<<"$MAP"
}

summary() {
    local flat=$1 status info
    status=$(lidar_http status.json | flatten) || status=''
    info=$(lidar_http info.json | flatten) || info=''
    info "$(live "$info" model) serial $(live "$info" serial), firmware $(live "$info" build.bot), MAC $(live "$flat" net.mac_addr)"
    info "net $(live "$flat" net.addr)/$(live "$flat" net.mask) gw $(live "$flat" net.gateway) dhcp $(live "$flat" net.dhcp); data to $(live "$flat" host.addr):$(live "$flat" host.dport), telemetry :$(live "$flat" host.tport)"
    info "rpm $(live "$flat" rpm), returns $(live "$flat" returns), FOV $(live "$flat" fov.start)-$(live "$flat" fov.end), phase lock $(live "$flat" phaselock.enabled) offset $(live "$flat" phaselock.offset)"
    info "laser $(live "$status" laser.state), motor $(live "$status" motor.state) at $(live "$status" motor.rpm) rpm (lock $(live "$status" motor.lock)), PPS $(live "$status" gps.pps_state)"
    [[ $(live "$flat" net.dhcp) == Off ]] || warn "DHCP is on; the sensor may leave $LIDAR_IP after a reset"
    local key
    for key in addr mask gateway; do
        [[ $(live "$flat" "net.$key") == "$(live "$flat" "active_net.$key")" ]] || warn "net.$key is saved but not active until a reset"
    done
}

verify() {
    local flat changes env json endpoint form have want
    flat=$(settings)
    summary "$flat"
    changes=$(diffs "$flat")
    if [[ -z $changes ]]; then info "Matches config/boat-network.env"; return; fi
    warn "Differs from config/boat-network.env:"
    while IFS='|' read -r env json endpoint form have want; do
        printf '  %-21s %-12s %s -> %s\n' "$env" "$json" "$have" "$want" >&2
    done <<<"$changes"
    return 3
}

snapshot() {
    local file part
    mkdir -p -m 700 "$STATE_DIR"
    file="$STATE_DIR/lidar-$(date +%Y%m%d-%H%M%S).json"
    (umask 077
        { printf '{'
          for part in settings status info; do
              [[ $part == settings ]] || printf ','
              printf '"%s":' "$part"
              lidar_http "$part.json" || exit 1
          done
          printf '}\n'; } >"$file") || { rm -f -- "$file"; die "Snapshot failed; is $LIDAR_IP reachable?"; }
    python3 -I -c 'import json, sys; json.load(open(sys.argv[1]))' "$file" || die "$file is not valid JSON"
    info "Saved $file" >&2
    printf '%s\n' "$file"
}

post() {
    lidar_http "$1" "$2" >/dev/null || die "POST /cgi/$1 $2 failed; snapshot: $3"
}

apply() {
    local flat changes file answer env json endpoint form have want net=0
    flat=$(settings)
    [[ $(live "$flat" net.addr) == "$LIDAR_IP" ]] || die "The sensor at $LIDAR_IP reports address $(live "$flat" net.addr)"
    changes=$(diffs "$flat")
    [[ -z $changes ]] && { info "The lidar already matches; nothing to apply"; return; }
    grep -q '^LIDAR_IP|' <<<"$changes" && die "LIDAR_IP differs from the sensor's address; re-address it in the web UI"
    file=$(snapshot)
    info "Changes for $LIDAR_IP:"
    while IFS='|' read -r env json endpoint form have want; do
        printf '  %-12s %s -> %s\n' "$json" "$have" "$want"
        [[ $endpoint != setting/net ]] || net=1
    done <<<"$changes"
    ((net)) && warn "Netmask/gateway changes are applied last and need a save and reset (the sensor restarts)"
    read -r -p "POST these to the lidar and save? Type APPLY: " answer
    [[ $answer == APPLY ]] || { info "Cancelled; nothing changed"; return; }
    while IFS='|' read -r env json endpoint form have want; do
        [[ $endpoint == setting/net ]] || post "$endpoint" "$form=$want" "$file"
    done <<<"$changes"
    post save submit "$file"
    if ((net)); then
        warn "Recovery if $LIDAR_IP stops answering: the address itself is unchanged, so check 'ip neigh' for"
        warn "MAC ${LIDAR_MAC:-?} and 'tcpdump -n udp port ${LIDAR_DATA_PORT:-2368}' on the data host for its source address."
        warn "Put a host on that subnet, fix Network in the web UI, Save Configuration, Reset System."
        warn "There is no reset switch; a factory-configured VLP16 is 192.168.1.201/24 (gateway 192.168.1.1)."
        while IFS='|' read -r env json endpoint form have want; do
            [[ $endpoint != setting/net ]] || post "$endpoint" "$form=$want" "$file"
        done <<<"$changes"
        post save submit "$file"
        post reset reset_system "$file"
        info "Waiting for the sensor to come back (up to 60 s)"
        local tries=0
        until lidar_http settings.json >/dev/null 2>&1; do
            ((++tries < 12)) || die "No answer from $LIDAR_IP after the reset; see the recovery note above. Snapshot: $file"
            sleep 5
        done
    fi
    verify || die "Settings still differ after apply; snapshot: $file"
    info "Applied and saved. Previous settings: $file"
}

case ${1:-} in
    verify) verify ;;
    snapshot) snapshot >/dev/null ;;
    apply) apply ;;
    -h|--help|'') usage ;;
    *) die "Unknown command: $1" ;;
esac
