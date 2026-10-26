# shellcheck shell=bash
# Shared by hw.sh and the device drivers: logging, env layering, profile values, ssh and state helpers.
# Functions only: sourcing it changes nothing.

info() { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

# hw_load_env FILE: KEY=value lines (quotes and CRLF stripped). A variable already set, even empty, wins.
hw_load_env() {
    local key value
    [[ -r $1 ]] || return 0
    while IFS='=' read -r key value || [[ -n $key ]]; do
        [[ $key =~ ^[A-Z][A-Z0-9_]*$ ]] && [[ ! -v $key ]] || continue
        value=${value%$'\r'}
        [[ $value =~ ^\"(.*)\"$ || $value =~ ^\'(.*)\'$ ]] && value=${BASH_REMATCH[1]}
        printf -v "$key" '%s' "$value"
    done <"$1"
}

# hw_init: layers environment > .env (secrets, per machine) > the profile chain (devices/profile.py).
# Selection: hw.sh --profile > ASTRO_PROFILE (environment, then .env) > the one profile naming a
# network env; ASTRO_PROFILE_DIR (a directory) is for tests. Sets HW_PROFILE, HW_PROFILE_CHAIN,
# HW_PROFILE_KINDS, and ASTRO_NETWORK_ENV / ASTRO_REGISTRY (absolute, or empty when no layer names one).
hw_init() {
    local profiles=$HW_ROOT/config/profiles out
    hw_load_env "${ASTRO_ENV_FILE:-$HW_ROOT/.env}"
    if [[ -n ${ASTRO_PROFILE_DIR:-} ]]; then
        HW_PROFILE=$ASTRO_PROFILE_DIR
    else
        [[ -n ${ASTRO_PROFILE:-} ]] || ASTRO_PROFILE=$(python3 -I "$HW_ROOT/devices/profile.py" default "$profiles") || exit 1
        [[ $ASTRO_PROFILE =~ ^[a-z0-9][a-z0-9_-]*$ && -d $profiles/$ASTRO_PROFILE ]] \
            || die "No profile '$ASTRO_PROFILE' in config/profiles"
        HW_PROFILE=$profiles/$ASTRO_PROFILE
    fi
    out=$(python3 -I "$HW_ROOT/devices/profile.py" meta "$HW_PROFILE") || exit 1
    eval "$out"
    [[ -z $ASTRO_NETWORK_ENV || $ASTRO_NETWORK_ENV == /* ]] || ASTRO_NETWORK_ENV=$HW_ROOT/$ASTRO_NETWORK_ENV
    [[ -z $ASTRO_REGISTRY || $ASTRO_REGISTRY == /* ]] || ASTRO_REGISTRY=$HW_ROOT/$ASTRO_REGISTRY
}

# hw_load_kind KIND: the merged KIND.yaml of the profile chain as HW_* variables; set HW_* win.
hw_load_kind() {
    local out
    [[ " $HW_PROFILE_KINDS " == *" $1 "* ]] || die "Profile $(basename -- "$HW_PROFILE") has no $1.yaml"
    out=$(python3 -I "$HW_ROOT/devices/profile.py" kind "$HW_PROFILE" "$1" "$ASTRO_NETWORK_ENV") || exit 1
    eval "$out"
}

# cfg a.b.c: a profile value (HW_A_B_C). cfg_keys a.b: the keys of a mapping. secret a.b: the value of
# the .env key the profile names at a.b (credentials stay out of the repo; the profile names them).
hw_var() { local v=${1^^}; v=${v//[^A-Z0-9]/_}; printf 'HW_%s' "$v"; }
cfg() { local n; n=$(hw_var "$1"); printf '%s' "${!n:-}"; }
cfg_keys() { local n; n=$(hw_var "$1")__KEYS; printf '%s' "${!n:-}"; }
secret() {
    local key
    key=$(cfg "$1")
    [[ -z $key ]] && return 0
    [[ $key =~ ^[A-Z][A-Z0-9_]*$ ]] || die "$1 must name an .env key, not '$key'"
    printf '%s' "${!key:-}"
}

# hw_with_password PASSWORD CMD...: run an ssh command that may ask for PASSWORD (sshpass when
# installed, else SSH_ASKPASS). The password travels in the environment, never on a command line.
hw_with_password() {
    local password=$1 askpass rc=0
    shift
    [[ -n $password ]] || { "$@"; return; }
    if command -v sshpass >/dev/null; then
        SSHPASS=$password sshpass -e "$@"
        return
    fi
    askpass=$(mktemp) && printf '#!/bin/sh\nprintf "%%s\\n" "$HW_ASKPASS"\n' >"$askpass" && chmod 700 "$askpass"
    HW_ASKPASS=$password SSH_ASKPASS=$askpass SSH_ASKPASS_REQUIRE=force "$@" || rc=$?
    rm -f -- "$askpass"
    return "$rc"
}

# hw_state NAME: a private state directory under XDG_STATE_HOME/astro_dock.
hw_state() {
    local dir=${XDG_STATE_HOME:-$HOME/.local/state}/astro_dock/$1
    mkdir -p -m 700 "$dir" && printf '%s\n' "$dir"
}
