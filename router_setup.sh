#!/usr/bin/env bash
set -Eeuo pipefail

PROGRAM=${0##*/}
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
STATE_DIR="$SCRIPT_DIR/router_setup_state"
HOST=192.168.0.1
IDENTITY=""
DRY_RUN=0
SSH_USER=root
SSH_PORT=22

ROUTER_IP=192.168.0.1
NETMASK=255.255.255.0
LAPTOP_IP=192.168.0.2
DHCP_START=100
DHCP_COUNT=100
DNS_PRIMARY=1.1.1.1
DNS_SECONDARY=8.8.8.8
ROUTER_NAME=mhseals
SSID=mhseals
ADMIN_USER=roboboat
ADMIN_PASSWORD=roboboat
WIFI_PASSWORD=roboboat

KNOWN_HOSTS=""
KEY_DIR=""
PUBLIC_KEY=""
AUTH_MODE=""
CURRENT_PASSWORD=""
TRUSTED_HOST=""
RADIOS=()
DDWRT_VERSION=""
BACKUP_FILE=""

info() { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

cleanup() {
    if [[ -n ${CURRENT_PASSWORD:-} ]]; then
        CURRENT_PASSWORD=x
        unset CURRENT_PASSWORD SSHPASS
    fi
}
trap cleanup EXIT

usage() {
    cat <<EOF
Usage: $PROGRAM [options]

Interactive DD-WRT flat-LAN setup for a laptop-provided internet gateway.

Options:
  --dry-run              Connect, inspect, and preview without changing the router
  --host ADDRESS         Current router management address (default: $HOST)
  --identity FILE        Existing SSH private key
  --state-dir DIRECTORY  Key, known-hosts, and backup directory
  --help                 Show this help

The configured clients use 192.168.0.2 as their DHCP default gateway. DD-WRT's
WAN and NAT are disabled; connect the laptop to a normal LAN port.
EOF
}

while (($#)); do
    case $1 in
        --dry-run) DRY_RUN=1 ;;
        --host) (($# >= 2)) || die "--host requires a value"; HOST=$2; shift ;;
        --identity) (($# >= 2)) || die "--identity requires a value"; IDENTITY=$2; shift ;;
        --state-dir) (($# >= 2)) || die "--state-dir requires a value"; STATE_DIR=$2; shift ;;
        --help|-h) usage; exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
    shift
done

require_local_tools() {
    local tool
    for tool in ssh ssh-keygen ssh-keyscan awk sed grep sort find; do
        command -v "$tool" >/dev/null 2>&1 || die "Required command not found: $tool"
    done
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

prompt_value() {
    local label=$1 variable=$2 current input
    current=${!variable}
    read -r -p "$label [$current]: " input
    [[ -z $input ]] || printf -v "$variable" '%s' "$input"
}

prompt_secret() {
    local label=$1 variable=$2 current input
    current=${!variable}
    read -r -s -p "$label [press Enter to keep default]: " input
    printf '\n'
    [[ -z $input ]] || printf -v "$variable" '%s' "$input"
    [[ -n ${!variable} ]] || printf -v "$variable" '%s' "$current"
}

edit_settings() {
    printf '\nDD-WRT flat-LAN settings (Enter keeps the displayed default)\n\n'
    prompt_value "Current router address" HOST
    prompt_value "Final router address" ROUTER_IP
    prompt_value "LAN netmask" NETMASK
    prompt_value "Laptop/default gateway" LAPTOP_IP
    prompt_value "DHCP starting host number" DHCP_START
    prompt_value "DHCP lease count" DHCP_COUNT
    prompt_value "Primary client DNS" DNS_PRIMARY
    prompt_value "Secondary client DNS" DNS_SECONDARY
    prompt_value "Router name" ROUTER_NAME
    prompt_value "Wi-Fi SSID" SSID
    prompt_value "Web-admin username" ADMIN_USER
    prompt_secret "Web-admin password" ADMIN_PASSWORD
    prompt_secret "Wi-Fi password" WIFI_PASSWORD
}

validate_settings() {
    local value start_ip end_host
    for value in HOST ROUTER_IP NETMASK LAPTOP_IP DNS_PRIMARY DNS_SECONDARY; do
        is_ipv4 "${!value}" || die "$value is not a valid IPv4 address: ${!value}"
    done
    netmask_to_int "$NETMASK" >/dev/null || die "NETMASK is not contiguous: $NETMASK"
    same_subnet "$ROUTER_IP" "$LAPTOP_IP" "$NETMASK" || die "Router and laptop must be on the same LAN subnet"
    [[ $DHCP_START =~ ^[0-9]+$ && $DHCP_COUNT =~ ^[0-9]+$ ]] || die "DHCP start/count must be integers"
    (( DHCP_START >= 2 && DHCP_START <= 254 && DHCP_COUNT >= 1 )) || die "Invalid DHCP range"
    end_host=$((DHCP_START + DHCP_COUNT - 1))
    (( end_host <= 254 )) || die "DHCP range ends beyond host 254"
    start_ip=${ROUTER_IP%.*}.$DHCP_START
    same_subnet "$ROUTER_IP" "$start_ip" "$NETMASK" || die "DHCP range is outside the LAN subnet"
    [[ $ROUTER_IP != "$LAPTOP_IP" ]] || die "Router and laptop addresses must differ"
    [[ -n $ROUTER_NAME && -n $SSID && -n $ADMIN_USER ]] || die "Names cannot be empty"
    (( ${#WIFI_PASSWORD} >= 8 && ${#WIFI_PASSWORD} <= 63 )) || die "WPA2 password must contain 8-63 characters"
    [[ $ROUTER_NAME != *$'\n'* && $SSID != *$'\n'* && $ADMIN_USER != *$'\n'* ]] || die "Names cannot contain newlines"
    [[ $ADMIN_PASSWORD != *$'\n'* && $WIFI_PASSWORD != *$'\n'* ]] || die "Passwords cannot contain newlines"
    if [[ $ADMIN_PASSWORD == roboboat || $WIFI_PASSWORD == roboboat ]]; then
        warn "The requested shared password 'roboboat' is weak. WAN administration remains disabled."
    fi
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
    if [[ -z $IDENTITY ]]; then
        IDENTITY="$KEY_DIR/id_router"
    fi
}

ensure_key() {
    if [[ -f $IDENTITY && -f $IDENTITY.pub ]]; then
        :
    elif [[ -f $IDENTITY ]]; then
        ssh-keygen -y -f "$IDENTITY" >"$IDENTITY.pub"
        chmod 600 -- "$IDENTITY.pub"
    else
        info "Generating a unique per-router SSH key at $IDENTITY"
        if ! ssh-keygen -q -t ed25519 -N '' -C "$ROUTER_NAME@$HOST" -f "$IDENTITY"; then
            warn "Ed25519 generation failed; falling back to RSA 3072."
            ssh-keygen -q -t rsa -b 3072 -N '' -C "$ROUTER_NAME@$HOST" -f "$IDENTITY"
        fi
    fi
    chmod 600 -- "$IDENTITY" "$IDENTITY.pub"
    PUBLIC_KEY=$(<"$IDENTITY.pub")
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
        read -r -s -p "Existing DD-WRT SSH password (leave blank for key-only setup): " CURRENT_PASSWORD
        printf '\n'
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

probe_router() {
    local probe radio
    probe=$(router_ssh 'printf "version="; version=$(nvram get os_version); [ -n "$version" ] || version=$(nvram get dist_type); [ -n "$version" ] || version=$(uname -n); printf "%s\n" "$version"; printf "build="; nvram get os_date; command -v nvram >/dev/null || exit 20; command -v base64 >/dev/null && echo base64=yes || echo base64=no; command -v setuserpasswd >/dev/null && echo setuserpasswd=yes || echo setuserpasswd=no; nvram show 2>/dev/null | sed -n "s/^\(wl[0-9][0-9]*\|ath[0-9][0-9]*\|wlan[0-9][0-9]*\)_\(ssid\|ifname\)=.*/radio=\1/p" | sort -u') \
        || die "Router inspection failed"
    DDWRT_VERSION=$(awk -F= '/^version=/{print $2; exit}' <<<"$probe")
    [[ -n $DDWRT_VERSION ]] || die "The target does not identify itself as DD-WRT"
    grep -q '^setuserpasswd=yes$' <<<"$probe" || die "This build lacks setuserpasswd; set the requested admin credentials in the Web UI, then rerun"
    grep -q '^base64=yes$' <<<"$probe" || die "This build lacks base64, which is required for a safe, restorable NVRAM backup"
    mapfile -t RADIOS < <(awk -F= '/^radio=/{print $2}' <<<"$probe" | sort -u)
    ((${#RADIOS[@]})) || die "No supported DD-WRT physical radio (wl*, ath*, or wlan*) was detected"
    for radio in "${RADIOS[@]}"; do
        [[ $radio =~ ^(wl|ath|wlan)[0-9]+$ ]] || die "Unsafe radio name returned by router: $radio"
    done
    info "Detected DD-WRT $DDWRT_VERSION with radio(s): ${RADIOS[*]}"
}

make_backup() {
    local stamp
    stamp=$(date +%Y%m%d-%H%M%S)
    BACKUP_FILE="$KEY_DIR/nvram-$stamp.b64"
    umask 077
    router_ssh 'for key in $(nvram show 2>/dev/null | sed -n "s/^\([A-Za-z0-9_.:-][A-Za-z0-9_.:-]*\)=.*/\1/p"); do encoded=$(nvram get "$key" | base64 | tr -d "\n"); printf "%s\t%s\n" "$key" "$encoded"; done' \
        >"$BACKUP_FILE" || { rm -f -- "$BACKUP_FILE"; die "NVRAM backup failed"; }
    chmod 600 -- "$BACKUP_FILE"
    [[ -s $BACKUP_FILE ]] || die "NVRAM backup was empty"
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
    local script='' radio existing_keys merged_keys dns_options
    existing_keys=$(router_ssh 'nvram get sshd_authorized_keys' 2>/dev/null || true)
    if grep -Fqx -- "$PUBLIC_KEY" <<<"$existing_keys"; then
        merged_keys=$existing_keys
    elif [[ -n $existing_keys ]]; then
        merged_keys="$existing_keys"$'\n'"$PUBLIC_KEY"
    else
        merged_keys=$PUBLIC_KEY
    fi
    dns_options="dhcp-option=3,$LAPTOP_IP"$'\n'"dhcp-option=6,$DNS_PRIMARY,$DNS_SECONDARY"

    script+="set -e"$'\n'
    script+="command -v nvram >/dev/null"$'\n'
    script+=$(nvset_line wan_proto disabled)$'\n'
    script+=$(nvset_line wk_mode router)$'\n'
    script+=$(nvset_line lan_proto static)$'\n'
    script+=$(nvset_line lan_ipaddr "$ROUTER_IP")$'\n'
    script+=$(nvset_line lan_netmask "$NETMASK")$'\n'
    script+=$(nvset_line lan_gateway "$LAPTOP_IP")$'\n'
    script+=$(nvset_line router_name "$ROUTER_NAME")$'\n'
    script+=$(nvset_line wan_hostname "$ROUTER_NAME")$'\n'
    script+=$(nvset_line dhcp_start "$DHCP_START")$'\n'
    script+=$(nvset_line dhcp_num "$DHCP_COUNT")$'\n'
    script+=$(nvset_line dhcp_lease 1440)$'\n'
    script+=$(nvset_line dhcpfwd_enable 0)$'\n'
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
    script+=$(nvset_line time_zone America/Chicago)$'\n'
    script+=$(nvset_line http_username "$ADMIN_USER")$'\n'

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
    printf '%s' "$script"
}

show_preview() {
    cat <<EOF

Configuration preview
---------------------
Target/current host : $HOST
Router LAN address  : $ROUTER_IP / $NETMASK
Client gateway      : $LAPTOP_IP (the forwarding laptop)
Client DNS          : $DNS_PRIMARY, $DNS_SECONDARY
DHCP pool           : ${ROUTER_IP%.*}.$DHCP_START - ${ROUTER_IP%.*}.$((DHCP_START + DHCP_COUNT - 1))
WAN / router NAT    : disabled
Router name / SSID  : $ROUTER_NAME / $SSID
Detected radios     : ${RADIOS[*]}
Wi-Fi security      : WPA2-Personal / AES
Web-admin user      : $ADMIN_USER
SSH login           : root, generated key retained, password login enabled
Services            : DHCP/DNSMasq/NTP/SSH on; WAN admin/Telnet/UPnP/WPS/IPv6 off
Physical topology   : laptop and clients connect through normal LAN ports
EOF
    [[ $ADMIN_PASSWORD == "$WIFI_PASSWORD" ]] && printf 'Shared passwords      : yes (values hidden)\n'
}

wait_for_router() {
    local attempt old_host=$HOST
    HOST=$ROUTER_IP
    ssh_base_options
    info "Waiting for DD-WRT to reboot at $HOST..."
    for attempt in {1..30}; do
        if ssh "${SSH_OPTIONS[@]}" -o BatchMode=yes "$SSH_USER@$HOST" true </dev/null 2>/dev/null; then
            AUTH_MODE=key
            info "Router is reachable after reboot."
            return
        fi
        sleep 4
    done
    HOST=$old_host
    die "Router did not return within two minutes. Connect to the LAN and check $ROUTER_IP manually."
}

verify_router() {
    local result
    result=$(router_ssh "printf 'lan='; nvram get lan_ipaddr; printf 'gateway='; nvram get lan_gateway; printf 'wan='; nvram get wan_proto; printf 'route='; ip route 2>/dev/null | sed -n '/^default /{p;q}'") \
        || die "Post-reboot verification failed"
    grep -qx "lan=$ROUTER_IP" <<<"$result" || die "Router LAN address verification failed"
    grep -qx "gateway=$LAPTOP_IP" <<<"$result" || die "Router gateway verification failed"
    grep -qx 'wan=disabled' <<<"$result" || die "WAN-disable verification failed"
    if ! grep -q "^route=.*via $LAPTOP_IP" <<<"$result"; then
        warn "DD-WRT saved LAN gateway $LAPTOP_IP but did not expose a matching default route yet."
    fi
    info "Core NVRAM settings verified. Check that a client receives gateway $LAPTOP_IP and has internet access."
}

configure_router() {
    local answer apply_script
    edit_settings
    validate_settings
    initialize_state
    ensure_key
    trust_host_key
    authenticate
    probe_router
    show_preview
    if ((DRY_RUN)); then
        info "Dry run complete; no router settings, credentials, commits, or reboots were performed."
        return
    fi
    read -r -p "Back up NVRAM and apply this configuration? Type APPLY to continue: " answer
    [[ $answer == APPLY ]] || { info "Cancelled without router changes."; return; }
    make_backup
    apply_script=$(build_apply_script)
    info "Applying one NVRAM transaction..."
    printf '%s\n' "$apply_script" | router_ssh 'sh -s' || die "Apply failed; backup is at $BACKUP_FILE"
    info "Settings committed. Rebooting DD-WRT..."
    router_ssh 'reboot' >/dev/null 2>&1 || true
    AUTH_MODE=key
    wait_for_router
    verify_router
}

restore_backup() {
    local files=() selected answer
    initialize_state
    ensure_key
    trust_host_key
    authenticate
    mapfile -t files < <(find "$KEY_DIR" -maxdepth 1 -type f -name 'nvram-*.b64' -print | sort -r)
    ((${#files[@]})) || die "No NVRAM backups found in $KEY_DIR"
    printf '\nAvailable backups:\n'
    select selected in "${files[@]}" "Cancel"; do
        [[ $selected == Cancel ]] && return
        [[ -n $selected ]] && break
    done
    printf 'Selected: %s\n' "$selected"
    read -r -p "Type RESTORE to replace NVRAM and reboot: " answer
    [[ $answer == RESTORE ]] || { info "Restore cancelled."; return; }
    router_ssh \
        'tab=$(printf "\t"); while IFS="$tab" read -r key encoded; do case "$key" in *[!A-Za-z0-9_.:-]*|"") exit 30;; esac; value=$(printf "%s" "$encoded" | base64 -d) || exit 31; nvram set "$key=$value"; done; nvram commit; sync' \
        <"$selected" \
        || die "Restore failed"
    router_ssh 'reboot' >/dev/null 2>&1 || true
    info "Backup restored and reboot requested."
}

export_public_key() {
    validate_settings
    initialize_state
    ensure_key
    printf '\nPublic key for DD-WRT Services > Secure Shell > Authorized Keys:\n\n%s\n\n' "$PUBLIC_KEY"
    info "Private key: $IDENTITY"
    info "Keep the private key secret; only copy the public key to the router."
}

main_menu() {
    local choice
    if ((DRY_RUN)); then
        configure_router
        return
    fi
    while true; do
        printf '\nDD-WRT Router Setup\n\n'
        printf '  1) Configure router\n  2) Preview / dry run\n  3) Export public key\n  4) Restore NVRAM backup\n  5) Quit\n\n'
        read -r -p "Choose [1-5]: " choice
        case $choice in
            1) configure_router; return ;;
            2) DRY_RUN=1; configure_router; return ;;
            3) export_public_key ;;
            4) restore_backup; return ;;
            5) return ;;
            *) warn "Choose a number from 1 through 5." ;;
        esac
    done
}

require_local_tools
main_menu
