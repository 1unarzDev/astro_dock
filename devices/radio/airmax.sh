# shellcheck shell=bash
# Ubiquiti airMAX (airOS 8) point-to-point link: two radios from the profile's radio.yaml.
# Writes are RAM-only trials that reboot into the saved config after revert_seconds unless KEEP is typed.

TRIAL_PID=/tmp/astro-trial.pid
TRIAL_ALIVE='p=$(cat '$TRIAL_PID' 2>/dev/null) && kill -0 "$p" 2>/dev/null'
# Keys that move the management address or must change on both radios at once.
REFUSED_KEYS='^(netconf|bridge|route|vlan|dhcpc|aaa|wpasupplicant)\.|\.(ip|netmask)$|^wireless\.1\.(ssid|security|wds|scan_list)|^radio\.1\.(mode|freq|chanbw|chanshift|ieee_mode|dfs\.)|^radio[0-9.]*\.countrycode'
# Channel and width are the AP's alone: the station re-scans and follows, and the trial timer
# restores the AP if it does not.
AP_KEYS='^radio\.1\.(freq|center\.1\.freq|chanbw|ieee_mode)$'

driver_usage() {
    local names
    names=$(cfg_keys devices | tr ' ' '|')
    cat <<EOF
Usage: $HW_PROGRAM $HW_KIND verify [--via HOST]
       $HW_PROGRAM $HW_KIND status|snapshot|rollback $names
       $HW_PROGRAM $HW_KIND plan $names [KEY=VALUE ...]
       $HW_PROGRAM $HW_KIND apply $names [--dry-run] [KEY=VALUE ...]
       $HW_PROGRAM $HW_KIND measure | propose [FILE]

status    Read-only: mca-status, wstalist, status.cgi, radio/wireless config keys (secrets
          dropped) and uptime. No site survey: it leaves the channel and drops the link.
verify    Read-only: ping and ARP each radio, ping the profile's reachable hosts, one UBNT
          discovery probe (UDP 10001). --via runs it on HOST over ssh.
snapshot  Copy the radio's /tmp/system.cfg to the state directory (contains secrets).
plan      Read the radio's config and print the diff of KEY=VALUE pairs (default: the radio's
          cfg in the profile); nothing is applied or saved. Same as apply --dry-run.
apply     plan, then a RAM-only trial: the radio reboots into its saved config after
          $(cfg revert_seconds)s unless you type KEEP. A channel/width trial is kept only if the peer
          answers through it.
rollback  End a pending trial now: reboot into the saved config.
measure   Read-only: both radios' status, iperf3 TCP and UDP both ways to the profile's
          measure.target with ping alongside; saved under the link state directory.
propose   Offline: setting changes per radio from the latest (or given) measurement, as
          plan/apply commands. Guards: the profile's propose.* (HW_PROPOSE_* override).
Logins: the .env keys named in credentials (without a password ssh prompts).
status, plan and verify write nothing to the radios; locally, ssh may add a radio's host key
to the state directory's known_hosts.
EOF
}

state_dir() { hw_state ubiquiti; }

radio() {
    [[ " $(cfg_keys devices) " == *" ${1:-} "* && -n ${1:-} ]] || die "Name a radio: $(cfg_keys devices)"
    [[ -n $(cfg "devices.$1.address") ]] || die "devices.$1.address is unset in the profile"
}
addr() { cfg "devices.$1.address"; }
peer_of() { local n; for n in $(cfg_keys devices); do [[ $n == "$1" ]] || { printf '%s' "$n"; return; }; done; }

ssh_radio() {
    local ip=$1 jump
    shift
    jump=$(secret credentials.jump)
    # airOS 8 dropbear only offers ssh-rsa and drops clients whose host-key list is long.
    local cmd=(ssh -o ConnectTimeout=8 -o UserKnownHostsFile="$(state_dir)/known_hosts" -o StrictHostKeyChecking=accept-new
        -o HostKeyAlgorithms=ssh-rsa -o LogLevel=ERROR)
    # A ProxyCommand in batch mode keeps the radio password from ever answering the jump host.
    [[ -z $jump ]] || cmd+=(-o "ProxyCommand=ssh -o BatchMode=yes -W %h:%p $jump")
    local user
    user=$(secret credentials.user)
    cmd+=("${user:-ubnt}@$ip" "$@")
    hw_with_password "$(secret credentials.password)" "${cmd[@]}"
}

driver_verify() {
    local name ip mac src
    if [[ ${1:-} == --via ]]; then
        [[ -n ${2:-} ]] || die "--via requires a host"
        { echo 'set -Eeuo pipefail'; declare -p HW_PROGRAM HW_KIND $(compgen -v HW_ | grep -v '^HW_PROGRAM$\|^HW_KIND$')
          cat "$HW_ROOT/devices/lib.sh" "$HW_DRIVER_FILE"; echo driver_verify; } | ssh "$2" bash -s
        return
    fi
    for name in $(cfg_keys devices); do
        ip=$(addr "$name") mac=$(cfg "devices.$name.mac")
        [[ -n $ip ]] || { warn "$name: no address in the profile"; continue; }
        if ping -c 1 -W 1 "$ip" >/dev/null 2>&1; then info "$name $ip answers ping"; else warn "$name $ip does not answer ping"; fi
        ip neigh show "$ip" | grep -qi "${mac:-unset}" && info "$name ARP matches $mac" \
            || warn "$name ARP: $(ip neigh show "$ip" | head -1)"
    done
    for ip in $(cfg reachable); do
        ping -c 1 -W 1 "$ip" >/dev/null 2>&1 && info "$ip reachable across the link" || warn "$ip unreachable"
    done
    ip=$(cfg reachable) ip=${ip%% *}
    src=$(ip -4 route get "$ip" 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p') || true
    [[ -n $src ]] || die "No route to $ip; run verify on a LAN host or with --via"
    command -v python3 >/dev/null || die "python3 is required for the discovery probe"
    info "Expected: SSID $(cfg ssid);$(for name in $(cfg_keys devices); do printf ' %s %s' "$name" "$(cfg "devices.$name.role")"; done)"
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

# Read-only on airOS: status tools and config reads only. A site survey is left out on purpose:
# it takes the radio off its channel and drops the link while it scans.
driver_status() {
    radio "${1:-}"
    ssh_radio "$(addr "$1")" 'for c in mca-status wstalist "REQUEST_METHOD=GET /usr/www/status.cgi"; do
            echo "### $c"; eval "$c" 2>/dev/null; echo; done
        echo "### cfg"
        grep -E "^(radio|wireless\.1\.(ssid|wds|scan_list|mcast)|tshaper|gps)\." /tmp/system.cfg | grep -viE "psk|pass|secret|community|key"
        echo "### uptime"; cat /proc/uptime' </dev/null
}

snapshot() {
    local name=$1 file=${2:-}
    [[ -n $file ]] || file="$(state_dir)/$name-$(date +%Y%m%d-%H%M%S).cfg"
    (umask 077; ssh_radio "$(addr "$name")" 'cat /tmp/system.cfg' </dev/null >"$file") || { rm -f -- "$file"; die "Snapshot failed"; }
    grep -q '^wireless\.' "$file" || die "$file does not look like an airOS system.cfg"
    [[ -n ${2:-} ]] || info "Saved $file (contains secrets; keep it out of the repo)" >&2
    printf '%s\n' "$file"
}

driver_snapshot() { radio "${1:-}"; snapshot "$1" >/dev/null; }

driver_plan() { radio "${1:-}"; driver_apply "$1" --dry-run "${@:2}"; }

driver_apply() {
    local name=${1:-} peer ip pairs pair key value file new answer changed=0 dry=0 freq='' channel=0 scan revert
    radio "$name"
    shift
    [[ ${1:-} == --dry-run ]] && { dry=1; shift; }
    peer=$(peer_of "$name") ip=$(addr "$name") pairs=${*:-$(cfg "devices.$name.cfg")} revert=$(cfg revert_seconds)
    [[ $revert =~ ^[0-9]+$ ]] && ((revert >= 60)) || die "revert_seconds must be at least 60, not '$revert'"
    [[ -n $peer ]] || die "The profile needs two radios; $name has no peer"
    [[ -n $pairs ]] || die "No KEY=VALUE pairs and devices.$name.cfg lists no keys; nothing to apply"
    umask 077
    ssh_radio "$(addr "$peer")" "! { $TRIAL_ALIVE; }" </dev/null \
        || die "The $peer radio is unreachable or has a pending trial; apply one radio at a time"
    ssh_radio "$ip" "! { $TRIAL_ALIVE; }" </dev/null || die "$name is unreachable or already has a pending trial"
    new=$(mktemp) && trap "rm -f -- '$new' '$new.tmp' '$new.live'" EXIT
    # A plan reads the config into a temporary file: no secret-bearing snapshot is kept.
    if ((dry)); then file=$new.live; snapshot "$name" "$file" >/dev/null; else file=$(snapshot "$name"); fi
    cp -- "$file" "$new"
    set -f
    for pair in $pairs; do
        key=${pair%%=*} value=${pair#*=}
        [[ $key =~ ^[A-Za-z0-9_.]+$ && $pair == *=* ]] || die "Bad KEY=VALUE pair for $name: $pair"
        if [[ $key =~ $AP_KEYS && $(cfg "devices.$name.role") == ap ]]; then
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
        scan=$(ssh_radio "$(addr "$peer")" "grep '^wireless\.1\.scan_list\.' /tmp/system.cfg" </dev/null) || true
        if grep -qx 'wireless.1.scan_list.status=enabled' <<<"$scan" && [[ -n $freq ]] \
            && ! sed -n 's/^wireless\.1\.scan_list\.channels=//p' <<<"$scan" | tr ', ' '\n\n' | grep -qx "$freq"; then
            die "The $peer's scan list does not include $freq MHz; add it there in the airOS UI first"
        fi
        warn "Channel/width change: the link drops while the $peer re-scans (about 30 s); if it does not come back, $name reverts after ${revert}s."
    fi
    ((dry)) && { info "Dry run; nothing applied or saved."; return; }
    read -r -p "Trial $name at $ip in RAM, rebooting to its saved config after ${revert}s? Type APPLY: " answer
    [[ $answer == APPLY ]] || { info "Cancelled; nothing changed"; return; }
    # Flash is untouched until KEEP, so the timer, a reboot or a power cycle all restore the saved config.
    ssh_radio "$ip" "cat >/tmp/astro-new.cfg || exit 1
        sh -c 'trap \"\" HUP; sleep $revert; reboot' </dev/null >/dev/null 2>&1 &
        echo \$! >$TRIAL_PID
        cp /tmp/astro-new.cfg /tmp/system.cfg && /usr/etc/rc.d/rc.softrestart test" <"$new" >/dev/null \
        || warn "The session dropped during apply; checking the radio"
    if ssh_radio "$ip" "$TRIAL_ALIVE && ps w | grep -F 'sleep $revert' | grep -v grep" </dev/null; then
        info "Trial running and revert timer alive. Check the link now (another terminal: $HW_PROGRAM $HW_KIND verify)."
    else
        warn "!!! Could not confirm the revert timer on $name. Its flash still holds the saved config:"
        warn "!!! reboot or power-cycle the radio to restore it if the link misbehaves."
    fi
    read -r -p "Type KEEP to save the trial to flash; anything else lets it revert: " answer
    [[ $answer == KEEP ]] || { info "Not kept; $name reboots into its saved config when the timer ends"; return; }
    # A channel/width trial is kept only if the link carries it: the peer must answer through it.
    if ((channel)) && ! ssh_radio "$(addr "$peer")" true </dev/null; then
        die "The $peer radio is unreachable on the trial channel/width; not saved, $name reverts after the timer"
    fi
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

# A pending trial is the only unsaved state; rebooting now does what its timer would.
driver_rollback() {
    local answer
    radio "${1:-}"
    ssh_radio "$(addr "$1")" "$TRIAL_ALIVE" </dev/null \
        || die "$1 has no pending trial; its flash holds the last kept config (apply a snapshot's keys to go further back)"
    read -r -p "Reboot $1 into its saved config now? Type ROLLBACK: " answer
    [[ $answer == ROLLBACK ]] || { info "Cancelled; the trial reverts when its timer ends"; return; }
    ssh_radio "$(addr "$1")" 'reboot' </dev/null >/dev/null 2>&1 || true
    info "$1 is rebooting into its saved config"
}

# iperf3 on a host: its own, else its container's (one line, so it survives ssh quoting). It
# installs nothing; timeout ends a server whose client never came.
iperf_cmd() {
    local seconds=$1 container=$2
    printf '%s' "t=\$(($seconds + 60)); if command -v iperf3 >/dev/null 2>&1; then exec timeout \$t iperf3 \"\$@\"; fi; \
docker exec $container sh -c 'command -v iperf3' >/dev/null 2>&1 \
|| { echo \"iperf3: not on \$(hostname) or in its $container container; install it (astro-ros-install iperf3) and rerun\" >&2; exit 127; }; \
exec docker exec $container timeout \$t iperf3 \"\$@\""
}

driver_measure() {
    local target seconds port container user raw stamp name src iperf label try server pinger out roles=()
    target=$(cfg measure.target) seconds=$(cfg measure.seconds) port=$(cfg measure.port) container=$(cfg measure.container)
    user=$(secret measure.credentials.user)
    [[ -n $target ]] || die "measure.target is unset in the profile"
    [[ -n $user ]] || die "$(cfg measure.credentials.user) (the login on $target) is unset; set it in .env"
    [[ $seconds =~ ^[0-9]+$ ]] && ((seconds >= 3 && seconds <= 120)) || die "measure.seconds must be 3..120"
    [[ $container =~ ^[A-Za-z0-9_.-]+$ ]] || die "measure.container must be a container name"
    command -v python3 >/dev/null || die "python3 is required"
    command -v ping >/dev/null || die "ping is required"
    src=$(ip -4 route get "$target" 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p') || true
    [[ -n $src ]] || die "No route to $target; run this on a LAN host"
    iperf=$(iperf_cmd "$seconds" "$container")
    far() {
        local cmd=(ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o LogLevel=ERROR)
        [[ -n $(secret measure.credentials.password) ]] || cmd+=(-o BatchMode=yes)
        hw_with_password "$(secret measure.credentials.password)" "${cmd[@]}" "$user@$target" "sh -c ${iperf@Q} iperf3 $*" </dev/null
    }
    near() { sh -c "$iperf" iperf3 "$@"; }
    umask 077
    stamp=$(date +%Y%m%d-%H%M%S)
    raw=$(hw_state link)/link-$stamp.txt
    printf '# measure from %s to %s, %ss per test\n' "$src" "$target" "$seconds" >"$raw"
    for name in $(cfg_keys devices); do
        info "Reading the $name's airOS status"
        printf '\n==== radio %s\n' "$name" >>"$raw"
        (driver_status "$name") >>"$raw" 2>&1 || warn "No status from the $name"
        roles+=("$name=$(cfg "devices.$name.role")")
    done
    printf '\n==== site survey\nskipped: an airOS survey scans off-channel and drops the link\n' >>"$raw"
    info "Idle ping to $target"
    printf '\n==== ping idle\n' >>"$raw"
    ping -q -i 0.2 -c 25 "$target" >>"$raw" 2>&1 || true
    info "Checking iperf3 here and on $user@$target"
    near --version >/dev/null || die "iperf3 is unavailable here"
    far --version >/dev/null || die "iperf3 is unavailable on $user@$target"
    # One-off server on the far end, client here, ping alongside; retried only while the server starts.
    run() {
        label=$1 out=$(mktemp) pinger=''
        shift
        far -s -1 -p "$port" >/dev/null 2>&1 & server=$!
        for try in 1 2 3 4 5 6 7 8 9 10; do
            ping -q -i 0.2 -c $((seconds * 5)) "$target" >"$out.ping" 2>&1 & pinger=$!
            near -c "$target" -p "$port" -t "$seconds" -J "$@" >"$out" 2>&1 && break
            kill "$pinger" 2>/dev/null || true
            wait "$pinger" 2>/dev/null || true
            pinger=''
            grep -qiE 'unable to (connect|send control)|refused' "$out" || break
            sleep 1
        done
        [[ -z $pinger ]] || wait "$pinger" || true
        { printf '\n==== iperf3 %s\n' "$label"; cat "$out"; printf '\n==== ping %s\n' "$label"; cat "$out.ping" 2>/dev/null; } >>"$raw"
        wait "$server" 2>/dev/null || true
        rm -f -- "$out" "$out.ping"
    }
    rate() { python3 -I "$HW_ROOT/devices/radio/airmax_link.py" udp-rate "$raw" "$1"; }
    info "TCP to the far end, then from it (${seconds}s each)"
    run "tcp to-target" -O 2
    run "tcp from-target" -O 2 -R
    info "UDP at 70% of each TCP rate"
    run "udp to-target" -u -b "$(rate "tcp to-target")M"
    run "udp from-target" -u -b "$(rate "tcp from-target")M" -R
    python3 -I "$HW_ROOT/devices/radio/airmax_link.py" summary "$raw" "${raw%.txt}.json" "${roles[@]}"
    info "Raw output: $raw"
    info "Summary: ${raw%.txt}.json; next: $HW_PROGRAM $HW_KIND propose"
}

driver_propose() {
    local file=${1:-} dir
    dir=$(hw_state link)
    if [[ -z $file ]]; then
        file=$(find "$dir" -maxdepth 1 -name 'link-*.json' 2>/dev/null | sort | tail -1) || true
        [[ -n $file ]] || die "No measurement under $dir; run $HW_PROGRAM $HW_KIND measure first"
    fi
    [[ -r $file ]] || die "Cannot read $file"
    info "Proposal from $file (nothing is applied)"
    python3 -I "$HW_ROOT/devices/radio/airmax_link.py" propose "$file" "$(cfg propose.range_m)" "$(cfg propose.freq)" \
        "$(cfg propose.width_max)" "$(cfg propose.txpower_max)" "$(cfg revert_seconds)" "$HW_PROGRAM $HW_KIND"
}
