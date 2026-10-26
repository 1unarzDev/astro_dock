#!/usr/bin/env bash
# Measure the Ubiquiti link read-only (radio status, iperf3 both ways, ping under load) and
# propose radio settings from the result for ubiquiti_setup.sh's timed-revert trial.
set -Eeuo pipefail

PROGRAM=${0##*/}
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
STATE_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/astro_dock/link
# A caller that already exported the env (BOAT_SUBNET set) keeps its values.
# shellcheck source=config/boat-network.env
[[ -n ${BOAT_SUBNET:-} ]] || source "$SCRIPT_DIR/config/boat-network.env"
# BOAT_HOST_USER/PASSWORD come from the ignored .env (see .env.example); set variables win.
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

LINK_SECONDS=${LINK_SECONDS:-10}
LINK_PORT=${LINK_PORT:-5201}
LINK_CONTAINER=${LINK_CONTAINER:-roboboat_dev}
: "${BOAT_TRANSPORT_IP:?BOAT_TRANSPORT_IP is unset in config/boat-network.env}"

usage() {
    cat <<EOF
Usage: $PROGRAM measure
       $PROGRAM propose [FILE]

measure   Read-only. Reads both radios' airOS status (ubiquiti_setup.sh status),
          runs iperf3 TCP and UDP to and from the boat computer
          (BOAT_TRANSPORT_IP) with ping running alongside, prints a summary and
          saves the raw output and the summary under $STATE_DIR (mode 600).
          No site survey: on airOS it leaves the channel and drops the link.
          iperf3 runs from each host, else from its $LINK_CONTAINER container,
          and fails when neither has it (it installs nothing).
propose   Offline. From the latest (or the given) summary, print the setting
          changes per radio with reasons and the ubiquiti_setup.sh commands
          that dry-run and then trial them. Nothing is applied.
          Guards: LINK_RANGE_M (max link distance, default 1000), LINK_FREQ
          (MHz from a site survey; default keep), LINK_WIDTH_MAX (default 40),
          LINK_TXPOWER_MAX (dBm; default each radio's current power, so power
          only goes down).
Run it on the operator laptop (the boat-LAN end of the link). LINK_SECONDS sets
each test's length (default $LINK_SECONDS); BOAT_HOST_USER is the boat login.
EOF
}

# One line, so it survives ssh quoting: the host's iperf3, else the container's; measure installs
# nothing. timeout ends a server whose client never came.
IPERF="t=\$((${LINK_SECONDS} + 60)); if command -v iperf3 >/dev/null 2>&1; then exec timeout \$t iperf3 \"\$@\"; fi; \
docker exec $LINK_CONTAINER sh -c 'command -v iperf3' >/dev/null 2>&1 \
|| { echo \"iperf3: not on \$(hostname) or in its $LINK_CONTAINER container; install it (astro-ros-install iperf3) and rerun\" >&2; exit 127; }; \
exec docker exec $LINK_CONTAINER timeout \$t iperf3 \"\$@\""

local_iperf() { sh -c "$IPERF" iperf3 "$@"; }

boat() {
    local cmd=(ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o LogLevel=ERROR "$BOAT_SSH" "$@")
    if [[ -n ${BOAT_HOST_PASSWORD:-} ]] && command -v sshpass >/dev/null; then
        SSHPASS=$BOAT_HOST_PASSWORD sshpass -e "${cmd[@]}" </dev/null
    else
        "${cmd[0]}" -o BatchMode=yes "${cmd[@]:1}" </dev/null
    fi
}

boat_iperf() { boat "sh -c ${IPERF@Q} iperf3 $*"; }

# iperf_test LABEL ARGS...: one-off server on the boat, client here, ping alongside.
iperf_test() {
    local label=$1 try server pinger out
    shift
    out=$(mktemp) && pinger=''
    boat_iperf -s -1 -p "$LINK_PORT" >/dev/null 2>&1 & server=$!
    for try in 1 2 3 4 5 6 7 8 9 10; do
        ping -q -i 0.2 -c $((LINK_SECONDS * 5)) "$BOAT_TRANSPORT_IP" >"$out.ping" 2>&1 & pinger=$!
        local_iperf -c "$BOAT_TRANSPORT_IP" -p "$LINK_PORT" -t "$LINK_SECONDS" -J "$@" >"$out" 2>&1 && break
        kill "$pinger" 2>/dev/null || true
        wait "$pinger" 2>/dev/null || true
        pinger=''
        # Only a server that is not listening yet is worth another try.
        grep -qiE 'unable to (connect|send control)|refused' "$out" || break
        sleep 1
    done
    [[ -z $pinger ]] || wait "$pinger" || true
    { printf '\n==== iperf3 %s\n' "$label"; cat "$out"; printf '\n==== ping %s\n' "$label"; cat "$out.ping" 2>/dev/null; } >>"$RAW"
    wait "$server" 2>/dev/null || true
    rm -f -- "$out" "$out.ping"
}

# Mbit/s for a UDP test: 70% of the TCP rate seen in that direction, at least 1.
udp_rate() {
    python3 -I - "$RAW" "$1" <<'PY'
import json, re, sys
text, label = open(sys.argv[1]).read(), sys.argv[2]
body = text.split("\n==== iperf3 %s\n" % label, 1)[-1].split("\n==== ", 1)[0]
try:
    bps = json.loads(body)["end"]["sum_received"]["bits_per_second"]
except Exception:
    bps = 0
print(max(1, round(bps * 0.7 / 1e6)))
PY
}

measure() {
    local stamp radio src
    command -v python3 >/dev/null || die "python3 is required"
    command -v ping >/dev/null || die "ping is required"
    [[ $LINK_SECONDS =~ ^[0-9]+$ ]] && ((LINK_SECONDS >= 3 && LINK_SECONDS <= 120)) || die "LINK_SECONDS must be 3..120"
    src=$(ip -4 route get "$BOAT_TRANSPORT_IP" 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p') || true
    [[ -n $src ]] || die "No route to $BOAT_TRANSPORT_IP; run this on a boat-LAN host"
    umask 077
    mkdir -p -m 700 "$STATE_DIR"
    stamp=$(date +%Y%m%d-%H%M%S)
    RAW=$STATE_DIR/link-$stamp.txt
    printf '# %s measure from %s to %s, %ss per test\n' "$PROGRAM" "$src" "$BOAT_TRANSPORT_IP" "$LINK_SECONDS" >"$RAW"
    for radio in rocket bullet; do
        info "Reading the $radio's airOS status"
        printf '\n==== radio %s\n' "$radio" >>"$RAW"
        "$SCRIPT_DIR/ubiquiti_setup.sh" status "$radio" >>"$RAW" 2>&1 || warn "No status from the $radio"
    done
    printf '\n==== site survey\nskipped: an airOS survey scans off-channel and drops the link\n' >>"$RAW"
    info "Idle ping to $BOAT_TRANSPORT_IP"
    printf '\n==== ping idle\n' >>"$RAW"
    ping -q -i 0.2 -c 25 "$BOAT_TRANSPORT_IP" >>"$RAW" 2>&1 || true
    info "Checking iperf3 here and on $BOAT_SSH"
    local_iperf --version >/dev/null || die "iperf3 is unavailable here"
    boat_iperf --version >/dev/null || die "iperf3 is unavailable on $BOAT_SSH"
    info "TCP to the boat, then from it (${LINK_SECONDS}s each)"
    iperf_test "tcp to-boat" -O 2
    iperf_test "tcp from-boat" -O 2 -R
    info "UDP at 70% of each TCP rate"
    iperf_test "udp to-boat" -u -b "$(udp_rate "tcp to-boat")M"
    iperf_test "udp from-boat" -u -b "$(udp_rate "tcp from-boat")M" -R
    python3 -I - "$RAW" "${RAW%.txt}.json" "${UBNT_ROCKET_ROLE:-}" "${UBNT_BULLET_ROLE:-}" <<'PY'
import json, re, sys
raw, out, rocket_role, bullet_role = sys.argv[1:]
sections = {}
for part in open(raw).read().split("\n==== ")[1:]:
    name, _, body = part.partition("\n")
    sections[name] = body

def get(data, path):
    for key in path.split("."):
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data

def first(*values):
    return next((v for v in values if v not in (None, "")), None)

def jsonish(text):
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0] or [-1])
    try:
        return json.loads(text[start:]) if start >= 0 else None
    except ValueError:
        return None

def radio(body, role):
    parts = {}
    for chunk in ("\n" + body).split("\n### ")[1:]:
        name, _, text = chunk.partition("\n")
        parts[name.split()[-1].rsplit("/", 1)[-1]] = text
    mca = {}
    for line in parts.get("mca-status", "").splitlines():
        for item in line.split(","):
            if "=" in item:
                k, v = item.split("=", 1)
                mca[k.strip()] = v.strip()
    cfg = dict(l.split("=", 1) for l in parts.get("cfg", "").splitlines() if "=" in l)
    status = jsonish(parts.get("status.cgi", "")) or {}
    sta = jsonish(parts.get("wstalist", "")) or get(status, "wireless.sta") or []
    sta = sta[0] if isinstance(sta, list) and sta else {}
    def num(v):
        try:
            return float(str(v).split()[0])
        except (ValueError, IndexError):
            return None
    r = {
        "role": role,
        "signal": num(first(sta.get("signal"), get(status, "wireless.signal"), mca.get("signal"))),
        "noise": num(first(sta.get("noisefloor"), get(status, "wireless.noisef"), mca.get("noise"))),
        "cinr_rx": num(get(sta, "airmax.rx.cinr")), "cinr_tx": num(get(sta, "airmax.tx.cinr")),
        "tx_rate": first(sta.get("tx_rate"), mca.get("wlanTxRate")),
        "rx_rate": first(sta.get("rx_rate"), mca.get("wlanRxRate")),
        "tx_mcs": first(sta.get("tx_idx")), "tx_nss": first(sta.get("tx_nss")),
        "rx_mcs": first(sta.get("rx_idx")), "rx_nss": first(sta.get("rx_nss")),
        "capacity_dl": num(first(get(sta, "airmax.dl_capacity"), get(status, "wireless.polling.dl_capacity"))),
        "capacity_ul": num(first(get(sta, "airmax.ul_capacity"), get(status, "wireless.polling.ul_capacity"))),
        "capacity": num(mca.get("wlanPollingCapacity")),
        "quality": num(first(mca.get("wlanPollingQuality"), get(sta, "airmax.quality"), sta.get("ccq"), mca.get("ccq"))),
        "retries": num(first(sta.get("tx_lretries"), get(sta, "stats.tx_retries"), get(status, "wireless.stats.tx_retries"))),
        "latency_ms": num(first(sta.get("tx_latency"), mca.get("wlanTxLatency"))),
        "freq": num(first(get(status, "wireless.frequency"), mca.get("wlanFrequency"), mca.get("freq"), cfg.get("radio.1.freq"))),
        "center": num(first(get(status, "wireless.center1_freq"), cfg.get("radio.1.center.1.freq"))),
        "width": num(first(get(status, "wireless.chanbw"), cfg.get("radio.1.chanbw"), mca.get("chwidth"))),
        "distance_m": num(first(sta.get("distance"), get(status, "wireless.distance"), mca.get("distance"))),
        "txpower": num(first(get(status, "wireless.txpower"), cfg.get("radio.1.txpower"), mca.get("wlanTxPower"))),
        "ack": "auto" if cfg.get("radio.1.ack.auto") == "enabled" else cfg.get("radio.1.ackdistance"),
        "uptime_s": num(parts.get("uptime", "")),
        "cfg": cfg,
    }
    return r

def iperf(label):
    data = jsonish(sections.get("iperf3 " + label, "")) or {}
    end = data.get("end", {})
    if label.startswith("tcp"):
        r = {"mbps": get(end, "sum_received.bits_per_second"), "retransmits": get(end, "sum_sent.retransmits")}
    else:
        r = {"mbps": get(end, "sum.bits_per_second"), "jitter_ms": get(end, "sum.jitter_ms"),
             "lost_percent": get(end, "sum.lost_percent")}
    if r["mbps"] is not None:
        r["mbps"] = round(r["mbps"] / 1e6, 1)
    r["error"] = data.get("error") or (None if data else "no result")
    return r

def ping(label):
    text = sections.get("ping " + label, "")
    loss = re.search(r"([0-9.]+)% packet loss", text)
    rtt = re.search(r"= ([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+) ms", text)
    r = {"loss_percent": float(loss.group(1)) if loss else None}
    if rtt:
        r.update(zip(("min_ms", "avg_ms", "max_ms", "mdev_ms"), map(float, rtt.groups())))
    return r

labels = ("tcp to-boat", "tcp from-boat", "udp to-boat", "udp from-boat")
summary = {"raw": raw,
           "radios": {n: radio(sections.get("radio " + n, ""), role) for n, role in (("rocket", rocket_role), ("bullet", bullet_role))},
           "iperf": {l: iperf(l) for l in labels},
           "ping": {l: ping(l) for l in ("idle",) + labels}}
json.dump(summary, open(out, "w"), indent=1)

def f(v, fmt="%g"):
    return "?" if v is None else fmt % v if isinstance(v, (int, float)) else str(v)
print("%-7s %6s %6s %9s %8s %15s %-11s %9s %6s %10s %6s %5s" % (
    "radio", "signal", "noise", "cinr rx/tx", "quality", "capacity dl/ul", "mcs tx/rx", "rate tx/rx", "retry",
    "freq/width", "dist", "txpwr"))
for name, r in summary["radios"].items():
    print("%-7s %6s %6s %9s %8s %15s %-11s %9s %6s %10s %6s %5s" % (
        name, f(r["signal"]), f(r["noise"]), "%s/%s" % (f(r["cinr_rx"]), f(r["cinr_tx"])), f(r["quality"]),
        "%s/%s" % (f(r["capacity_dl"]), f(r["capacity_ul"])) if r["capacity_dl"] is not None else f(r["capacity"]),
        "%s/%s" % (f(r["tx_mcs"]), f(r["rx_mcs"])), "%s/%s" % (f(r["tx_rate"]), f(r["rx_rate"])), f(r["retries"]),
        "%s/%s" % (f(r["freq"]), f(r["width"])), f(r["distance_m"]), f(r["txpower"])))
    print("        ack %s, uptime %s s, tx latency %s ms" % (f(r["ack"]), f(r["uptime_s"], "%d"), f(r["latency_ms"])))
for label in labels:
    i, p = summary["iperf"][label], summary["ping"][label]
    extra = ("retrans %s" % f(i.get("retransmits"))) if label.startswith("tcp") else \
        "jitter %s ms, lost %s%%" % (f(i.get("jitter_ms"), "%.1f"), f(i.get("lost_percent"), "%.1f"))
    print("%-14s %7s Mbit/s  %-24s | ping avg %s max %s mdev %s ms, loss %s%%%s" % (
        label, f(i["mbps"]), extra, f(p.get("avg_ms")), f(p.get("max_ms")), f(p.get("mdev_ms")),
        f(p.get("loss_percent")), "  ERROR: " + i["error"] if i["error"] else ""))
p = summary["ping"]["idle"]
print("%-14s %7s         %-24s | ping avg %s max %s mdev %s ms, loss %s%%" % (
    "idle", "", "", f(p.get("avg_ms")), f(p.get("max_ms")), f(p.get("mdev_ms")), f(p.get("loss_percent"))))
PY
    info "Raw output: $RAW"
    info "Summary: ${RAW%.txt}.json; next: $PROGRAM propose"
}

propose() {
    local file=${1:-}
    if [[ -z $file ]]; then
        file=$(find "$STATE_DIR" -maxdepth 1 -name 'link-*.json' 2>/dev/null | sort | tail -1) || true
        [[ -n $file ]] || die "No measurement under $STATE_DIR; run $PROGRAM measure first"
    fi
    [[ -r $file ]] || die "Cannot read $file"
    info "Proposal from $file (nothing is applied)"
    python3 -I - "$file" "${LINK_RANGE_M:-1000}" "${LINK_FREQ:-}" "${LINK_WIDTH_MAX:-40}" "${LINK_TXPOWER_MAX:-}" <<'PY'
import json, math, re, sys
path, range_m, want_freq, width_max, power_max = sys.argv[1:]
m = json.load(open(path))
radios = m["radios"]

def fail(message):
    sys.exit("link proposal: " + message)
def integer(name, value, low, high):
    if not (re.fullmatch(r"[0-9]+", value) and low <= int(value) <= high):
        fail("%s must be an integer in %d..%d, not %r" % (name, low, high, value))
    return int(value)
range_m = integer("LINK_RANGE_M", range_m, 100, 10000)
width_max = integer("LINK_WIDTH_MAX", width_max, 20, 80)
if width_max not in (20, 40, 80):
    fail("LINK_WIDTH_MAX must be 20, 40 or 80")
want_freq = integer("LINK_FREQ", want_freq, 5150, 5895) if want_freq else None
power_max = integer("LINK_TXPOWER_MAX", power_max, 1, 30) if power_max else None

# 802.11 5 GHz grid: 20 MHz control channels and the 40/80 MHz blocks they belong to.
GRID = [f for f in range(5180, 5321, 20)] + [f for f in range(5500, 5721, 20)] + [f for f in range(5745, 5826, 20)]
BLOCKS = {40: [5190, 5230, 5270, 5310, 5510, 5550, 5590, 5630, 5670, 5710, 5755, 5795],
          80: [5210, 5290, 5530, 5610, 5690, 5775]}
def center(freq, width):
    if freq not in GRID:
        return None
    if width == 20:
        return freq
    return next((c for c in BLOCKS[width] if abs(freq - c) < width / 2), None)
def dfs(freq, width):
    c = center(freq, width) or freq
    return c + width / 2 > 5250 and c - width / 2 < 5730

changes = {name: [] for name in radios}
notes = []
def propose(name, key, value, reason):
    cfg = radios[name]["cfg"]
    if key not in cfg:
        notes.append("%s: %s is not in its config; skipped (%s)" % (name, key, reason))
    elif cfg[key] != str(value):
        changes[name].append((key, cfg[key], str(value), reason))

ap = next((n for n, r in radios.items() if r["role"] == "ap"), None)
noise = [r["noise"] for r in radios.values() if r["noise"] is not None]
snr = [r["signal"] - r["noise"] for r in radios.values() if r["signal"] is not None and r["noise"] is not None]
worst_noise, worst_snr = (max(noise) if noise else None), (min(snr) if snr else None)

# Width and channel: AP only. 40 MHz costs 3 dB of SNR; take it only on a quiet channel with margin.
if ap is None:
    notes.append("No radio has role ap (UBNT_*_ROLE); width and channel left alone")
elif worst_snr is None:
    notes.append("No signal/noise reading; width and channel left alone")
else:
    cfg = radios[ap]["cfg"]
    try:
        freq, width = int(cfg["radio.1.freq"]), int(cfg["radio.1.chanbw"])
    except (KeyError, ValueError):
        freq = width = None
    if width is None:
        notes.append("%s: no radio.1.freq/chanbw in its config; width and channel left alone" % ap)
    else:
        new_width = width
        if worst_noise <= -88 and worst_snr >= 32 and width_max >= 80:
            new_width = 80
        elif worst_noise <= -88 and worst_snr >= 25:
            new_width = 40
        elif worst_noise > -85 or worst_snr < 18:
            new_width = 20
        new_width = min(new_width, width_max)
        new_freq = want_freq or freq
        reason = "noise %g dBm, worst SNR %g dB" % (worst_noise, worst_snr)
        if (new_width, new_freq) != (width, freq):
            c = center(new_freq, new_width)
            if c is None:
                notes.append("%s: %d MHz is off the 802.11 grid for %d MHz; pick the channel in the airOS UI" % (ap, new_freq, new_width))
            elif "radio.1.center.1.freq" in cfg and str(center(freq, width)) != cfg["radio.1.center.1.freq"]:
                notes.append("%s: its freq/center pair (%s/%s) is not on the 802.11 grid; set width and channel in the airOS UI"
                             % (ap, freq, cfg["radio.1.center.1.freq"]))
            else:
                if new_freq != freq:
                    propose(ap, "radio.1.freq", new_freq, "LINK_FREQ from the survey")
                propose(ap, "radio.1.center.1.freq", c, "center of the %d MHz block" % new_width)
                propose(ap, "radio.1.chanbw", new_width, reason)
                mode = cfg.get("radio.1.ieee_mode", "")
                if re.fullmatch(r"11acvht(20|40|80)", mode):
                    propose(ap, "radio.1.ieee_mode", "11acvht%d" % new_width, "matches the width")
        else:
            notes.append("%s: keep %d MHz at %d MHz (%s)" % (ap, width, freq, reason))
        if dfs(new_freq, new_width) and cfg.get("radio.1.dfs.status") == "enabled":
            notes.append("%s: %d MHz/%d is a DFS channel: a radar hit or the 60 s channel check drops the link; "
                         "prefer 5180-5240 or 5745-5825 if the survey shows them quiet" % (ap, new_freq, new_width))
        if freq not in GRID:
            notes.append("%s: %d MHz is not a standard 20 MHz channel; confirm it is legal for the country code" % (ap, freq))

for name, r in radios.items():
    cfg, peer = r["cfg"], radios[[n for n in radios if n != name][0]]
    # TDMA for one station: airMAX polling on, AP in PtP mode.
    propose(name, "radio.1.polling", "enabled", "airMAX TDMA scheduling")
    if r["role"] == "ap":
        propose(name, "radio.1.ptpmode", "1", "one station: PtP framing")
    # Fixed distance for a moving boat: the farthest range plus margin, so ACKs never time out.
    dist = max(range_m, math.ceil((r["distance_m"] or 0) * 1.2 / 100) * 100)
    if "radio.1.ack.auto" in cfg or "radio.1.ackdistance" in cfg:
        propose(name, "radio.1.ack.auto", "disabled", "the distance changes as the boat moves")
        old = cfg.get("radio.1.ackdistance", "")
        propose(name, "radio.1.ackdistance", dist, "LINK_RANGE_M %d m, measured %s m" % (range_m, r["distance_m"]))
        if old.isdigit() and cfg.get("radio.1.acktimeout", "").isdigit():
            # Round trip at light speed: 2 * 1 km / 300 m/us = 6.7 us per km.
            timeout = int(cfg["radio.1.acktimeout"]) + math.ceil(2 * (dist - int(old)) / 300)
            propose(name, "radio.1.acktimeout", max(timeout, 1), "round-trip time for %d m" % dist)
    # Power: what the peer hears from this radio, now and (free-space loss) at LINK_RANGE_M.
    # -65..-50 dBm is clean; hotter distorts, weaker loses the top modulations.
    current, heard, measured = r["txpower"], peer["signal"], peer["distance_m"] or r["distance_m"]
    if current is not None and heard is not None and "radio.1.txpower" in cfg:
        far = heard - 20 * math.log10(max(range_m, measured) / measured) if measured else heard
        if not measured:
            notes.append("%s: no distance reading; power judged from the signal here only" % name)
        if cfg.get("radio.1.atpc.sta.status") == "enabled" and r["role"] != "ap":
            notes.append("%s: ATPC sets its power; txpower left alone" % name)
        elif far < -65:
            ceiling = power_max if power_max is not None else int(current)
            raised = min(ceiling, int(current + math.ceil(-58 - far)))
            if raised > current:
                propose(name, "radio.1.txpower", raised, "peer would hear %.0f dBm at %d m" % (far, range_m))
            else:
                notes.append("%s: peer would hear %.0f dBm at %d m; raise power only with LINK_TXPOWER_MAX after an EIRP check"
                             % (name, far, range_m))
        elif heard > -50 and int(min(heard + 50, far + 65)) >= 1:
            propose(name, "radio.1.txpower", int(current - min(heard + 50, far + 65)),
                    "peer hears %g dBm here, %.0f dBm at %d m" % (heard, far, range_m))
    # Queueing on the radio adds latency; the boat's traffic is far below the link rate.
    propose(name, "tshaper.status", "disabled", "traffic shaping queues packets")

link_keys = re.compile(r"^radio\.1\.(freq|center\.1\.freq|chanbw|ieee_mode)$")
for name, items in changes.items():
    print("%s (%s):" % (name, radios[name]["role"] or "?"))
    if not items:
        print("  no change")
    for key, old, new, reason in items:
        print("  %-24s %s -> %s  (%s)" % (key, old, new, reason))
for note in notes:
    print("note: " + note)
print("\nEach trial reverts in 300 s unless you type KEEP; re-run measure before KEEP. Channel/width first, one radio at a time:")
for name, items in sorted(changes.items(), key=lambda kv: radios[kv[0]]["role"] != "ap"):
    for group in ([i for i in items if link_keys.match(i[0])], [i for i in items if not link_keys.match(i[0])]):
        if group:
            pairs = " ".join("%s=%s" % (k, n) for k, _, n, _ in group)
            print("  ./ubiquiti_setup.sh apply %s --dry-run %s" % (name, pairs))
            print("  ./ubiquiti_setup.sh apply %s %s" % (name, pairs))
PY
}

case ${1:-} in
    measure) BOAT_SSH=${BOAT_HOST_USER:?BOAT_HOST_USER (the boat login) is unset; set it in .env, see .env.example}@$BOAT_TRANSPORT_IP
        measure ;;
    propose) propose "${2:-}" ;;
    -h|--help|'') usage ;;
    *) die "Unknown command: $1" ;;
esac
