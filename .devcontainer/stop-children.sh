#!/usr/bin/env bash
# PID 1 of the dev service. `docker stop` signals only PID 1 and then SIGKILLs everything
# else once it exits, and `docker exec` children (keepers, bag recorders) are not its
# descendants. So on TERM this forwards SIGTERM to every process of this user in its own
# mount namespace (and SIGHUP to an interactive shell, which ignores TERM), waits for them to finish (a keeper
# stops its bag cleanly), and only then exits.
# Under `pid: host` pgrep sees every process on the host, so only PIDs in this script's own
# mount namespace count: exec'd children share it, other containers and host shells do not.
# KEEPER_WAIT bounds the wait and stays under the compose stop_grace_period.
set -u

wait_s="${KEEPER_WAIT:-25}"
idle=""

children() {
    local p
    for p in $(pgrep -u "$(id -u)"); do
        [[ "$p" != "$$" && "$p" != "$BASHPID" && "$p" != "$idle" \
            && /proc/$p/ns/mnt -ef /proc/self/ns/mnt ]] && echo "$p"
    done
    return 0
}

# Of the given PIDs, hang up those on a terminal that ignore TERM (an interactive `docker exec -it ... bash`),
# as closing that terminal would, so an open shell does not hold the stop for KEEPER_WAIT. Keepers and bag
# recorders run without a terminal (docker exec -d) and handle TERM; a TUI on one handles TERM too.
hang_up() {
    local p key value ign stat fields
    for p in "$@"; do
        ign=0
        { while read -r key value; do [[ $key != SigIgn: ]] || ign=$value; done </proc/$p/status
          stat=$(</proc/$p/stat); } 2>/dev/null || continue
        read -ra fields <<<"${stat##*) }"          # state ppid pgrp session tty_nr ...
        (( (16#$ign >> 14 & 1) && fields[4] != 0 )) && kill -HUP "$p" 2>/dev/null
    done
    return 0
}

stop_children() {
    local pids deadline=$((SECONDS + wait_s))
    pids="$(children)"
    [[ -n "$pids" ]] && kill -TERM $pids 2>/dev/null && hang_up $pids
    while [[ -n "$(children)" && $SECONDS -lt $deadline ]]; do
        sleep 0.2
    done
    exit 0
}

trap stop_children TERM INT
sleep infinity &
idle=$!
# wait returns on the trapped signal; loop in case of an unrelated wakeup.
while true; do wait; done
