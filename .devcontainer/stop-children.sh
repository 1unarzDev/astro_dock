#!/usr/bin/env bash
# PID 1 of the dev service. `docker stop` signals only PID 1 and then SIGKILLs everything
# else once it exits, and `docker exec` children (keepers, bag recorders) are not its
# descendants. So on TERM this forwards SIGTERM to every process of this user in its own
# mount namespace, waits for them to finish (a keeper stops its bag cleanly), and only then exits.
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

stop_children() {
    local pids deadline=$((SECONDS + wait_s))
    pids="$(children)"
    [[ -n "$pids" ]] && kill -TERM $pids 2>/dev/null
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
