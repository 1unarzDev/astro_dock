#!/usr/bin/env bash
# Verify, read, snapshot or apply the Ubiquiti airMAX link between the gateway laptop and the boat.
set -Eeuo pipefail

PROGRAM=${0##*/}
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
STATE_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/astro_dock/ubiquiti
REVERT_SECONDS=300
# --via ships the env ahead of this script, so it is already defined there.
# shellcheck source=config/boat-network.env
[[ -n ${BOAT_SUBNET:-} ]] || source "$SCRIPT_DIR/config/boat-network.env"
# Credentials come from the ignored .env (see .env.example); set variables win.
if [[ -r $SCRIPT_DIR/.env ]]; then
    while IFS='=' read -r key value; do
        [[ $key =~ ^[A-Z][A-Z0-9_]*$ && -z ${!key:-} ]] || continue
        value=${value%\"} value=${value#\"}
        printf -v "$key" '%s' "$value"
    done <"$SCRIPT_DIR/.env"
fi

info() { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Usage: $PROGRAM verify [--via HOST]
       $PROGRAM status|snapshot bullet|rocket
       $PROGRAM apply bullet|rocket [--dry-run] [KEY=VALUE ...]

verify    Read-only: ping each radio, show ARP entries, and send one UBNT
          discovery probe (UDP 10001) from this host's boat-LAN address.
          --via runs it on HOST over SSH (e.g. the gateway laptop).
status    Read-only: print the radio's mca-status, wstalist, status.cgi, its
          radio/wireless config keys (secrets dropped) and uptime.
snapshot  Copy the radio's /tmp/system.cfg to $STATE_DIR.
apply     Snapshot, then set only the given KEY=VALUE pairs (default: the
          UBNT_<RADIO>_CFG keys in config/boat-network.env) as a RAM-only
          trial. The radio reboots into its saved config after
          ${REVERT_SECONDS}s unless you type KEEP. --dry-run stops after the diff.
Run status/snapshot/apply from a host on the boat LAN, or set UBNT_JUMP to one.
UBNT_USER and UBNT_PASSWORD come from .env; without a password SSH prompts.
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
    src=$(ip -4 route get "$ROUTER_IP" 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p') || true
    [[ -n $src ]] || die "No route to $ROUTER_IP; run verify on a boat-LAN host or with --via"
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
    local ip=$1 askpass; shift
    # airOS 8 dropbear only offers ssh-rsa and drops clients whose host-key list is long.
    local cmd=(ssh -o ConnectTimeout=8 -o UserKnownHostsFile="$STATE_DIR/known_hosts" -o StrictHostKeyChecking=accept-new
        -o HostKeyAlgorithms=ssh-rsa -o LogLevel=ERROR)
    # A ProxyCommand in batch mode keeps the radio password from ever answering the jump host.
    [[ -z ${UBNT_JUMP:-} ]] || cmd+=(-o "ProxyCommand=ssh -o BatchMode=yes -W %h:%p $UBNT_JUMP")
    cmd+=("${UBNT_USER:-ubnt}@$ip" "$@")
    [[ -n ${UBNT_PASSWORD:-} ]] || { "${cmd[@]}"; return; }
    # The password travels in the environment, never on a command line.
    if command -v sshpass >/dev/null; then
        SSHPASS=$UBNT_PASSWORD sshpass -e "${cmd[@]}"
        return
    fi
    askpass=$(mktemp) && printf '#!/bin/sh\nprintf "%%s\\n" "$UBNT_PASSWORD"\n' >"$askpass" && chmod 700 "$askpass"
    local rc=0
    UBNT_PASSWORD=$UBNT_PASSWORD SSH_ASKPASS=$askpass SSH_ASKPASS_REQUIRE=force "${cmd[@]}" || rc=$?
    rm -f -- "$askpass"
    return "$rc"
}

# Read-only on airOS: status tools and config reads only. A site survey is left out on purpose:
# it takes the radio off its channel and drops the link while it scans.
status() {
    local ip
    ip=$(radio_var "$1" IP)
    [[ -n $ip ]] || die "UBNT_${1^^}_IP is unset"
    mkdir -p -m 700 "$STATE_DIR"
    ssh_radio "$ip" 'for c in mca-status wstalist "REQUEST_METHOD=GET /usr/www/status.cgi"; do
            echo "### $c"; eval "$c" 2>/dev/null; echo; done
        echo "### cfg"
        grep -E "^(radio|wireless\.1\.(ssid|wds|scan_list|mcast)|tshaper|gps)\." /tmp/system.cfg | grep -viE "psk|pass|secret|community|key"
        echo "### uptime"; cat /proc/uptime' </dev/null
}

snapshot() {
    local name=$1 ip file
    ip=$(radio_var "$name" IP)
    [[ -n $ip ]] || die "UBNT_${name^^}_IP is unset"
    mkdir -p -m 700 "$STATE_DIR"
    file="$STATE_DIR/$name-$(date +%Y%m%d-%H%M%S).cfg"
    (umask 077; ssh_radio "$ip" 'cat /tmp/system.cfg' </dev/null >"$file") || { rm -f -- "$file"; die "Snapshot failed"; }
    grep -q '^wireless\.' "$file" || die "$file does not look like an airOS system.cfg"
    info "Saved $file (contains secrets; keep it out of the repo)" >&2
    printf '%s\n' "$file"
}

# Keys that move the management address or must change on both radios at once.
REFUSED_KEYS='^(netconf|bridge|route|vlan|dhcpc|aaa|wpasupplicant)\.|\.(ip|netmask)$|^wireless\.1\.(ssid|security|wds|scan_list)|^radio\.1\.(mode|freq|chanbw|chanshift|ieee_mode|dfs\.)|^radio[0-9.]*\.countrycode'
# Channel and width are the AP's alone: the station re-scans and follows, and the trial timer
# restores the AP if it does not.
AP_KEYS='^radio\.1\.(freq|center\.1\.freq|chanbw|ieee_mode)$'
TRIAL_PID=/tmp/astro-trial.pid
TRIAL_ALIVE='p=$(cat '$TRIAL_PID' 2>/dev/null) && kill -0 "$p" 2>/dev/null'

apply() {
    local name=$1 peer ip pairs pair key value file new answer changed=0 dry=0 freq='' channel=0 scan
    shift
    [[ ${1:-} == --dry-run ]] && { dry=1; shift; }
    [[ $name == rocket ]] && peer=bullet || peer=rocket
    ip=$(radio_var "$name" IP) pairs=${*:-$(radio_var "$name" CFG)}
    [[ -n $pairs ]] || die "No KEY=VALUE pairs and UBNT_${name^^}_CFG lists no keys; nothing to apply"
    umask 077
    ssh_radio "$(radio_var "$peer" IP)" "! { $TRIAL_ALIVE; }" </dev/null \
        || die "The $peer radio is unreachable or has a pending trial; apply one radio at a time"
    ssh_radio "$ip" "! { $TRIAL_ALIVE; }" </dev/null || die "$name is unreachable or already has a pending trial"
    file=$(snapshot "$name")
    new=$(mktemp) && trap "rm -f -- '$new' '$new.tmp'" EXIT
    cp -- "$file" "$new"
    set -f
    for pair in $pairs; do
        key=${pair%%=*} value=${pair#*=}
        [[ $key =~ ^[A-Za-z0-9_.]+$ && $pair == *=* ]] || die "Bad UBNT_${name^^}_CFG entry: $pair"
        if [[ $key =~ $AP_KEYS && $(radio_var "$name" ROLE) == ap ]]; then
            [[ $value =~ ^[0-9a-z]+$ ]] || die "Bad value for $key: $value"
        elif [[ $key =~ $REFUSED_KEYS ]]; then
            die "$key is a management-address or link-shared key; change it in the airOS UI on both radios"
        fi
        awk -F= -v k="$key" '$1 == k { f = 1 } END { exit !f }' "$new" || die "$key is not in the radio's config; only existing keys are changed"
        awk -F= -v k="$key" -v v="$value" '$1 == k { sub(/^[^=]*=/, ""); f = ($0 == v) } END { exit !f }' "$new" && continue
        [[ $key =~ $AP_KEYS ]] && channel=1
        [[ $key == radio.1.freq ]] && freq=$value
        if [[ $key =~ pass|psk|community|secret ]]; then
            printf '  %s: (hidden) -> (hidden)\n' "$key"
        else
            printf '  %s: %s -> %s\n' "$key" "$(awk -F= -v k="$key" '$1 == k { sub(/^[^=]*=/, ""); print; exit }' "$new")" "$value"
        fi
        awk -F= -v k="$key" -v v="$value" '$1 == k { print k "=" v; next } { print }' "$new" >"$new.tmp" && mv -- "$new.tmp" "$new"
        changed=1
    done
    set +f
    ((changed)) || { info "$name already matches; nothing to apply"; return; }
    if ((channel)); then
        # A station with a restricted scan list never finds the AP on a channel outside it.
        scan=$(ssh_radio "$(radio_var "$peer" IP)" "grep '^wireless\.1\.scan_list\.' /tmp/system.cfg" </dev/null) || true
        if grep -qx 'wireless.1.scan_list.status=enabled' <<<"$scan" && [[ -n $freq ]] \
            && ! sed -n 's/^wireless\.1\.scan_list\.channels=//p' <<<"$scan" | tr ', ' '\n\n' | grep -qx "$freq"; then
            die "The $peer's scan list does not include $freq MHz; add it there in the airOS UI first"
        fi
        warn "Channel/width change: the link drops while the $peer re-scans (about 30 s); if it does not come back, $name reverts after ${REVERT_SECONDS}s."
    fi
    ((dry)) && { info "Dry run; nothing applied. Snapshot: $file"; return; }
    read -r -p "Trial $name at $ip in RAM, rebooting to its saved config after ${REVERT_SECONDS}s? Type APPLY: " answer
    [[ $answer == APPLY ]] || { info "Cancelled; nothing changed"; return; }
    # Flash is untouched until KEEP, so the timer, a reboot or a power cycle all restore the saved config.
    ssh_radio "$ip" "cat >/tmp/astro-new.cfg || exit 1
        sh -c 'trap \"\" HUP; sleep $REVERT_SECONDS; reboot' </dev/null >/dev/null 2>&1 &
        echo \$! >$TRIAL_PID
        cp /tmp/astro-new.cfg /tmp/system.cfg && /usr/etc/rc.d/rc.softrestart test" <"$new" >/dev/null \
        || warn "The session dropped during apply; checking the radio"
    if ssh_radio "$ip" "$TRIAL_ALIVE && ps w | grep -F 'sleep $REVERT_SECONDS' | grep -v grep" </dev/null; then
        info "Trial running and revert timer alive. Check the link now (another terminal: $PROGRAM verify)."
    else
        warn "!!! Could not confirm the revert timer on $name. Its flash still holds the saved config:"
        warn "!!! reboot or power-cycle the radio to restore it if the link misbehaves."
    fi
    read -r -p "Type KEEP to save the trial to flash; anything else lets it revert: " answer
    [[ $answer == KEEP ]] || { info "Not kept; $name reboots into its saved config when the timer ends"; return; }
    # Save only a live, unchanged trial, and only after the timer is confirmed dead.
    local rc=0
    ssh_radio "$ip" "p=\$(cat $TRIAL_PID 2>/dev/null) && kill -0 \"\$p\" 2>/dev/null &&
            diff /tmp/system.cfg /tmp/astro-new.cfg >/dev/null || exit 13
        kill \"\$p\"; sleep 1; kill -0 \"\$p\" 2>/dev/null && exit 14
        sort /tmp/system.cfg | tr -d '\r' >/tmp/running.cfg && cfgmtd -w -f /tmp/running.cfg -p /etc/ || exit 15
        rm -f $TRIAL_PID /tmp/astro-new.cfg" </dev/null || rc=$?
    case $rc in
        0) ;;
        13) die "Trial already reverted (timer gone or config changed); nothing was saved" ;;
        14) die "Could not stop the timer; nothing was saved and $name reboots into its saved config" ;;
        15) die "The flash write failed after the timer was stopped; $name runs the trial until its next reboot, which restores the saved config" ;;
        *) die "SSH to $name failed (exit $rc); nothing confirmed saved, so expect it to reboot into its saved config" ;;
    esac
    info "Saved to flash and timer cancelled. Previous config: $file"
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
    status) [[ ${2:-} =~ ^(bullet|rocket)$ ]] || die "Name a radio: bullet or rocket"; status "$2" ;;
    snapshot) [[ ${2:-} =~ ^(bullet|rocket)$ ]] || die "Name a radio: bullet or rocket"; snapshot "$2" >/dev/null ;;
    apply) [[ ${2:-} =~ ^(bullet|rocket)$ ]] || die "Name a radio: bullet or rocket"; apply "$2" "${@:3}" ;;
    -h|--help|'') usage ;;
    *) die "Unknown command: $1" ;;
esac
