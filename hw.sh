#!/usr/bin/env bash
# Robot device tooling: ./hw.sh [--vessel V] [--host H] KIND VERB [ARGS]. KIND is a YAML file of the
# vessel's profile chain (PROFILES/<vessel>/KIND.yaml, merged over its parents) whose `driver:` names
# devices/<driver>.sh. PROFILES is ASTRO_PROFILES, else the profiles/ submodule; without one, generic mode.
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
    printf 'Usage: %s [--vessel V] [--host H] KIND VERB [ARGS]   (KIND VERB --help for details)\n' "$HW_PROGRAM"
    printf '       %s whoami               this machine'"'"'s host and vessel, and how they were found\n' "$HW_PROGRAM"
    printf '       %s env [KEY]            the vessel'"'"'s network env path, or one of its keys\n' "$HW_PROGRAM"
    printf '       %s profile show [NAME]  a merged profile (no secrets)\n' "$HW_PROGRAM"
    printf '       %s secrets check        the secrets file and the missing key names (never values)\n\n' "$HW_PROGRAM"
    printf 'Without a match, --vessel V (or ASTRO_VESSEL) picks the vessel; --host H (or ASTRO_HOST) the host.\n'
    if [[ -z $HW_PROFILES_ROOT ]]; then
        printf 'Generic mode: no profile root (ASTRO_PROFILES or the profiles/ submodule), so no device kinds.\n'
        return
    fi
    printf 'Kinds in profile %s%s:\n' "${HW_PROFILE_CHAIN// / -> }" \
        "${HW_SCOPE_ERROR:+ (the fleet; no vessel, so only hosts repos runs)}"
    for kind in $HW_PROFILE_KINDS; do
        (hw_load_kind "$kind" 2>/dev/null; printf '  %-8s %s\n' "$kind" "$(cfg driver)")
    done
}

whoami_report() {
    if [[ -z $HW_PROFILES_ROOT ]]; then
        printf 'profiles  none: generic mode (set ASTRO_PROFILES or run git submodule update --init profiles)\n'
        return
    fi
    printf 'profiles  %s\n' "$HW_PROFILES_ROOT"
    printf 'fleet     %s; members %s\n' "$HW_FLEET" "$HW_MEMBERS"
    printf 'host      %s (%s)\n' "${ASTRO_HOST:-none}" "$HW_SELF_HOW"
    [[ -z $HW_SELF_MEMBER ]] || printf 'station   %s\n' "$HW_SELF_MEMBER"
    if [[ -n $ASTRO_VESSEL ]]; then
        printf 'vessel    %s (%s)\n' "$ASTRO_VESSEL" "$HW_SCOPE_HOW"
        printf 'network   %s\n' "${ASTRO_NETWORK_ENV:-none}"
        printf 'registry  %s\n' "${ASTRO_REGISTRY:-none}"
    else
        printf 'vessel    none: pass --vessel V (device verbs and env need one)\n'
    fi
}

# secrets check: the fleet's secrets.keys (KEY, or KEY? when optional) against the environment and the
# secrets file. Names only; fails when a required key is missing.
secrets_check() {
    local keys key missing=() optional=() rc=0
    printf 'secrets   %s (%s)\n' "$HW_SECRETS" "$( [[ -e $HW_SECRETS ]] && stat -L -c 'mode %a' -- "$HW_SECRETS" || echo missing)"
    keys=${HW_PROFILES_ROOT:+$HW_PROFILES_ROOT/${HW_FLEET:-}/secrets.keys}
    [[ -n $keys && -r $keys ]] || { printf 'keys      none listed (no secrets.keys in the fleet profile)\n'; return 0; }
    while read -r key _; do
        [[ $key =~ ^([A-Z][A-Z0-9_]*)(\??)$ ]] || continue
        [[ -n ${!BASH_REMATCH[1]:-} ]] && continue
        if [[ -n ${BASH_REMATCH[2]} ]]; then optional+=("${BASH_REMATCH[1]}"); else missing+=("${BASH_REMATCH[1]}"); fi
    done <"$keys"
    printf 'missing   %s\n' "${missing[*]:-none}"
    printf 'optional  %s (unset)\n' "${optional[*]:-none}"
    ((${#missing[@]} == 0)) || rc=1
    return "$rc"
}

need_scope() { [[ -z $HW_SCOPE_ERROR ]] || die "$HW_SCOPE_ERROR"; }

# Global flags: --vessel V, --host H (--profile V is --vessel's old name, for one release).
while (($#)); do
    case $1 in
        --vessel|--profile) [[ -n ${2:-} ]] || die "$1 requires a name"
            [[ $1 == --vessel ]] || warn "--profile is now --vessel"
            ASTRO_VESSEL=$2; unset ASTRO_PROFILE ASTRO_PROFILE_DIR; shift 2 ;;
        --host) [[ -n ${2:-} ]] || die "--host requires a name"; ASTRO_HOST=$2; shift 2 ;;
        *) break ;;
    esac
done
hw_init
case ${1:-} in
    ''|-h|--help) usage; exit 0 ;;
    whoami)
        if [[ ${2:-} == --root ]]; then printf '%s\n' "$HW_PROFILES_ROOT"; else whoami_report; fi
        exit 0
        ;;
    secrets)
        [[ ${2:-} == check ]] || die "Usage: $HW_PROGRAM secrets check"
        secrets_check
        exit
        ;;
    profile)
        [[ ${2:-} == show ]] || die "Usage: $HW_PROGRAM profile show [NAME]"
        [[ -n $HW_PROFILES_ROOT ]] || die "$HW_SCOPE_ERROR"
        dir=$HW_PROFILE
        if [[ -n ${3:-} ]]; then
            [[ $3 =~ ^[a-z0-9][a-z0-9_-]*$ && -f $HW_PROFILES_ROOT/$3/profile.yaml ]] \
                || die "No profile '$3' in $HW_PROFILES_ROOT"
            dir=$HW_PROFILES_ROOT/$3
        fi
        exec python3 -I "$HW_ROOT/devices/profile.py" show "$dir"
        ;;
    env)
        need_scope
        [[ -n $ASTRO_NETWORK_ENV ]] || die "Profile ${HW_PROFILE_CHAIN// / -> } names no network_env"
        [[ -r $ASTRO_NETWORK_ENV ]] || die "The network env $ASTRO_NETWORK_ENV is missing"
        if [[ -z ${2:-} ]]; then printf '%s\n' "$ASTRO_NETWORK_ENV"; exit 0; fi
        [[ $2 =~ ^[A-Z][A-Z0-9_]*$ ]] || die "Not an env key: $2"
        # Network values only: secrets and other variables are never printed.
        grep -q "^$2=" "$ASTRO_NETWORK_ENV" || die "$2 is not a key of $ASTRO_NETWORK_ENV"
        hw_load_env "$ASTRO_NETWORK_ENV"
        printf '%s\n' "${!2:-}"
        exit 0
        ;;
esac
[[ $1 =~ ^[a-z0-9_]+$ ]] || die "Not a device kind: $1"
HW_KIND=$1
# Without a vessel only the fleet-wide `hosts repos` (the repositories per role) runs.
[[ "$HW_KIND ${2:-}" == "hosts repos" ]] || need_scope
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
