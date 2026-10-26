#!/usr/bin/env bash
# Robot device tooling: ./hw.sh [--profile NAME] KIND VERB [ARGS]. KIND is a YAML file of the active
# profile chain (config/profiles/<profile>/KIND.yaml, merged over its parents) whose `driver:` names
# devices/<driver>.sh.
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
    local kind
    printf 'Usage: %s [--profile NAME] KIND VERB [ARGS]   (KIND VERB --help for details)\n' "$HW_PROGRAM"
    printf '       %s env [KEY]            the network env path, or one of its keys (layered)\n' "$HW_PROGRAM"
    printf '       %s profile show [NAME]  the merged profile (no secrets)\n\n' "$HW_PROGRAM"
    printf 'Kinds in profile %s:\n' "${HW_PROFILE_CHAIN// / -> }"
    for kind in $HW_PROFILE_KINDS; do
        (hw_load_kind "$kind" 2>/dev/null; printf '  %-8s %s\n' "$kind" "$(cfg driver)")
    done
}

# --profile NAME picks the profile over ASTRO_PROFILE and .env.
if [[ ${1:-} == --profile ]]; then
    [[ -n ${2:-} ]] || die "--profile requires a name"
    ASTRO_PROFILE=$2
    unset ASTRO_PROFILE_DIR
    shift 2
fi
[[ ${1:-} != profile || -z ${3:-} ]] || { ASTRO_PROFILE=$3; unset ASTRO_PROFILE_DIR; }
hw_init
case ${1:-} in
    ''|-h|--help) usage; exit 0 ;;
    profile)
        [[ ${2:-} == show ]] || die "Usage: $HW_PROGRAM profile show [NAME]"
        exec python3 -I "$HW_ROOT/devices/profile.py" show "$HW_PROFILE" "$ASTRO_NETWORK_ENV"
        ;;
    env)
        [[ -n $ASTRO_NETWORK_ENV ]] || die "Profile ${HW_PROFILE_CHAIN// / -> } names no network_env"
        if [[ -z ${2:-} ]]; then printf '%s\n' "$ASTRO_NETWORK_ENV"; exit 0; fi
        [[ $2 =~ ^[A-Z][A-Z0-9_]*$ ]] || die "Not an env key: $2"
        # Network values only: .env secrets and other variables are never printed.
        grep -q "^$2=" "$ASTRO_NETWORK_ENV" || die "$2 is not a key of $ASTRO_NETWORK_ENV"
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
