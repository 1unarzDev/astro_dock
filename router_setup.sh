#!/usr/bin/env bash
# Deprecated alias for one release: --dry-run is ./hw.sh router plan, otherwise ./hw.sh router apply
# (--host/--identity/--state-dir pass through). Also: ./hw.sh router key|rollback|verify|status.
# With no arguments it prints usage: apply opens ssh before any prompt, so it is never the default.
(($#)) || set -- --help
args=() verb=apply
for arg in "$@"; do
    case $arg in
        --dry-run) verb=plan ;;
        -h|--help) verb=--help ;;
        *) args+=("$arg") ;;
    esac
done
echo "router_setup.sh is deprecated; use: ./hw.sh router $verb ${args[*]}" >&2
exec "$(dirname -- "${BASH_SOURCE[0]}")/hw.sh" router "$verb" "${args[@]}"
