#!/usr/bin/env bash
# Robot device tooling: ./hw.sh KIND VERB [ARGS]. KIND is a YAML file of the active profile
# (config/profiles/<profile>/KIND.yaml) whose `driver:` names devices/<driver>.sh.
# Every driver implements status, plan, apply, verify and rollback; status and plan change nothing.
set -Eeuo pipefail

HW_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
HW_PROGRAM=${HW_PROGRAM:-${0##*/}}
# shellcheck source=devices/lib.sh
source "$HW_ROOT/devices/lib.sh"
HW_REQUIRED=(status plan apply verify rollback)

# hw_driver NAME: source devices/NAME.sh and refuse it unless it implements the whole contract.
hw_driver() {
    local verb missing=()
    HW_DRIVER_FILE=${HW_DRIVERS:-$HW_ROOT/devices}/$1.sh
    [[ $1 =~ ^[a-z0-9_]+/[a-z0-9_]+$ && -r $HW_DRIVER_FILE ]] || die "No driver '$1' ($HW_DRIVER_FILE)"
    # shellcheck source=/dev/null
    source "$HW_DRIVER_FILE"
    for verb in "${HW_REQUIRED[@]}"; do
        declare -F "driver_$verb" >/dev/null || missing+=("driver_$verb")
    done
    ((${#missing[@]} == 0)) || die "Driver $1 breaks the device contract: missing ${missing[*]}"
}

verbs() { declare -F | sed -n 's/^declare -f driver_//p' | grep -vx usage | tr '\n' ' '; }

usage() {
    local file
    printf 'Usage: %s KIND VERB [ARGS]   (KIND VERB --help for details)\n' "$HW_PROGRAM"
    printf '       %s env [KEY]          the network env path, or one layered value\n\n' "$HW_PROGRAM"
    printf 'Kinds in profile %s:\n' "$(basename -- "$HW_PROFILE")"
    for file in "$HW_PROFILE"/*.yaml; do
        [[ -e $file ]] || continue
        printf '  %-8s %s\n' "$(basename -- "$file" .yaml)" "$(sed -n 's/^driver: *\([^ #]*\).*/\1/p' "$file")"
    done
}

hw_init
case ${1:-} in
    ''|-h|--help) usage; exit 0 ;;
    env)
        if [[ -z ${2:-} ]]; then printf '%s\n' "$ASTRO_NETWORK_ENV"; exit 0; fi
        [[ $2 =~ ^[A-Z][A-Z0-9_]*$ ]] || die "Not an env key: $2"
        hw_load_env "$ASTRO_NETWORK_ENV"
        printf '%s\n' "${!2:-}"
        exit 0
        ;;
esac
[[ $1 =~ ^[a-z0-9_]+$ ]] || die "Not a device kind: $1"
HW_KIND=$1
hw_load_kind "$HW_KIND"
hw_driver "$(cfg driver)"
verb=${2:-}
if [[ -z $verb || $verb == -h || $verb == --help ]]; then
    if declare -F driver_usage >/dev/null; then driver_usage; else printf 'Verbs: %s\n' "$(verbs)"; fi
    exit 0
fi
[[ $verb =~ ^[a-z_]+$ && $verb != usage ]] && declare -F "driver_$verb" >/dev/null \
    || die "$HW_KIND has no verb '$verb' (verbs: $(verbs))"
"driver_$verb" "${@:3}"
