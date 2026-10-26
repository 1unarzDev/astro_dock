#!/usr/bin/env bash
# Stubbed checks for the router auto-revert (devices/router/ddwrt.sh) and stage_boat's gateway guard.
# Nothing here contacts a router or changes the host network.
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fail() { echo "FAIL: $*"; exit 1; }

# A fake DD-WRT: one file per NVRAM key, run by router_ssh through sh with these on PATH.
mkdir -p "$tmp/bin"
cat >"$tmp/bin/nvram" <<'EOF'
#!/bin/sh
case $1 in
    get) [ -f "$NV/$2" ] && cat "$NV/$2"; echo; exit 0 ;;
    set) k=${2%%=*}; printf '%s' "${2#*=}" >"$NV/$k" ;;
    unset) rm -f "$NV/$2" ;;
    show) for f in "$NV"/*; do [ -f "$f" ] && printf '%s=%s\n' "${f##*/}" "$(cat "$f")"; done ;;
    commit) : ;;
esac
EOF
printf '#!/bin/sh\n:\n' >"$tmp/bin/sleep"
printf '#!/bin/sh\necho rebooted >>"$NV/../reboots"\n' >"$tmp/bin/reboot"
printf '#!/bin/sh\n:\n' >"$tmp/bin/setuserpasswd"
chmod +x "$tmp/bin"/*

# The router driver with the fixture vessel a's values; sourcing it runs nothing.
HW_ROOT=$root ASTRO_ENV_FILE=/dev/null ASTRO_SECRETS=/nonexistent
export ASTRO_PROFILES="$root/tests/data/profiles" ASTRO_VESSEL=a
source "$root/devices/lib.sh"
hw_init
hw_load_kind router
source "$root/devices/router/ddwrt.sh"
router_settings --state-dir "$tmp/state"
trap 'rm -rf "$tmp"' EXIT
set +e
router_ssh() {
    local cmd=${1//\/proc\/sys\/kernel\/random\/boot_id/$tmp/boot_id}
    PATH="$tmp/bin:$PATH" NV=$NV sh -c "$cmd"
}
sleep() { SECONDS=$((SECONDS + $1)); }
RADIOS=(wl0) PUBLIC_KEY="ssh-ed25519 AAAA" ADMIN_PASSWORD=secretpw WIFI_PASSWORD=wifipass1

fresh() {
    NV="$tmp/nv$1"; mkdir -p "$NV"; rm -f "$NV"/* "$tmp/reboots"
    printf 'owner-script' >"$NV/rc_startup"
    printf '192.168.1.1' >"$NV/lan_ipaddr"
    printf 'old-ssid' >"$NV/wl0_ssid"
    echo boot-A >"$tmp/boot_id"
}
apply() { build_apply_script | router_ssh 'sh -s'; }
boot() { echo "$1" >"$tmp/boot_id"; PATH="$tmp/bin:$PATH" NV=$NV sh -c "$(cat "$NV/rc_startup")"$'\nwait'; }
nv() { cat "$NV/$1" 2>/dev/null || echo "<unset>"; }

# Apply arms the revert; a timeout restores old values, unsets new keys, keeps the owner script.
fresh 1
apply || fail "apply"
[[ $(nv lan_ipaddr) == "$ROUTER_IP" && $(nv astro_prev_rc_startup) == xowner-script ]] || fail "apply state"
[[ $(nv rc_startup) == *"sleep $REVERT_SECONDS;"* && $REVERT_SECONDS == 300 ]] || fail "revert delay from the profile"
boot boot-B
[[ $(nv lan_ipaddr) == 192.168.1.1 && $(nv wl0_ssid) == old-ssid ]] || fail "revert values"
[[ $(nv dhcpfwd_enable) == "<unset>" && $(nv rc_startup) == owner-script ]] || fail "revert unset/rc_startup"
[[ $(nv astro_prev_rc_startup) == "<unset>" && -s $tmp/reboots ]] || fail "revert flag/reboot"

# Confirm after the timeout: refuse, leave rc_startup alone.
out=$( (confirm_router) 2>&1 ) && fail "confirm after timeout succeeded"
grep -q "already ran or was never armed" <<<"$out" || fail "confirm after timeout message"
[[ $(nv rc_startup) == owner-script ]] || fail "confirm after timeout touched rc_startup"

# Confirm before the reboot: wait_for_router never sees a new boot ID, so confirm never runs.
fresh 2
apply || fail "apply 2"
out=$( (wait_for_router boot-A; confirm_router) 2>&1 ) && fail "wait passed on the old boot"
grep -q "did not reboot" <<<"$out" || fail "wait message"
[[ $(nv astro_prev_rc_startup) == xowner-script ]] || fail "flag cleared before reboot"

# Re-apply while unconfirmed: exit 3 and the original revert stays intact.
revert=$(nv rc_startup)
ROUTER_IP=10.0.1.9 apply; [[ $? == 3 ]] || fail "re-apply not refused"
[[ $(nv rc_startup) == "$revert" && $(nv lan_ipaddr) == "$ROUTER_IP" ]] || fail "re-apply changed state"

# Normal path: new boot, confirm restores the owner script and the revert no longer fires.
echo boot-C >"$tmp/boot_id"
(wait_for_router boot-A && confirm_router) >/dev/null || fail "confirm after reboot"
[[ $(nv rc_startup) == owner-script && $(nv astro_prev_rc_startup) == "<unset>" ]] || fail "confirm state"
echo "router revert ok"

# Rollback: a valid backup is restored (differing keys only, never rc_startup) under the same timer.
trust_host_key() { :; }
authenticate() { AUTH_MODE=key; ssh_base_options; }
b64() { printf '%s\n' "$1" | base64 | tr -d '\n'; }
keydir="$tmp/state/$(cfg lan.address)"
rollback() { ( driver_rollback --state-dir "$tmp/state" ) <<<$'1\nRESTORE' 2>&1; }
backup() {
    mkdir -p "$keydir"; rm -f "$keydir"/nvram-*
    printf 'lan_ipaddr\t%s\nwl0_ssid\t%s\nrc_startup\t%s\nos_version\t%s\n' "$(b64 192.168.1.2)" \
        "$(b64 backup-ssid)" "$(b64 other-script)" "$(b64 r1)" >"$keydir/nvram-20260101-000000.b64"
}
# Unconfirmed: no new boot, so no confirm; the next boot returns to the pre-restore values.
fresh 3; printf r1 >"$NV/os_version"; backup
out=$(rollback) && fail "unconfirmed rollback succeeded"
grep -q "did not reboot" <<<"$out" && grep -q "rc_startup differs" <<<"$out" || fail "unconfirmed rollback messages: $out"
! grep -qi "restored and confirmed" <<<"$out" || fail "unconfirmed rollback claimed success"
[[ $(nv wl0_ssid) == backup-ssid && $(nv lan_ipaddr) == 192.168.1.2 ]] || fail "rollback values"
[[ $(nv astro_prev_rc_startup) == xowner-script && $(nv rc_startup) == *"sleep $REVERT_SECONDS;"* ]] || fail "rollback timer"
[[ $(nv rc_startup) != *os_version* ]] || fail "rollback set an unchanged key"
ls "$keydir"/nvram-*-before-restore.b64 >/dev/null 2>&1 || fail "no pre-restore backup"
boot boot-B
[[ $(nv wl0_ssid) == old-ssid && $(nv lan_ipaddr) == 192.168.1.1 && $(nv sshd_authorized_keys) == "<unset>" ]] || fail "rollback revert values"
[[ $(nv rc_startup) == owner-script && $(nv astro_prev_rc_startup) == "<unset>" ]] || fail "rollback revert rc_startup"
# Confirmed: the router answers at the restored address after a new boot; the timer is cancelled.
fresh 4; printf r1 >"$NV/os_version"; backup
printf '#!/bin/sh\necho rebooted >>"$NV/../reboots"\necho boot-R >"$NV/../boot_id"\n' >"$tmp/bin/reboot"
out=$(rollback) || fail "confirmed rollback: $out"
grep -q "Backup restored and confirmed" <<<"$out" || fail "confirmed rollback message"
[[ $(nv wl0_ssid) == backup-ssid && $(nv lan_ipaddr) == 192.168.1.2 ]] || fail "confirmed rollback values"
[[ $(nv rc_startup) == owner-script && $(nv astro_prev_rc_startup) == "<unset>" ]] || fail "confirmed rollback state"
grep -qF "$(cat "$keydir/id_router.pub")" "$NV/sshd_authorized_keys" || fail "rollback dropped the tool's key"
echo "router rollback ok"

# stage_boat: the gateway host keeps its network even when GATEWAY_IP is not the first address.
read -r gw subnet < <(. "$ASTRO_PROFILES/a/network.env"; echo "$GATEWAY_IP $LAN_SUBNET")
other="${gw%.*}.77"
mkdir -p "$tmp/host"
cat >"$tmp/host/nmcli" <<'EOF'
#!/bin/sh
case "$*" in
    "-g RUNNING general") echo running ;;
    "-g DEVICE,TYPE device") echo eth0:ethernet ;;
    *) exit 0 ;;
esac
EOF
printf '#!/bin/sh\n[ "$1 $2 $3" = "-4 -o addr" ] && printf "%%s\\n" "$ADDRS" | tr ";" "\\n"\nexit 0\n' >"$tmp/host/ip"
chmod +x "$tmp/host"/*
boat() { ADDRS="$1" PATH="$tmp/host:$PATH" "$root/setup.linux.sh" --dry-run --non-interactive --components boat 2>&1; }
line() { echo "2: eth0    inet $1/${subnet#*/} scope global eth0"; }
out=$(boat "$(line "$other");$(line "$gw")")
grep -q "holds the gateway address" <<<"$out" || fail "gateway second address not detected"
out=$(boat "$(line "$other")")
! grep -q "holds the gateway address" <<<"$out" || fail "non-gateway host treated as gateway"
echo "stage_boat gateway ok"
