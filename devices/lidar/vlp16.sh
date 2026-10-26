# shellcheck shell=bash
# Velodyne VLP16 over its HTTP API (/cgi/*.json, /cgi/setting*), from the profile's lidar.yaml.
# The sensor address is checked, never posted; network changes go last, then one save and a reset.

driver_usage() {
    cat <<EOF
Usage: $HW_PROGRAM $HW_KIND status|plan|verify|snapshot|apply|rollback [SNAPSHOT]

status    Read-only: model, firmware, network, data host, rpm, returns, FOV, laser/motor/PPS.
plan      Read-only: the profile keys that differ from the sensor, as apply would post them.
verify    Read-only: status, then exit 3 if any profile key differs.
snapshot  Save settings, status and info as one JSON file (state directory lidar).
apply     Snapshot, show the differing keys, and after you type APPLY post only those, then
          save once and verify. Netmask/gateway go last and add a reset; they must be active after.
rollback  apply the settings of a snapshot (default: the latest) instead of the profile.
Runs from a LAN host, or through the host in the .env key named by jump (e.g. tranquility).
EOF
}

# profile key|settings.json key|endpoint|form key. address is checked, never posted.
MAP='address|net.addr||
data_host|host.addr|setting/host|addr
data_port|host.dport|setting/host|dport
telemetry_port|host.tport|setting/host|tport
rpm|rpm|setting|rpm
returns|returns|setting|returns
fov.start|fov.start|setting/fov|start
fov.end|fov.end|setting/fov|end
netmask|net.mask|setting/net|mask
gateway|net.gateway|setting/net|gateway
netmask|active_net.mask|reset|
gateway|active_net.gateway|reset|'
POLL_SECONDS=1

lidar_ip() { local ip; ip=$(cfg address); [[ -n $ip ]] || die "address is unset in the lidar profile"; printf '%s' "$ip"; }

# on_lan CMD...: run on this host, or on the jump host over ssh.
on_lan() {
    local jump
    jump=$(secret jump)
    if [[ -n $jump ]]; then
        ssh -o BatchMode=yes -o ConnectTimeout=8 "$jump" "${*@Q}" </dev/null
    else
        "$@"
    fi
}

# lidar_http PATH [FORM]: GET, or POST FORM, against the sensor.
lidar_http() {
    local cmd=(curl -fsS -m 5 "http://$(lidar_ip)/cgi/$1")
    [[ -z ${2:-} ]] || cmd+=(--data "$2")
    on_lan "${cmd[@]}"
}

# Every value is typed and range-checked before it can reach a form, and the
# network must keep the gateway, the data host and the LAN host on one subnet.
validate() {
    python3 -I - "${1:-}" "$(lidar_ip)" "$(cfg netmask)" "$(cfg gateway)" "$(cfg data_host)" "$(cfg data_port)" \
        "$(cfg telemetry_port)" "$(cfg rpm)" "$(cfg returns)" "$(cfg fov.start)" "$(cfg fov.end)" <<'PY'
import ipaddress, re, sys
src, ip, mask, gateway, host, dport, tport, rpm, returns, start, end = sys.argv[1:]
def fail(message):
    sys.exit("lidar config: " + message)
def integer(name, value, low, high):
    if value and not (re.fullmatch(r"[0-9]+", value) and low <= int(value) <= high):
        fail("%s must be an integer in %d..%d, not %r" % (name, low, high, value))
for name, value in (("address", ip), ("gateway", gateway), ("data_host", host), ("LAN host", src)):
    if value and not re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,3}){3}", value):
        fail("%s must be an IPv4 address, not %r" % (name, value))
    try:
        value and ipaddress.IPv4Address(value)
    except ValueError:
        fail("%s must be an IPv4 address, not %r" % (name, value))
integer("data_port", dport, 1, 65535)
integer("telemetry_port", tport, 1, 65535)
integer("rpm", rpm, 300, 1200)
if rpm and int(rpm) % 60:
    fail("rpm must be a multiple of 60")
integer("fov.start", start, 0, 359)
integer("fov.end", end, 0, 359)
if returns and returns not in ("Strongest", "Last", "Dual"):
    fail("returns must be Strongest, Last or Dual, not %r" % returns)
if mask:
    try:
        net = ipaddress.IPv4Network("%s/%s" % (ip, mask), strict=False)
    except ValueError:
        fail("netmask %r is not a netmask" % mask)
    if not re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,3}){3}", mask):
        fail("netmask must be dotted, not %r" % mask)
    for name, value in (("gateway", gateway), ("data_host", host), ("the LAN host", src)):
        if value and ipaddress.IPv4Address(value) not in net:
            fail("%s %s is outside %s" % (name, value, net))
PY
}

# The LAN host's own address toward the sensor (the jump host when set).
lan_source() {
    on_lan ip -4 route get "$(lidar_ip)" 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1
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

settings() { lidar_http settings.json | flatten || die "No settings from the lidar at $(lidar_ip)"; }
live() { sed -n "s/^$(sed 's/\./\\./g' <<<"$2")=//p" <<<"$1" | head -1; }

# Prints "key|json|endpoint|form|live|want" for each profile key that differs.
diffs() {
    local flat=$1 key json endpoint form want have
    while IFS='|' read -r key json endpoint form; do
        want=$(cfg "$key")
        [[ -n $want ]] || continue
        have=$(live "$flat" "$json")
        [[ $have == "$want" ]] || printf '%s|%s|%s|%s|%s|%s\n' "$key" "$json" "$endpoint" "$form" "$have" "$want"
    done <<<"$MAP"
}

summary() {
    local flat=$1 status info key
    status=$(lidar_http status.json | flatten) || status=''
    info=$(lidar_http info.json | flatten) || info=''
    info "$(live "$info" model) serial $(live "$info" serial), firmware $(live "$info" build.bot), MAC $(live "$flat" net.mac_addr)"
    info "net $(live "$flat" net.addr)/$(live "$flat" net.mask) gw $(live "$flat" net.gateway) dhcp $(live "$flat" net.dhcp); data to $(live "$flat" host.addr):$(live "$flat" host.dport), telemetry :$(live "$flat" host.tport)"
    info "rpm $(live "$flat" rpm), returns $(live "$flat" returns), FOV $(live "$flat" fov.start)-$(live "$flat" fov.end), phase lock $(live "$flat" phaselock.enabled) offset $(live "$flat" phaselock.offset)"
    info "laser $(live "$status" laser.state), motor $(live "$status" motor.state) at $(live "$status" motor.rpm) rpm (lock $(live "$status" motor.lock)), PPS $(live "$status" gps.pps_state)"
    [[ $(live "$flat" net.dhcp) == Off ]] || warn "DHCP is on; the sensor may leave $(lidar_ip) after a reset"
    for key in addr mask gateway; do
        [[ $(live "$flat" "net.$key") == "$(live "$flat" "active_net.$key")" ]] || warn "net.$key is saved but not active until a reset"
    done
}

print_diffs() {
    local key json endpoint form have want
    while IFS='|' read -r key json endpoint form have want; do
        printf '  %-15s %-18s %s -> %s\n' "$key" "$json" "$have" "$want"
    done <<<"$1"
}

driver_status() { summary "$(settings)"; }

driver_plan() {
    local changes
    validate
    changes=$(diffs "$(settings)")
    if [[ -z $changes ]]; then info "Matches the profile; apply would post nothing"; return; fi
    info "apply would post (network keys last, then save and reset):"
    print_diffs "$changes"
}

driver_verify() {
    local flat changes
    validate
    flat=$(settings)
    summary "$flat"
    changes=$(diffs "$flat")
    if [[ -z $changes ]]; then info "Matches the profile"; return; fi
    warn "Differs from the profile:"
    print_diffs "$changes" >&2
    return 3
}

snapshot() {
    local file part
    file="$(hw_state lidar)/lidar-$(date +%Y%m%d-%H%M%S).json"
    (umask 077
        { printf '{'
          for part in settings status info; do
              [[ $part == settings ]] || printf ','
              printf '"%s":' "$part"
              lidar_http "$part.json" || exit 1
          done
          printf '}\n'; } >"$file") || { rm -f -- "$file"; die "Snapshot failed; is $(lidar_ip) reachable?"; }
    python3 -I -c 'import json, sys; json.load(open(sys.argv[1]))' "$file" || die "$file is not valid JSON"
    info "Saved $file" >&2
    printf '%s\n' "$file"
}

driver_snapshot() { snapshot >/dev/null; }

post() {
    lidar_http "$1" "$2" >/dev/null || die "POST /cgi/$1 $2 failed; snapshot: $3"
}

driver_apply() {
    local flat changes file answer key json endpoint form have want net=0 src='' tries ip
    ip=$(lidar_ip)
    validate
    flat=$(settings)
    [[ $(live "$flat" net.addr) == "$ip" ]] || die "The sensor at $ip reports address $(live "$flat" net.addr)"
    changes=$(diffs "$flat")
    [[ -z $changes ]] && { info "The lidar already matches; nothing to apply"; return; }
    grep -q '^address|' <<<"$changes" && die "address differs from the sensor's address; re-address it in the web UI"
    grep -Eq '\|(setting/net|reset)\|' <<<"$changes" && net=1
    if ((net)); then
        [[ $(live "$flat" net.dhcp) == Off && $(live "$flat" active_net.dhcp) == Off ]] \
            || die "DHCP is on (saved or active); a reset could move the sensor off $ip. Turn it off in the web UI first"
        src=$(lan_source)
        [[ -n $src ]] || die "Cannot find this LAN host's address toward $ip"
        validate "$src"
    fi
    file=$(snapshot)
    info "Changes for $ip:"
    while IFS='|' read -r key json endpoint form have want; do
        printf '  %-18s %s -> %s\n' "$json" "$have" "$want"
    done <<<"$changes"
    ((net)) && warn "Netmask/gateway changes are posted last, then one save and a reset (the sensor restarts)"
    read -r -p "POST these to the lidar and save? Type APPLY: " answer
    [[ $answer == APPLY ]] || { info "Cancelled; nothing changed"; return; }
    while IFS='|' read -r key json endpoint form have want; do
        [[ $endpoint == setting/net || $endpoint == reset ]] || post "$endpoint" "$form=$want" "$file"
    done <<<"$changes"
    while IFS='|' read -r key json endpoint form have want; do
        [[ $endpoint != setting/net ]] || post "$endpoint" "$form=$want" "$file"
    done <<<"$changes"
    post save submit "$file"
    if ((net)); then
        warn "Recovery if $ip stops answering: the address itself is unchanged, so check 'ip neigh' for"
        warn "MAC $(cfg mac) and 'tcpdump -n udp port $(cfg data_port)' on the data host for its source address."
        warn "Put a host on that subnet, fix Network in the web UI, Save Configuration, Reset System."
        warn "There is no reset switch; a factory-configured VLP16 is 192.168.1.201/24 (gateway 192.168.1.1)."
        # The sensor may drop the connection as it restarts (curl 52/56); the wait decides.
        lidar_http reset reset_system >/dev/null 2>&1 || true
        tries=0
        while lidar_http settings.json >/dev/null 2>&1 && ((++tries < 5)); do sleep "$POLL_SECONDS"; done
        info "Waiting for the sensor to come back (up to 60 s)"
        tries=0
        until flat=$(lidar_http settings.json 2>/dev/null | flatten 2>/dev/null) && [[ -n $flat ]]; do
            ((++tries < 60)) || die "No answer from $ip after the reset; see the recovery note above. Snapshot: $file"
            sleep "$POLL_SECONDS"
        done
        [[ $(live "$flat" active_net.mask) == "$(cfg netmask)" && $(live "$flat" active_net.gateway) == "$(cfg gateway)" ]] \
            || die "The sensor came back without the new netmask/gateway active; snapshot: $file"
    fi
    driver_verify || die "Settings still differ after apply; snapshot: $file"
    info "Applied and saved. Previous settings: $file"
}

# rollback [SNAPSHOT]: the snapshot's settings become the wanted values, then the apply path.
driver_rollback() {
    local file=${1:-} out
    if [[ -z $file ]]; then
        file=$(find "$(hw_state lidar)" -maxdepth 1 -name 'lidar-*.json' | sort | tail -1)
        [[ -n $file ]] || die "No lidar snapshot; name one"
    fi
    out=$(python3 -I - "$file" "$MAP" <<'PY'
import json, shlex, sys
settings = json.load(open(sys.argv[1]))["settings"]
for line in sys.argv[2].splitlines():
    key, path, endpoint, _ = line.split("|")
    value = settings
    for part in path.split("."):
        value = value.get(part, {}) if isinstance(value, dict) else {}
    if endpoint != "reset" and not isinstance(value, dict):
        print("HW_%s=%s" % (key.upper().replace(".", "_"), shlex.quote(str(value))))
PY
    ) || die "$file is not a lidar snapshot"
    eval "$out"
    info "Rolling back to $file"
    driver_apply
}
