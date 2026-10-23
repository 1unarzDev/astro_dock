#!/usr/bin/env bash
# PID 1 of the dev service. `docker stop` signals only PID 1 and then SIGKILLs everything
# else once it exits, and `docker exec` children (the keepers, the bag) are not its
# descendants. So on TERM this forwards SIGTERM to the keepers, waits for them to finish
# (they stop the bag cleanly), and only then exits.
#
# Keepers are found by command line: KEEPER_PATTERN is a pgrep -f regex, run as this user.
# The default must match how the mhseals_testing harness starts its keeper (ts015).
# Under `pid: host` pgrep sees every process on the host, so only PIDs in this script's own
# mount namespace count: exec'd children share it, other containers and host shells do not.
# KEEPER_WAIT bounds the wait and stays under the compose stop_grace_period.
set -u

pattern="${KEEPER_PATTERN:-mhseals.*harness\.keeper}"
wait_s="${KEEPER_WAIT:-25}"

keepers() {
    local p
    for p in $(pgrep -u "$(id -u)" -f -- "$pattern"); do
        [[ "$p" != "$$" && /proc/$p/ns/mnt -ef /proc/self/ns/mnt ]] && echo "$p"
    done
    return 0
}

stop_keepers() {
    local pids deadline=$((SECONDS + wait_s))
    pids="$(keepers)"
    [[ -n "$pids" ]] && kill -TERM $pids 2>/dev/null
    while [[ -n "$(keepers)" && $SECONDS -lt $deadline ]]; do
        sleep 0.2
    done
    exit 0
}

trap stop_keepers TERM INT
sleep infinity &
# wait returns on the trapped signal; loop in case of an unrelated wakeup.
while true; do wait; done
