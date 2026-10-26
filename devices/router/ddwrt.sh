# shellcheck shell=bash
# DD-WRT flat-LAN router from the profile's router.yaml: DHCP (with reservations), DNS and WPA2 Wi-Fi,
# router WAN/NAT off, clients' default gateway = the forwarding host. apply is one NVRAM transaction
# that restores the previous values revert_seconds after the next boot unless verification confirms it.

driver_usage() {
    cat <<EOF
Usage: $HW_PROGRAM $HW_KIND status|plan|apply|verify|rollback|key [--host ADDRESS] [--identity FILE] [--state-dir DIR]

status    Read-only: firmware, radios and the LAN/DHCP/Wi-Fi NVRAM values.
plan      Connect, inspect and preview the configuration; changes nothing on the router.
apply     plan, then (after you type APPLY) back up NVRAM, commit one transaction and reboot. It
          restores the previous values $(cfg revert_seconds)s after boot unless the post-reboot verify passes.
verify    Read-only: check NVRAM and the dnsmasq config the router serves against the profile.
rollback  Restore a saved NVRAM backup (you pick it and type RESTORE): refused unless every line
          is KEY<TAB>VALUE and lan_ipaddr is an IPv4. Only differing keys are set, under the same
          timed revert as apply, confirmed once the router answers at the restored lan_ipaddr.
key       Print the per-router public key for DD-WRT's Authorized Keys.
--host is the router's current address (default: lan.address). State (key, known_hosts, backups):
the ignored router_setup_state/ when present, else ~/.local/state/astro_dock/router.
status, plan and verify write nothing to the router; locally they may create the router key and
add its host key to known_hosts (after you accept the fingerprint).
Secrets: the .env keys named under credentials; empty passwords are asked for.
EOF
}

# router_settings [OPTIONS]: profile values and options into the variables below.
router_settings() {
    local dns
    STATE_DIR=$HW_ROOT/router_setup_state
    [[ -d $STATE_DIR ]] || STATE_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/astro_dock/router
    IDENTITY='' SSH_PORT=22
    ROUTER_IP=$(cfg lan.address) NETMASK=$(cfg lan.netmask) LAPTOP_IP=$(cfg lan.gateway)
    HOST=$ROUTER_IP
    while (($#)); do
        case $1 in
            --host) (($# >= 2)) || die "--host requires a value"; HOST=$2; shift ;;
            --identity) (($# >= 2)) || die "--identity requires a value"; IDENTITY=$2; shift ;;
            --state-dir) (($# >= 2)) || die "--state-dir requires a value"; STATE_DIR=$2; shift ;;
            *) die "Unknown option: $1" ;;
        esac
        shift
    done
    dns=$(cfg dns)
    DNS_PRIMARY=${dns%% *} DNS_SECONDARY=${dns#* }
    DHCP_START=$(cfg dhcp.start) DHCP_COUNT=$(cfg dhcp.count) LEASE_MINUTES=$(cfg dhcp.lease_minutes)
    STATIC_LEASES=$(cfg dhcp.reservations) TIME_ZONE=$(cfg time_zone) REVERT_SECONDS=$(cfg revert_seconds)
    ROUTER_NAME=$(cfg name) SSID=$(cfg ssid) ADMIN_USER=$(cfg admin_user)
    SSID=${SSID:-$ROUTER_NAME} ADMIN_USER=${ADMIN_USER:-$ROUTER_NAME}
    SSH_USER=$(secret credentials.user) SSH_USER=${SSH_USER:-root}
    ADMIN_PASSWORD=$(secret credentials.admin_password) WIFI_PASSWORD=$(secret credentials.wifi_password)
    KNOWN_HOSTS='' KEY_DIR='' PUBLIC_KEY='' AUTH_MODE='' CURRENT_PASSWORD='' TRUSTED_HOST=''
    RADIOS=() DDWRT_VERSION='' BACKUP_FILE='' BACKUP_ENCODING=''
    local tool
    for tool in ssh ssh-keygen ssh-keyscan awk sed grep sort find; do
        command -v "$tool" >/dev/null 2>&1 || die "Required command not found: $tool"
    done
    trap 'CURRENT_PASSWORD=x; unset CURRENT_PASSWORD SSHPASS' EXIT
}

is_ipv4() {
    local ip=$1 a b c d extra octet
    IFS=. read -r a b c d extra <<<"$ip"
    [[ -z ${extra:-} && -n ${a:-} && -n ${b:-} && -n ${c:-} && -n ${d:-} ]] || return 1
    for octet in "$a" "$b" "$c" "$d"; do
        [[ $octet =~ ^[0-9]{1,3}$ ]] || return 1
        ((10#$octet <= 255)) || return 1
    done
}

ip_to_int() {
    local a b c d
    IFS=. read -r a b c d <<<"$1"
    printf '%u' "$(( (10#$a << 24) + (10#$b << 16) + (10#$c << 8) + 10#$d ))"
}

netmask_to_int() {
    is_ipv4 "$1" || return 1
    local value inverted
    value=$(ip_to_int "$1")
    inverted=$(( 0xFFFFFFFF ^ value ))
    (( (inverted & (inverted + 1)) == 0 ))
    printf '%u' "$value"
}

same_subnet() {
    local mask
    mask=$(netmask_to_int "$3") || return 1
    (( ( $(ip_to_int "$1") & mask ) == ( $(ip_to_int "$2") & mask ) ))
}

# ask_secret VARIABLE LABEL: prompt (hidden) when the .env key is empty.
ask_secret() {
    [[ -z ${!1} ]] || return 0
    local input
    read -r -s -p "$2: " input
    printf '\n'
    printf -v "$1" '%s' "$input"
}

validate_settings() {
    local value start_ip end_host
    for value in HOST ROUTER_IP NETMASK LAPTOP_IP DNS_PRIMARY DNS_SECONDARY; do
        is_ipv4 "${!value}" || die "$value is not a valid IPv4 address: ${!value}"
    done
    netmask_to_int "$NETMASK" >/dev/null || die "NETMASK is not contiguous: $NETMASK"
    same_subnet "$ROUTER_IP" "$LAPTOP_IP" "$NETMASK" || die "Router and gateway must be on the same LAN subnet"
    [[ $DHCP_START =~ ^[0-9]+$ && $DHCP_COUNT =~ ^[0-9]+$ && $LEASE_MINUTES =~ ^[0-9]+$ ]] \
        || die "DHCP start/count/lease_minutes must be integers"
    (( DHCP_START >= 2 && DHCP_START <= 254 && DHCP_COUNT >= 1 )) || die "Invalid DHCP range"
    end_host=$((DHCP_START + DHCP_COUNT - 1))
    (( end_host <= 254 )) || die "DHCP range ends beyond host 254"
    start_ip=${ROUTER_IP%.*}.$DHCP_START
    same_subnet "$ROUTER_IP" "$start_ip" "$NETMASK" || die "DHCP range is outside the LAN subnet"
    [[ $ROUTER_IP != "$LAPTOP_IP" ]] || die "Router and gateway addresses must differ"
    # The post-reboot wait is two minutes; the revert must not fire before it can be confirmed.
    [[ $REVERT_SECONDS =~ ^[0-9]+$ ]] && ((REVERT_SECONDS >= 240)) || die "revert_seconds must be at least 240"
    [[ -n $ROUTER_NAME && -n $SSID && -n $ADMIN_USER && -n $TIME_ZONE ]] || die "name and time_zone cannot be empty"
    [[ $ROUTER_NAME != *$'\n'* && $SSID != *$'\n'* && $ADMIN_USER != *$'\n'* ]] || die "Names cannot contain newlines"
    ask_secret ADMIN_PASSWORD "Web-admin password ($(cfg credentials.admin_password) is empty)"
    ask_secret WIFI_PASSWORD "Wi-Fi password ($(cfg credentials.wifi_password) is empty)"
    (( ${#WIFI_PASSWORD} >= 8 && ${#WIFI_PASSWORD} <= 63 )) || die "WPA2 password must contain 8-63 characters"
    [[ $ADMIN_PASSWORD != *$'\n'* && $WIFI_PASSWORD != *$'\n'* ]] || die "Passwords cannot contain newlines"
    [[ -n $ADMIN_PASSWORD ]] || die "Set $(cfg credentials.admin_password) in .env or enter a web-admin password"
}

initialize_state() {
    local host_slug
    host_slug=${HOST//[^A-Za-z0-9_.-]/_}
    KEY_DIR="$STATE_DIR/$host_slug"
    KNOWN_HOSTS="$STATE_DIR/known_hosts"
    TRUSTED_HOST=$HOST
    mkdir -p -- "$KEY_DIR"
    chmod 700 -- "$STATE_DIR" "$KEY_DIR"
    touch -- "$KNOWN_HOSTS"
    chmod 600 -- "$KNOWN_HOSTS"
    [[ -n $IDENTITY ]] || IDENTITY="$KEY_DIR/id_router"
}

ensure_key() {
    if [[ -f $IDENTITY && -f $IDENTITY.pub ]]; then
        :
    elif [[ -f $IDENTITY ]]; then
        ssh-keygen -y -f "$IDENTITY" >"$IDENTITY.pub"
        chmod 600 -- "$IDENTITY.pub"
    else
        info "Generating a unique per-router SSH key at $IDENTITY"
        if ! ssh-keygen -q -t ed25519 -N '' -C '' -f "$IDENTITY"; then
            warn "Ed25519 generation failed; falling back to RSA 3072."
            ssh-keygen -q -t rsa -b 3072 -N '' -C '' -f "$IDENTITY"
        fi
    fi
    chmod 600 -- "$IDENTITY" "$IDENTITY.pub"
    # Authorized Keys needs only the algorithm and encoded key. Comments such as
    # user@host are optional and make the copy/paste instructions look misleading.
    PUBLIC_KEY=$(awk 'NF >= 2 { print $1 " " $2; exit }' "$IDENTITY.pub")
    [[ $PUBLIC_KEY == *' '* ]] || die "Could not read a valid public key from $IDENTITY.pub"
    printf '%s\n' "$PUBLIC_KEY" >"$IDENTITY.pub"
}

trust_host_key() {
    ssh-keygen -F "$HOST" -f "$KNOWN_HOSTS" >/dev/null 2>&1 && return
    local scan answer
    scan=$(mktemp)
    if ! ssh-keyscan -T 5 -p "$SSH_PORT" "$HOST" >"$scan" 2>/dev/null || [[ ! -s $scan ]]; then
        rm -f -- "$scan"
        die "Could not retrieve an SSH host key from $HOST:$SSH_PORT. Is SSH enabled?"
    fi
    printf '\nSSH host-key fingerprint for %s:\n' "$HOST"
    ssh-keygen -lf "$scan"
    read -r -p "Trust and save this host key? [y/N]: " answer
    [[ $answer =~ ^[Yy]$ ]] || { rm -f -- "$scan"; die "Host key was not trusted"; }
    ssh-keygen -H -f "$scan" >/dev/null 2>&1
    grep -v '^#' "$scan" >>"$KNOWN_HOSTS"
    rm -f -- "$scan" "$scan.old"
}

ssh_base_options() {
    SSH_OPTIONS=(
        -p "$SSH_PORT"
        -o "UserKnownHostsFile=$KNOWN_HOSTS"
        -o "HostKeyAlias=${TRUSTED_HOST:-$HOST}"
        -o StrictHostKeyChecking=yes
        -o ConnectTimeout=8
        -o ServerAliveInterval=10
        -o ServerAliveCountMax=2
        -i "$IDENTITY"
    )
}

router_ssh() {
    case $AUTH_MODE in
        key) ssh "${SSH_OPTIONS[@]}" -o BatchMode=yes "$SSH_USER@$HOST" "$@" ;;
        password)
            SSHPASS=$CURRENT_PASSWORD sshpass -e ssh "${SSH_OPTIONS[@]}" \
                -o PreferredAuthentications=password -o PubkeyAuthentication=no \
                "$SSH_USER@$HOST" "$@"
            ;;
        *) return 1 ;;
    esac
}

authenticate() {
    ssh_base_options
    if ssh "${SSH_OPTIONS[@]}" -o BatchMode=yes "$SSH_USER@$HOST" true </dev/null 2>/dev/null; then
        AUTH_MODE=key
        info "Authenticated to $HOST with the router key."
        return
    fi

    if command -v sshpass >/dev/null 2>&1; then
        CURRENT_PASSWORD=$(secret credentials.password)
        if [[ -z $CURRENT_PASSWORD ]]; then
            read -r -s -p "Existing DD-WRT SSH password (leave blank for key-only setup): " CURRENT_PASSWORD
            printf '\n'
        fi
        if [[ -n $CURRENT_PASSWORD ]] && SSHPASS=$CURRENT_PASSWORD sshpass -e ssh \
            "${SSH_OPTIONS[@]}" -o PreferredAuthentications=password -o PubkeyAuthentication=no \
            "$SSH_USER@$HOST" true </dev/null 2>/dev/null; then
            AUTH_MODE=password
            info "Authenticated with the existing DD-WRT password."
            return
        fi
        [[ -z $CURRENT_PASSWORD ]] || warn "Password authentication failed."
    else
        warn "sshpass is unavailable, so automatic password bootstrap is disabled."
    fi

    printf '\nNo working SSH credential was found. In DD-WRT, open Services > Services,\n'
    printf 'enable SSHd, paste this public key into Authorized Keys, and Apply Settings:\n\n%s\n\n' "$PUBLIC_KEY"
    printf 'The SSH login name is root even when the Web UI username is different.\n'
    local answer
    while true; do
        read -r -p "Press Enter after installing the key, or type q to quit: " answer
        [[ $answer != q && $answer != Q ]] || exit 1
        if ssh "${SSH_OPTIONS[@]}" -o BatchMode=yes "$SSH_USER@$HOST" true </dev/null 2>/dev/null; then
            AUTH_MODE=key
            info "Public-key authentication is working."
            return
        fi
        warn "The key is not accepted yet. Check SSHd and Authorized Keys in DD-WRT."
    done
}

connect() { initialize_state; ensure_key; trust_host_key; authenticate; }

probe_router() {
    local probe radio
    probe=$(router_ssh 'printf "version="; version=$(nvram get os_version); [ -n "$version" ] || version=$(nvram get dist_type); [ -n "$version" ] || version=$(uname -n); printf "%s\n" "$version"; printf "build="; nvram get os_date; command -v nvram >/dev/null || exit 20; command -v base64 >/dev/null && echo base64=yes || echo base64=no; command -v od >/dev/null && echo od=yes || echo od=no; command -v setuserpasswd >/dev/null && echo setuserpasswd=yes || echo setuserpasswd=no; nvram show 2>/dev/null | sed -n "s/^\(wl[0-9][0-9]*\|ath[0-9][0-9]*\|wlan[0-9][0-9]*\)_\(ssid\|ifname\)=.*/radio=\1/p" | sort -u' </dev/null) \
        || die "Router inspection failed"
    DDWRT_VERSION=$(awk -F= '/^version=/{print $2; exit}' <<<"$probe")
    [[ -n $DDWRT_VERSION ]] || die "The target does not identify itself as DD-WRT"
    grep -q '^setuserpasswd=yes$' <<<"$probe" || die "This build lacks setuserpasswd; set the requested admin credentials in the Web UI, then rerun"
    if grep -q '^base64=yes$' <<<"$probe"; then
        BACKUP_ENCODING=base64
    elif grep -q '^od=yes$' <<<"$probe"; then
        BACKUP_ENCODING=hex
        info "Router has no base64 utility; NVRAM backups will use portable hexadecimal encoding."
    else
        BACKUP_ENCODING=none
        warn "Router has neither base64 nor od; configuration can continue, but no automatic NVRAM backup can be created."
    fi
    mapfile -t RADIOS < <(awk -F= '/^radio=/{print $2}' <<<"$probe" | sort -u)
    ((${#RADIOS[@]})) || die "No supported DD-WRT physical radio (wl*, ath*, or wlan*) was detected"
    for radio in "${RADIOS[@]}"; do
        [[ $radio =~ ^(wl|ath|wlan)[0-9]+$ ]] || die "Unsafe radio name returned by router: $radio"
    done
    info "Detected DD-WRT $DDWRT_VERSION with radio(s): ${RADIOS[*]}"
}

# make_backup [SUFFIX]: save every NVRAM key, encoded, to KEY_DIR/nvram-STAMP[SUFFIX].b64|.hex.
make_backup() {
    local stamp
    if [[ $BACKUP_ENCODING == none ]]; then
        BACKUP_FILE=""
        warn "Proceeding without an NVRAM backup because this DD-WRT build lacks encoding tools."
        return
    fi
    stamp=$(date +%Y%m%d-%H%M%S)${1:-}
    if [[ $BACKUP_ENCODING == base64 ]]; then
        BACKUP_FILE="$KEY_DIR/nvram-$stamp.b64"
    else
        BACKUP_FILE="$KEY_DIR/nvram-$stamp.hex"
    fi
    umask 077
    if [[ $BACKUP_ENCODING == base64 ]]; then
        router_ssh 'for key in $(nvram show 2>/dev/null | sed -n "s/^\([A-Za-z0-9_.:-][A-Za-z0-9_.:-]*\)=.*/\1/p"); do encoded=$(nvram get "$key" | base64 | tr -d "\n"); printf "%s\t%s\n" "$key" "$encoded"; done' \
            </dev/null >"$BACKUP_FILE" || { rm -f -- "$BACKUP_FILE"; die "NVRAM backup failed"; }
    else
        router_ssh 'for key in $(nvram show 2>/dev/null | sed -n "s/^\([A-Za-z0-9_.:-][A-Za-z0-9_.:-]*\)=.*/\1/p"); do encoded=$(nvram get "$key" | od -An -v -tx1 | tr -d " \n"); printf "%s\t%s\n" "$key" "$encoded"; done' \
            </dev/null >"$BACKUP_FILE" || { rm -f -- "$BACKUP_FILE"; die "NVRAM backup failed"; }
    fi
    chmod 600 -- "$BACKUP_FILE"
    [[ -s $BACKUP_FILE ]] || { rm -f -- "$BACKUP_FILE"; die "NVRAM backup was empty"; }
    info "Saved permission-restricted NVRAM backup: $BACKUP_FILE"
}

shell_quote() {
    local value=$1
    printf "'%s'" "${value//\'/\'\\\'\'}"
}

nvset_line() {
    local key=$1 value=$2
    printf 'nvram set %s=%s\n' "$key" "$(shell_quote "$value")"
}

build_apply_script() {
    local script='' radio existing_keys merged_keys dns_options lease static_leases='' static_count=0
    existing_keys=$(router_ssh 'nvram get sshd_authorized_keys' </dev/null 2>/dev/null || true)
    if grep -Fq -- "$PUBLIC_KEY" <<<"$existing_keys"; then
        merged_keys=$existing_keys
    elif [[ -n $existing_keys ]]; then
        merged_keys="$existing_keys"$'\n'"$PUBLIC_KEY"
    else
        merged_keys=$PUBLIC_KEY
    fi
    # Explicitly advertise the LAN mask as well as the gateway. With a correct connected route,
    # clients reach the DD-WRT UI directly while sending only off-subnet traffic to the forwarding
    # host. Options are tagged br0: DD-WRT's own dhcp-option=br0,3,<router> outranks untagged ones.
    dns_options="dhcp-option=br0,1,$NETMASK"$'\n'"dhcp-option=br0,3,$LAPTOP_IP"$'\n'"dhcp-option=br0,6,$DNS_PRIMARY,$DNS_SECONDARY"
    for lease in $STATIC_LEASES; do
        static_leases+="$lease=$LEASE_MINUTES "
        ((static_count += 1))
    done

    script+=$(nvset_line wan_proto disabled)$'\n'
    script+=$(nvset_line wk_mode router)$'\n'
    # On DD-WRT lan_proto=dhcp is "DHCP Type: DHCP Server"; static drops dhcp-range
    # and dhcp-host from dnsmasq.conf. The router's own address is lan_ipaddr.
    script+=$(nvset_line lan_proto dhcp)$'\n'
    script+=$(nvset_line lan_ipaddr "$ROUTER_IP")$'\n'
    script+=$(nvset_line lan_netmask "$NETMASK")$'\n'
    script+=$(nvset_line lan_gateway "$LAPTOP_IP")$'\n'
    script+=$(nvset_line router_name "$ROUTER_NAME")$'\n'
    script+=$(nvset_line wan_hostname "$ROUTER_NAME")$'\n'
    script+=$(nvset_line dhcp_start "$DHCP_START")$'\n'
    script+=$(nvset_line dhcp_num "$DHCP_COUNT")$'\n'
    script+=$(nvset_line dhcp_lease "$LEASE_MINUTES")$'\n'
    script+=$(nvset_line static_leases "${static_leases% }")$'\n'
    script+=$(nvset_line static_leasenum "$static_count")$'\n'
    script+=$(nvset_line dhcpfwd_enable 0)$'\n'
    script+=$(nvset_line dhcpd_enable 1)$'\n'
    script+=$(nvset_line dnsmasq_enable 1)$'\n'
    script+=$(nvset_line dhcp_dnsmasq 1)$'\n'
    script+=$(nvset_line dns_dnsmasq 1)$'\n'
    script+=$(nvset_line auth_dnsmasq 1)$'\n'
    script+=$(nvset_line wan_dns "$DNS_PRIMARY $DNS_SECONDARY")$'\n'
    script+=$(nvset_line sv_localdns "$DNS_PRIMARY")$'\n'
    script+=$(nvset_line dnsmasq_options "$dns_options")$'\n'
    script+=$(nvset_line ipv6_enable 0)$'\n'
    script+=$(nvset_line spi_firewall 0)$'\n'
    script+=$(nvset_line remote_management 0)$'\n'
    script+=$(nvset_line remote_mgt_ssh 0)$'\n'
    script+=$(nvset_line remote_mgt_telnet 0)$'\n'
    script+=$(nvset_line telnetd_enable 0)$'\n'
    script+=$(nvset_line upnp_enable 0)$'\n'
    script+=$(nvset_line wps_enable 0)$'\n'
    script+=$(nvset_line sshd_enable 1)$'\n'
    script+=$(nvset_line sshd_passwd 1)$'\n'
    script+=$(nvset_line sshd_authorized_keys "$merged_keys")$'\n'
    script+=$(nvset_line ntp_enable 1)$'\n'
    script+=$(nvset_line time_zone "$TIME_ZONE")$'\n'

    for radio in "${RADIOS[@]}"; do
        script+=$(nvset_line "${radio}_mode" ap)$'\n'
        script+=$(nvset_line "${radio}_net_mode" mixed)$'\n'
        script+=$(nvset_line "${radio}_ssid" "$SSID")$'\n'
        script+=$(nvset_line "${radio}_closed" 0)$'\n'
        script+=$(nvset_line "${radio}_ap_isolate" 0)$'\n'
        script+=$(nvset_line "${radio}_bridged" 1)$'\n'
        script+=$(nvset_line "${radio}_security_mode" psk2)$'\n'
        script+=$(nvset_line "${radio}_crypto" aes)$'\n'
        script+=$(nvset_line "${radio}_akm" psk2)$'\n'
        script+=$(nvset_line "${radio}_wpa_psk" "$WIFI_PASSWORD")$'\n'
        script+=$(nvset_line "${radio}_wps_mode" disabled)$'\n'
    done
    script+=$(nvset_line wl_ssid "$SSID")$'\n'
    script+="setuserpasswd $(shell_quote "$ADMIN_USER") $(shell_quote "$ADMIN_PASSWORD")"$'\n'
    script+="nvram commit"$'\n'
    script+="sync"$'\n'
    printf 'set -e\ncommand -v nvram >/dev/null\n%s\n%s' "$(revert_lines "$script")" "$script"
}

# Router-side lines, run before any change, that save the current value of every key the
# transaction sets into a startup script (keys absent before are unset again). REVERT_SECONDS
# after the next boot it restores them and reboots, unless confirm_router has cleared
# astro_prev_rc_startup. It lives in NVRAM because the apply reboots, and checks NVRAM
# rather than /tmp for the same reason. An unconfirmed earlier apply exits 3 untouched.
revert_lines() {
    local keys
    keys=$(sed -n 's/^nvram set \([A-Za-z0-9_.:-]*\)=.*/\1/p' <<<"$1" | sort -u | tr '\n' ' ')
    sed -e "s/@KEYS@/$keys/" -e "s/@SECONDS@/$((10#$REVERT_SECONDS))/" <<'EOF'
[ -z "$(nvram get astro_prev_rc_startup)" ] || exit 3
s=$(nvram show 2>/dev/null || true); [ -n "$s" ]
nvram set astro_prev_rc_startup="x$(nvram get rc_startup)"
r='( sleep @SECONDS@; [ -n "$(nvram get astro_prev_rc_startup)" ] || exit 0'
for k in @KEYS@; do if printf '%s\n' "$s" | grep -q "^$k="; then v=$(nvram get "$k" | sed "s/'/'\\\\''/g"); r="$r
nvram set $k='$v'"; else r="$r
nvram unset $k"; fi; done
r="$r
p=\$(nvram get astro_prev_rc_startup); nvram set rc_startup=\"\${p#x}\"; nvram unset astro_prev_rc_startup; nvram commit; reboot ) &"
nvram set rc_startup="$r"
EOF
}

# Cancel the auto-revert armed by the apply transaction and restore the startup script.
confirm_router() {
    local status=0
    router_ssh 'p=$(nvram get astro_prev_rc_startup); [ -n "$p" ] || exit 3; nvram set rc_startup="${p#x}" && nvram unset astro_prev_rc_startup && nvram commit && [ -z "$(nvram get astro_prev_rc_startup)" ]' </dev/null \
        || status=$?
    ((status != 3)) || die "The auto-revert already ran or was never armed; rc_startup left unchanged"
    ((status == 0)) || die "Could not cancel the auto-revert; the router restores its previous settings ${REVERT_SECONDS}s after boot"
    info "Verified; auto-revert cancelled."
}

show_preview() {
    cat <<EOF

Configuration preview
---------------------
Target/current host : $HOST
Router LAN address  : $ROUTER_IP / $NETMASK
Client gateway      : $LAPTOP_IP (the forwarding host)
Client LAN route    : ${ROUTER_IP%.*}.0 / $NETMASK (router UI remains directly reachable)
Client DNS          : $DNS_PRIMARY, $DNS_SECONDARY
DHCP pool           : ${ROUTER_IP%.*}.$DHCP_START - ${ROUTER_IP%.*}.$((DHCP_START + DHCP_COUNT - 1))
Reservations        : ${STATIC_LEASES:-none}
WAN / router NAT    : disabled
DHCP server         : enabled (static router LAN address)
Router name / SSID  : $ROUTER_NAME / $SSID
Time zone           : $TIME_ZONE
Detected radios     : ${RADIOS[*]}
NVRAM backup        : $([[ $BACKUP_ENCODING == none ]] && printf 'unavailable on this build' || printf '%s encoding' "$BACKUP_ENCODING")
Wi-Fi security      : WPA2-Personal / AES
Web-admin user      : $ADMIN_USER
SSH login           : $SSH_USER, generated key retained, password login enabled
Services            : DHCP/DNSMasq/NTP/SSH on; WAN admin/Telnet/UPnP/WPS/IPv6 off
Auto-revert         : ${REVERT_SECONDS}s after boot unless verified
Physical topology   : gateway host and clients connect through normal LAN ports
EOF
    # An if, not &&: a false test as the last command would end the caller under set -e.
    if [[ $ADMIN_PASSWORD == "$WIFI_PASSWORD" ]]; then printf 'Shared passwords      : yes (values hidden)\n'; fi
}

# Wait for a new boot ID so verify and confirm never run against the pre-reboot router. The
# two-minute limit keeps confirmation well inside the auto-revert.
wait_for_router() {
    local old_boot=$1 old_host=$HOST boot deadline=$((SECONDS + 120))
    HOST=$ROUTER_IP
    AUTH_MODE=key
    ssh_base_options
    info "Waiting for DD-WRT to reboot at $HOST..."
    while ((SECONDS < deadline)); do
        boot=$(router_ssh 'cat /proc/sys/kernel/random/boot_id' </dev/null 2>/dev/null) || boot=""
        if [[ -n $boot && $boot != "$old_boot" ]]; then
            info "Router is reachable after reboot."
            return
        fi
        sleep 4
    done
    HOST=$old_host
    die "Router did not reboot and return within two minutes; it restores its previous settings ${REVERT_SECONDS}s after boot. Check $ROUTER_IP manually."
}

verify_router() {
    local result lease mac ip
    result=$(router_ssh "printf 'lan='; nvram get lan_ipaddr; printf 'gateway='; nvram get lan_gateway; printf 'wan='; nvram get wan_proto; printf 'lan_proto='; nvram get lan_proto; printf 'dhcp='; nvram get dhcpd_enable; printf 'dnsmasq='; nvram get dnsmasq_enable; printf 'dnsmasq_process='; pidof dnsmasq >/dev/null 2>&1 && echo yes || echo no; printf 'route='; ip route 2>/dev/null | sed -n '/^default /{p;q}'; cat /tmp/dnsmasq.conf 2>/dev/null; for f in \$(sed -n 's/^dhcp-hostsfile=//p' /tmp/dnsmasq.conf 2>/dev/null); do cat \"\$f\"; done" </dev/null) \
        || die "Router verification failed"
    grep -qx "lan=$ROUTER_IP" <<<"$result" || die "Router LAN address verification failed"
    grep -qx "gateway=$LAPTOP_IP" <<<"$result" || die "Router gateway verification failed"
    grep -qx 'wan=disabled' <<<"$result" || die "WAN-disable verification failed"
    grep -qx 'lan_proto=dhcp' <<<"$result" || die "Router DHCP server type is not set"
    grep -qx 'dhcp=1' <<<"$result" || die "DHCP-server enable verification failed"
    grep -qx 'dnsmasq=1' <<<"$result" || die "DNSMasq enable verification failed"
    grep -qx 'dnsmasq_process=yes' <<<"$result" || die "DHCP was enabled in NVRAM, but DNSMasq did not start after reboot"
    grep -q '^dhcp-range=' <<<"$result" || die "dnsmasq.conf serves no DHCP range"
    if ! grep -q "^route=.*via $LAPTOP_IP" <<<"$result"; then
        warn "DD-WRT saved LAN gateway $LAPTOP_IP but did not expose a matching default route yet."
    fi
    # NVRAM is not what dnsmasq serves: check the generated config as well.
    grep -qx "dhcp-option=br0,3,$LAPTOP_IP" <<<"$result" \
        || warn "dnsmasq.conf lacks the tagged gateway option; fall back to dhcp-option=tag:boat,3 with set:boat on the reservations."
    for lease in $STATIC_LEASES; do
        IFS== read -r mac _ ip <<<"$lease"
        # Some builds write reservations to a dhcp-hostsfile instead of dhcp-host lines.
        grep -qiE "^(dhcp-host=)?[^=]*$mac.*$ip" <<<"$result" || die "dnsmasq serves no reservation for $lease"
    done
    info "Core NVRAM settings verified. On one client, check 'nmcli -f DHCP4 device show' reports routers = $LAPTOP_IP."
}

driver_status() {
    router_settings "$@"
    connect
    probe_router
    router_ssh 'for k in lan_ipaddr lan_netmask lan_gateway wan_proto lan_proto dhcp_start dhcp_num static_leasenum router_name wl_ssid time_zone; do printf "%s=" "$k"; nvram get "$k"; done; printf "uptime="; cut -d" " -f1 /proc/uptime' </dev/null
}

driver_plan() {
    router_settings "$@"
    validate_settings
    connect
    probe_router
    show_preview
    info "Plan only; no router settings, credentials, commits, or reboots were performed."
}

driver_apply() {
    local answer apply_script confirmation boot status=0
    driver_plan "$@"
    if [[ $BACKUP_ENCODING == none ]]; then
        confirmation=APPLY-NO-BACKUP
        warn "These changes cannot be restored from a backup on this router build."
        read -r -p "Type $confirmation to apply without a backup: " answer
    else
        confirmation=APPLY
        read -r -p "Back up NVRAM and apply this configuration? Type $confirmation to continue: " answer
    fi
    [[ $answer == "$confirmation" ]] || { info "Cancelled without router changes."; return; }
    boot=$(router_ssh 'cat /proc/sys/kernel/random/boot_id' </dev/null) && [[ -n $boot ]] \
        || die "Could not read the router boot ID; nothing was changed"
    make_backup
    apply_script=$(build_apply_script)
    info "Applying one NVRAM transaction..."
    printf '%s\n' "$apply_script" | router_ssh 'sh -s' || status=$?
    ((status != 3)) || die "An earlier apply is still unconfirmed; its auto-revert restores the previous settings ${REVERT_SECONDS}s after boot. Let it run (reboot the router if needed), then rerun."
    if ((status)); then
        [[ -n $BACKUP_FILE ]] && die "Apply failed; backup is at $BACKUP_FILE"
        die "Apply failed; no NVRAM backup was available on this router build"
    fi
    info "Settings committed. Rebooting DD-WRT; it restores the previous settings ${REVERT_SECONDS}s"
    info "after boot unless the verification below passes."
    router_ssh 'reboot' </dev/null >/dev/null 2>&1 || true
    wait_for_router "$boot"
    verify_router
    confirm_router
}

driver_verify() {
    router_settings "$@"
    connect
    verify_router
}

# decode_value FILE ENCODED: an encoded backup value as nvram set receives it (trailing newline
# dropped, as the old router-side decode did).
decode_value() {
    if [[ $1 == *.b64 ]]; then
        printf '%s' "$2" | base64 -d
    else
        [[ $2 =~ ^([0-9a-fA-F]{2})+$ ]] || return 1
        # shellcheck disable=SC2059 # the format is only \xHH escapes built from checked hex
        printf "$(sed 's/../\\x&/g' <<<"$2")"
    fi
}

# backup_value FILE KEY: the decoded value of KEY in an NVRAM backup.
backup_value() {
    local encoded
    encoded=$(awk -F'\t' -v k="$2" '$1 == k { print $2; exit }' "$1")
    [[ -n $encoded ]] && decode_value "$1" "$encoded"
}

# validate_backup FILE: refuse what make_backup never writes. Every line is KEY<TAB>ENCODED with a
# non-empty encoding (an empty NVRAM value still encodes its newline), and lan_ipaddr is an IPv4.
validate_backup() {
    local file=$1 pattern ip
    [[ -s $file ]] || die "Backup $file is empty; refusing to restore it"
    [[ $file == *.b64 ]] && pattern='^[A-Za-z0-9+/]+=*$' || pattern='^([0-9a-fA-F][0-9a-fA-F])+$'
    awk -F'\t' -v p="$pattern" 'NF != 2 || $1 !~ /^[A-Za-z0-9_.:-]+$/ || $2 !~ p { bad = 1; exit } END { exit bad }' "$file" \
        || die "Backup $file has a line that is not KEY<TAB>VALUE with an encoded value; refusing to restore it"
    ip=$(backup_value "$file" lan_ipaddr) && is_ipv4 "$ip" \
        || die "Backup $file has no valid lan_ipaddr; refusing to restore it"
}

# Restore a saved backup as one transaction under the same timed revert as apply: only keys whose
# value differs from a fresh pre-restore backup are set, the tool's key stays authorized so the
# result can be confirmed, and the revert is cancelled only once the router answers at the
# restored lan_ipaddr after a new boot.
driver_rollback() {
    local files=() selected answer boot restore_ip key encoded auth script status=0
    local -A want=()
    router_settings "$@"
    [[ $REVERT_SECONDS =~ ^[0-9]+$ ]] && ((REVERT_SECONDS >= 240)) || die "revert_seconds must be at least 240"
    initialize_state
    mapfile -t files < <(find "$KEY_DIR" -maxdepth 1 -type f \( -name 'nvram-*.b64' -o -name 'nvram-*.hex' \) -print | sort -r)
    ((${#files[@]})) || die "No NVRAM backups found in $KEY_DIR"
    printf '\nAvailable backups:\n'
    select selected in "${files[@]}" "Cancel"; do
        [[ $selected == Cancel ]] && return
        [[ -n $selected ]] && break
    done
    printf 'Selected: %s\n' "$selected"
    validate_backup "$selected"
    restore_ip=$(backup_value "$selected" lan_ipaddr)
    ensure_key; trust_host_key; authenticate
    probe_router
    [[ ${selected##*.} == "${BACKUP_ENCODING/base64/b64}" ]] \
        || die "The router now encodes backups as $BACKUP_ENCODING; it cannot be compared with $selected"
    boot=$(router_ssh 'cat /proc/sys/kernel/random/boot_id' </dev/null) && [[ -n $boot ]] \
        || die "Could not read the router boot ID; nothing was changed"
    make_backup -before-restore
    [[ $BACKUP_FILE != "$selected" ]] || die "The pre-restore backup would overwrite $selected; rerun"
    while IFS=$'\t' read -r key encoded; do
        want[$key]=$(decode_value "$selected" "$encoded") || die "Cannot decode $key in $selected"
    done < <(awk -F'\t' 'NR == FNR { now[$1] = $2; next } !($1 in now) || now[$1] != $2' "$BACKUP_FILE" "$selected")
    # The timer lives in rc_startup, so the restore never sets it or its saved copy.
    [[ -z ${want[rc_startup]+x} ]] || warn "rc_startup differs in $selected; it is left as is (the auto-revert uses it)."
    unset 'want[rc_startup]' 'want[astro_prev_rc_startup]'
    # Keep this tool's key and sshd: without them the restore could never be confirmed.
    auth=${want[sshd_authorized_keys]-$(backup_value "$BACKUP_FILE" sshd_authorized_keys || true)}
    grep -Fq -- "$PUBLIC_KEY" <<<"$auth" || want[sshd_authorized_keys]=${auth:+$auth$'\n'}$PUBLIC_KEY
    [[ ${want[sshd_enable]-1} == 1 ]] || want[sshd_enable]=1
    ((${#want[@]})) || { info "The router already matches $selected; nothing to restore."; return; }
    ((${#want[@]} <= 200)) || die "Refusing to restore more than 200 differing keys: their old values in rc_startup could overflow NVRAM."
    printf 'Keys to restore (%d): %s\n' "${#want[@]}" "$(printf '%s\n' "${!want[@]}" | sort | tr '\n' ' ')"
    printf 'Router LAN address after the restore: %s\n' "$restore_ip"
    read -r -p "Type RESTORE to restore them, reboot, and confirm within ${REVERT_SECONDS}s: " answer
    [[ $answer == RESTORE ]] || { info "Restore cancelled; the router is unchanged."; return; }
    script=''
    for key in "${!want[@]}"; do script+=$(nvset_line "$key" "${want[$key]}")$'\n'; done
    script+="nvram commit"$'\n'"sync"$'\n'
    printf 'set -e\ncommand -v nvram >/dev/null\n%s\n%s\n' "$(revert_lines "$script")" "$script" | router_ssh 'sh -s' || status=$?
    ((status != 3)) || die "An earlier apply or restore is still unconfirmed; let its auto-revert run (reboot the router if needed), then rerun."
    ((status == 0)) || die "Restore failed; the pre-restore values are in $BACKUP_FILE"
    info "Restore committed. Rebooting DD-WRT; it returns to the pre-restore values ${REVERT_SECONDS}s"
    info "after boot unless it answers at $restore_ip."
    router_ssh 'reboot' </dev/null >/dev/null 2>&1 || true
    ROUTER_IP=$restore_ip
    wait_for_router "$boot"
    [[ $(router_ssh 'nvram get lan_ipaddr' </dev/null) == "$restore_ip" ]] \
        || die "The router answered but lan_ipaddr is not $restore_ip; it reverts ${REVERT_SECONDS}s after boot"
    confirm_router
    info "Backup restored and confirmed: $selected (pre-restore values: $BACKUP_FILE)"
}

driver_key() {
    router_settings "$@"
    initialize_state
    ensure_key
    printf '\nPublic key for DD-WRT Services > Secure Shell > Authorized Keys:\n\n%s\n\n' "$PUBLIC_KEY"
    info "Private key: $IDENTITY"
    info "Keep the private key secret; only copy the public key to the router."
}
