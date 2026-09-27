#!/usr/bin/env bash
# Configure the host's existing NTP client, not clocks inside containers.
set -euo pipefail
if [[ "${1:-}" == --help ]]; then
    echo 'Usage: sudo setup-time-sync.sh [NTP_SERVER ...]'
    echo 'Uses existing chronyd (Fedora/CoreOS) or systemd-timesyncd (Ubuntu/Arch).'
    echo 'Example boat LAN: sudo setup-time-sync.sh 192.168.0.185'
    exit 0
fi
[[ $EUID == 0 ]] || { echo 'Run with sudo on the host.' >&2; exit 1; }
if [[ -e /.dockerenv || -e /run/.containerenv ]]; then
    echo 'Run this on the host: containers inherit the host clock.' >&2; exit 1
fi
(( $# )) || set -- time.cloudflare.com
for server in "$@"; do
    [[ "$server" =~ ^[a-zA-Z0-9][a-zA-Z0-9.:-]*$ ]] || {
        echo "Invalid NTP server: $server" >&2; exit 2;
    }
done
temporary="$(mktemp)"
trap 'rm -f "$temporary"' EXIT
if command -v chronyd >/dev/null && ! systemctl is-active --quiet systemd-timesyncd; then
    if [[ -f /etc/chrony/chrony.conf ]]; then
        config=/etc/chrony/chrony.conf
        service=chrony
    elif [[ -f /etc/chrony.conf ]]; then
        config=/etc/chrony.conf
        service=chronyd
    else
        echo 'Cannot locate the existing chrony configuration.' >&2; exit 1
    fi
    directory=/etc/chrony/astro-sources
    install -d -m 0755 "$directory"
    for server in "$@"; do printf 'server %s iburst\n' "$server"; done > "$temporary"
    install -m 0644 "$temporary" "$directory/boat.sources"
    line="sourcedir $directory"
    grep -Fxq "$line" "$config" || printf '\n%s\n' "$line" >> "$config"
    systemctl enable --now "$service"
    systemctl restart "$service"
    chronyc sources -v
elif systemctl list-unit-files systemd-timesyncd.service --no-legend | grep -q systemd-timesyncd; then
    # FallbackNTP is only used when no NTP servers are configured, not when
    # a configured LAN server is unreachable. Include public peers explicitly.
    printf '[Time]\nNTP=%s time.cloudflare.com time.google.com\nFallbackNTP=time.cloudflare.com\nPollIntervalMinSec=16\nPollIntervalMaxSec=64\n' "$*" > "$temporary"
    install -d -m 0755 /etc/systemd/timesyncd.conf.d
    install -m 0644 "$temporary" /etc/systemd/timesyncd.conf.d/60-astro.conf
    systemctl enable --now systemd-timesyncd
    systemctl restart systemd-timesyncd
else
    echo 'Install chrony or systemd-timesyncd for this host, then rerun.' >&2
    exit 1
fi
echo 'NTP configured; verify synchronization before ROS or package installation:'
timedatectl status
for attempt in $(seq 1 30); do
    if [[ "$(timedatectl show -p NTPSynchronized --value)" == yes ]]; then
        echo 'Host clock synchronized.'
        exit 0
    fi
    sleep 1
done
echo 'NTP is not synchronized yet. Check the server/network and retry before package installation.' >&2
exit 1
