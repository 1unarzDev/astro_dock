#!/usr/bin/env bash
# The station's operator heartbeat (ASTRO_HEARTBEAT, the hosts profile's operator_heartbeat command), as
# the systemd --user unit of hw.sh station take runs it:
#   heartbeat.sh run    in this computer's dev container (compose project ASTRO_COMPOSE_PROJECT), named
#                       ASTRO_HOST on ROS_DOMAIN_ID (else the container's); without the container it
#                       fails, and the unit retries
#   heartbeat.sh stop   its process group in the container (docker exec forwards no signals)
set -uo pipefail
DOCKER=${HW_DOCKER:-docker}
PIDFILE=/tmp/astro-operator-heartbeat.pid

container() {
    $DOCKER ps -q --filter "label=com.docker.compose.project=$ASTRO_COMPOSE_PROJECT" \
        --filter label=com.docker.compose.service=dev 2>/dev/null | head -1
}

c=$(container)
[[ -n $c ]] || { echo "no dev container of $ASTRO_COMPOSE_PROJECT running" >&2; exit 1; }
case ${1:-} in
    run)
        # set -m: the heartbeat gets its own process group, which stop signals whole.
        env=(-e "ASTRO_HOST=$ASTRO_HOST" -e "ASTRO_HOSTNAME=$ASTRO_HOST")
        [[ -z ${ROS_DOMAIN_ID:-} ]] || env+=(-e "ROS_DOMAIN_ID=$ROS_DOMAIN_ID")
        exec $DOCKER exec "${env[@]}" "$c" bash -c \
            "set -m; $ASTRO_HEARTBEAT & echo \$! > $PIDFILE; wait \$!" ;;
    stop)
        exec $DOCKER exec "$c" sh -c "[ -s $PIDFILE ] && kill -TERM -- -\$(cat $PIDFILE); rm -f $PIDFILE; true" ;;
    *) echo "Usage: ${0##*/} run|stop" >&2; exit 2 ;;
esac
