# shellcheck shell=bash
# Shared by hw.sh and the device drivers: logging, env layering, profile values, ssh and state helpers.
# Functions only: sourcing it changes nothing.

info() { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

# hw_load_env FILE [KEY...]: KEY=value lines (quotes and CRLF stripped), only the given keys when named.
# A variable already set, even empty, wins.
hw_load_env() {
    local key value keys=" ${*:2} "
    [[ -r $1 ]] || return 0
    while IFS='=' read -r key value || [[ -n $key ]]; do
        [[ $key =~ ^[A-Z][A-Z0-9_]*$ ]] && [[ ! -v $key ]] && [[ $# == 1 || $keys == *" $key "* ]] || continue
        value=${value%$'\r'}
        [[ $value =~ ^\"(.*)\"$ || $value =~ ^\'(.*)\'$ ]] && value=${BASH_REMATCH[1]}
        printf -v "$key" '%s' "$value"
    done <"$1"
}

# hw_secrets: the secrets file (ASTRO_SECRETS, default ~/.config/astro/secrets.env), refused unless
# private (mode 600 or 400) and outside every git work tree (a symlink is followed: one commit or
# `git add -f` away from a remote), then a legacy astro_dock/.env (ASTRO_ENV_FILE) with a warning, for one release.
hw_secrets() {
    local legacy=${ASTRO_ENV_FILE:-$HW_ROOT/.env} mode real
    HW_SECRETS=${ASTRO_SECRETS:-${XDG_CONFIG_HOME:-$HOME/.config}/astro/secrets.env}
    if [[ -e $HW_SECRETS ]]; then
        mode=$(stat -L -c %a -- "$HW_SECRETS") || die "Cannot stat $HW_SECRETS"
        [[ $mode == [46]00 ]] || die "$HW_SECRETS is mode $mode; secrets must be private: chmod 600 $HW_SECRETS"
        real=$(realpath -e -- "$HW_SECRETS") || die "Cannot resolve $HW_SECRETS"
        if [[ $(git -C "$(dirname -- "$real")" rev-parse --is-inside-work-tree 2>/dev/null) == true ]]; then
            die "$HW_SECRETS resolves to $real, inside a git work tree; move it to ~/.config/astro/secrets.env (mode 600)"
        fi
        hw_load_env "$HW_SECRETS"
    fi
    if [[ -f $legacy && -s $legacy ]]; then
        warn "Reading the legacy $legacy; move its keys to $HW_SECRETS (mode 600) and remove it"
        hw_load_env "$legacy"
    fi
}

# The profile set is the user's, never astro_dock's. HW_PROFILES_CONFIG (ASTRO_PROFILES_CONFIG, default
# ~/.config/astro/profiles.env; set variables win) names it: ASTRO_PROFILES, a directory you keep up to
# date, or ASTRO_PROFILES_URL, a git repository hw.sh checks out at HW_ROOT/profiles (gitignored) at
# ASTRO_PROFILES_REF (a commit, tag or branch; default main). Neither: generic mode. Only those three keys
# load: the file is not private, unlike the secrets file.
hw_profiles_config() {
    HW_PROFILES_CONFIG=${ASTRO_PROFILES_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/astro/profiles.env}
    hw_load_env "$HW_PROFILES_CONFIG" ASTRO_PROFILES ASTRO_PROFILES_URL ASTRO_PROFILES_REF
}

# hw_url_shown URL: URL without its user[:token]@, for messages.
hw_url_shown() {
    if [[ $1 =~ ^([A-Za-z][A-Za-z0-9+.-]*://)[^/@]*@(.*)$ ]]; then
        printf '%s%s' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
    else printf '%s' "$1"; fi
}

# hw_profiles_use URL [REF]: record the source in HW_PROFILES_CONFIG, keeping its other lines.
hw_profiles_use() {
    local tmp
    [[ -n $1 && $1 != -* ]] || die "Usage: $HW_PROGRAM profile sync [URL [REF]]"
    mkdir -p -- "$(dirname -- "$HW_PROFILES_CONFIG")" && tmp=$(mktemp "$HW_PROFILES_CONFIG.XXXXXX") || exit 1
    { grep -v '^ASTRO_PROFILES_\(URL\|REF\)=' "$HW_PROFILES_CONFIG" 2>/dev/null || :
      printf 'ASTRO_PROFILES_URL=%s\n' "$1"; [[ -z ${2:-} ]] || printf 'ASTRO_PROFILES_REF=%s\n' "$2"; } >"$tmp"
    chmod 644 -- "$tmp" && mv -- "$tmp" "$HW_PROFILES_CONFIG" || exit 1
    ASTRO_PROFILES_URL=$1 ASTRO_PROFILES_REF=${2:-}
}

# hw_profiles_commit: the commit ASTRO_PROFILES_REF names in HW_ROOT/profiles (a branch as last fetched).
hw_profiles_commit() {
    local ref=${ASTRO_PROFILES_REF:-main}
    git -C "$HW_ROOT/profiles" rev-parse -q --verify "refs/remotes/origin/$ref^{commit}" 2>/dev/null \
        || git -C "$HW_ROOT/profiles" rev-parse -q --verify "$ref^{commit}" 2>/dev/null
}

# hw_profiles_sync: HW_ROOT/profiles cloned (or fetched) from ASTRO_PROFILES_URL and checked out at
# ASTRO_PROFILES_REF; refused over local changes, which belong in the profile repository.
hw_profiles_sync() {
    local dir=$HW_ROOT/profiles commit
    [[ -z ${ASTRO_PROFILES:-} ]] || die "ASTRO_PROFILES=$ASTRO_PROFILES is your own directory: update it there"
    [[ -n ${ASTRO_PROFILES_URL:-} ]] \
        || die "No profile source: $HW_PROGRAM profile sync URL [REF] records one in $HW_PROFILES_CONFIG"
    if [[ -e $dir/.git ]]; then
        [[ -z $(git -C "$dir" status --porcelain) ]] \
            || die "$dir has local changes: push them to the profile repository or discard them, then sync"
        git -C "$dir" remote set-url origin "$ASTRO_PROFILES_URL" && git -C "$dir" fetch -q --tags origin \
            || die "Cannot fetch $(hw_url_shown "$ASTRO_PROFILES_URL") into $dir"
    else
        [[ ! -e $dir ]] || rmdir -- "$dir" 2>/dev/null || die "$dir is not a git checkout; move it away to sync"
        git clone -q --no-checkout -- "$ASTRO_PROFILES_URL" "$dir" || die "Cannot clone $(hw_url_shown "$ASTRO_PROFILES_URL")"
    fi
    commit=$(hw_profiles_commit) || die "$(hw_url_shown "$ASTRO_PROFILES_URL") has no ${ASTRO_PROFILES_REF:-main}"
    git -C "$dir" checkout -q --detach "$commit" || die "Cannot check out $commit in $dir"
    info "profiles $dir: $(hw_url_shown "$ASTRO_PROFILES_URL") at ${ASTRO_PROFILES_REF:-main} (${commit:0:7})" >&2
}

# hw_profiles_root: ASTRO_PROFILES > HW_ROOT/profiles once it holds a profile > empty (generic mode).
hw_profiles_root() {
    if [[ -n ${ASTRO_PROFILES:-} ]]; then
        [[ -d $ASTRO_PROFILES ]] || die "ASTRO_PROFILES=$ASTRO_PROFILES is not a directory"
        printf '%s\n' "$ASTRO_PROFILES"
    elif compgen -G "$HW_ROOT/profiles/*/profile.yaml" >/dev/null; then
        printf '%s\n' "$HW_ROOT/profiles"
    fi
    return 0
}

# hw_init: layers environment > secrets file > the profile chain (devices/profile.py). Every profile of
# the root loads and is checked; resolution picks this machine's host (--host/ASTRO_HOST, else hostname or
# a permanent MAC in a registry's STATIC_LEASES) and vessel (--vessel/ASTRO_VESSEL, else the host's). A host
# with the operator role, an unlisted --host NAME, or an unlisted machine with an address on exactly one
# vessel's LAN_SUBNET (BOAT_SUBNET, its old name) is a control station (HW_SELF_MEMBER, the fleet's station member; HW_SELF_ROLES);
# a boat computer never is.
# ASTRO_PROFILE is the vessel's old name, for one release. ASTRO_PROFILE_DIR (a directory) is for tests.
# Sets HW_PROFILE (the vessel's directory, else the fleet's), HW_PROFILE_CHAIN, HW_PROFILE_KINDS,
# HW_SCOPE_ERROR (non-empty without a vessel), ASTRO_HOST, ASTRO_VESSEL, ASTRO_NETWORK_ENV and
# ASTRO_REGISTRY (absolute, or empty). Without a profile root (generic mode) HW_PROFILES_ROOT is empty.
hw_init() {
    local out
    hw_secrets
    hw_profiles_config
    HW_PROFILES_ROOT='' HW_SCOPE_ERROR='' HW_PROFILE_KINDS='' HW_PROFILE_CHAIN=''
    if [[ -n ${ASTRO_PROFILE:-} && -z ${ASTRO_VESSEL:-} ]]; then
        warn "ASTRO_PROFILE/--profile is now ASTRO_VESSEL/--vessel"
        ASTRO_VESSEL=$ASTRO_PROFILE
    fi
    export ASTRO_HOST=${ASTRO_HOST:-} ASTRO_VESSEL=${ASTRO_VESSEL:-}
    if [[ -n ${ASTRO_PROFILE_DIR:-} ]]; then
        HW_PROFILE=$ASTRO_PROFILE_DIR HW_PROFILES_ROOT=$(dirname -- "$ASTRO_PROFILE_DIR")
        out=$(python3 -I "$HW_ROOT/devices/profile.py" meta "$HW_PROFILE") || exit 1
        eval "$out"
        return
    fi
    # A configured source is checked out on first use; moving it to a new ref is `profile sync`.
    if [[ -z ${ASTRO_PROFILES:-} && -n ${ASTRO_PROFILES_URL:-} ]]; then
        if ! compgen -G "$HW_ROOT/profiles/*/profile.yaml" >/dev/null; then
            hw_profiles_sync
        elif [[ $(hw_profiles_commit) != "$(git -C "$HW_ROOT/profiles" rev-parse -q --verify HEAD 2>/dev/null)" ]]; then
            warn "profiles/ is not at ASTRO_PROFILES_REF=${ASTRO_PROFILES_REF:-main} ($HW_PROFILES_CONFIG): run $HW_PROGRAM profile sync"
        fi
    fi
    HW_PROFILES_ROOT=$(hw_profiles_root)
    if [[ -z $HW_PROFILES_ROOT ]]; then
        HW_SCOPE_ERROR="No profiles (generic mode): $HW_PROGRAM profile sync URL [REF], or ASTRO_PROFILES=DIR"
        ASTRO_NETWORK_ENV=${ASTRO_NETWORK_ENV:-} ASTRO_REGISTRY=${ASTRO_REGISTRY:-}
    else
        out=$(python3 -I "$HW_ROOT/devices/profile.py" resolve "$HW_PROFILES_ROOT") || exit 1
        eval "$out"
        export ASTRO_HOST ASTRO_VESSEL
    fi
    # An empty ASTRO_NETWORK_ENV would read as "resolved, no network" to the consumers: never exported.
    [[ -n $ASTRO_NETWORK_ENV ]] || export -n ASTRO_NETWORK_ENV
}

# hw_load_kind KIND: the merged KIND.yaml of the profile chain as HW_* variables; set HW_* win.
hw_load_kind() {
    local out
    [[ -n $HW_PROFILE_CHAIN ]] || die "$HW_SCOPE_ERROR"
    [[ " $HW_PROFILE_KINDS " == *" $1 "* ]] || die "Profile $(basename -- "$HW_PROFILE") has no $1.yaml"
    out=$(python3 -I "$HW_ROOT/devices/profile.py" kind "$HW_PROFILE" "$1" "$ASTRO_NETWORK_ENV") || exit 1
    eval "$out"
}

# cfg a.b.c: a profile value (HW_A_B_C). cfg_keys a.b: the keys of a mapping. secret a.b: the value of
# the secrets key the profile names at a.b (credentials stay out of every repo; the profile names them).
hw_var() { local v=${1^^}; v=${v//[^A-Z0-9]/_}; printf 'HW_%s' "$v"; }
cfg() { local n; n=$(hw_var "$1"); printf '%s' "${!n:-}"; }
cfg_keys() { local n; n=$(hw_var "$1")__KEYS; printf '%s' "${!n:-}"; }
secret() {
    local key
    key=$(cfg "$1")
    [[ -z $key ]] && return 0
    [[ $key =~ ^[A-Z][A-Z0-9_]*$ ]] || die "$1 must name a secrets key, not '$key'"
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
