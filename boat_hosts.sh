#!/usr/bin/env bash
# The one-command boat deploy, kept as an alias of ./hw.sh hosts (see ./hw.sh hosts --help):
#   deploy [--image REF] [HOST...]   ./hw.sh hosts apply [--image REF] [HOST...]
#   keys [HOST...]                   ./hw.sh hosts keys [HOST...]
# BOAT_REPOS, BOAT_DEVICES and BOAT_ENV still override the repos, the registry and the network env.
hw="$(dirname -- "${BASH_SOURCE[0]}")/hw.sh"
[[ ! -v BOAT_REPOS ]] || export HW_REPOS=$BOAT_REPOS
[[ ! -v BOAT_DEVICES ]] || export ASTRO_REGISTRY=$BOAT_DEVICES
[[ ! -v BOAT_ENV ]] || export ASTRO_NETWORK_ENV=$BOAT_ENV
case ${1:-} in
    deploy) shift; exec "$hw" hosts apply "$@" ;;
    keys) shift; exec "$hw" hosts keys "$@" ;;
    *) sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
esac
