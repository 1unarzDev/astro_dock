# shellcheck shell=bash
# The robot's computers: every ssh host of the registry (ASTRO_REGISTRY, the vessel profile's devices.yaml; the
# name is the ssh destination). apply fast-forwards astro_dock and the profile's repos to origin/main
# and builds in each host's container; --image first recreates that container on REF, after committing
# the old one as a rollback image. Logs: ~/.local/state/astro_dock/hosts/ on this computer.

driver_usage() {
    cat <<EOF
Usage: $HW_PROGRAM $HW_KIND status|verify [HOST...]
       $HW_PROGRAM $HW_KIND plan|apply [--image REF] [HOST...]
       $HW_PROGRAM $HW_KIND rollback IMAGE [HOST...]
       $HW_PROGRAM $HW_KIND keys [HOST...]
       $HW_PROGRAM $HW_KIND repos [--role ROLE...]
       $HW_PROGRAM $HW_KIND project|compose

status    Read-only: each repository's branch, head and dirty state, and the container's image.
plan      Read-only: what apply would do (heads against origin/main via ls-remote; no fetch).
apply     Back up dirty state, fast-forward astro_dock and the host's repos to origin/main, check out
          this computer's profile set at its commit (profiles/ there), prune stale symlink-install
          links, build in the container. --image recreates it on REF. A host's repos: repos.all and
          repos.ROLE of each of its registry roles.
verify    Read-only: every repository is at its origin/main, profiles/ at this computer's profile
          commit, and the container runs.
rollback  Recreate the container on IMAGE (an astro-rollback:STAMP that apply printed), then build.
keys      Create the operator key (XDG_CONFIG_HOME/$(cfg operator_key)/id_ed25519) if missing, install
          it where the TUI is refused (your own ssh may ask once) and record each host key. Idempotent.
repos     Read-only, local: "NAME URL BRANCH" per repository of this computer (from sources): repos.all
          and those of its roles (--role ROLE, else this host's registry roles, operator for a
          generic control station, else none);
          ./setup.linux.sh --components repos clones and builds them on this computer.
project   The Compose project of the dev container (compose_project).
compose   The dev container's names as Compose variables (ASTRO_CONTAINER, ASTRO_HOSTNAME, ASTRO_CONFIG_DIR)
          from container: (name, hostname, config_dir), one KEY=VALUE line each it sets; prebuild.sh
          writes them to .devcontainer/.env. Unset ones keep docker-compose.yml's generic defaults.
Without HOST: every ssh host of ${ASTRO_REGISTRY:-the vessel registry}.
EOF
}

# host_roles HOST: its registry roles (none without a registry or entry).
host_roles() {
    [[ -n $1 && -r ${ASTRO_REGISTRY:-} ]] || return 0
    python3 -I "$HW_ROOT/devices/profile.py" roles "$ASTRO_REGISTRY" "$1"
}

# role_repos ROLE...: repos.all, then each role's, without repeats.
role_repos() {
    local seen=' ' role name
    for role in all "$@"; do
        for name in $(cfg "repos.$role"); do
            [[ $seen == *" $name "* ]] || { seen+="$name "; printf '%s\n' "$name"; }
        done
    done
}

driver_project() { printf '%s\n' "$(cfg compose_project)"; }

driver_compose() {
    local pair value
    for pair in ASTRO_CONTAINER=name ASTRO_HOSTNAME=hostname ASTRO_CONFIG_DIR=config_dir; do
        value=$(cfg "container.${pair#*=}")
        [[ -n $value ]] || continue
        [[ $value =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "$HW_KIND.yaml: container.${pair#*=} '$value' is not a plain name"
        printf '%s=%s\n' "${pair%%=*}" "$value"
    done
}

driver_repos() {
    local name url branch roles=()
    while (($#)); do
        [[ $1 == --role && -n ${2:-} ]] || die "Usage: $HW_PROGRAM $HW_KIND repos [--role ROLE...]"
        roles+=("$2")
        shift 2
    done
    # This machine's resolved roles (a generic control station has none in a registry: operator).
    if ((${#roles[@]} == 0)); then
        if [[ -v HW_SELF_ROLES ]]; then read -ra roles <<<"$HW_SELF_ROLES"
        else mapfile -t roles < <(host_roles "${ASTRO_HOST:-}"); fi
    fi
    for name in $(role_repos "${roles[@]}"); do
        [[ $name =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "$HW_KIND.yaml: '$name' is not a directory name"
        url=$(cfg "sources.$name.url") branch=$(cfg "sources.$name.branch")
        [[ -n $url ]] || die "$HW_KIND.yaml: sources.$name has no url"
        printf '%s %s %s\n' "$name" "$url" "${branch:-main}"
    done
}

fleet_init() {
    # Per-host failures are logged and counted (PIPESTATUS), not fatal: one host down must not stop the rest.
    set +e
    SSH=${HW_SSH:-ssh} SSH_COPY_ID=${HW_SSH_COPY_ID:-ssh-copy-id}
    [[ -r ${ASTRO_REGISTRY:-} ]] || die "The profile names no readable registry (ASTRO_REGISTRY)"
    STAMP=$(date +%Y%m%d-%H%M%S)
    LOG=$(hw_state hosts)/${FUNCNAME[1]#driver_}-$STAMP.log
    fleet_profiles
}

# The profile set every host gets: this computer's profile root, when it is a git checkout of its own
# (PROFILES_URL its origin, PROFILES_SHOWN that without credentials, PROFILES_PIN its commit). Else the
# hosts keep theirs.
fleet_profiles() {
    local root
    PROFILES_URL='' PROFILES_SHOWN='' PROFILES_PIN=''
    root=$(cd -- "$HW_PROFILES_ROOT" && pwd -P) || return 0
    if [[ $(git -C "$root" rev-parse --show-toplevel 2>/dev/null) != "$root" ]]; then
        warn "$HW_PROFILES_ROOT is not a git checkout: the hosts keep their profiles/"
        return 0
    fi
    PROFILES_URL=$(git -C "$root" remote get-url origin 2>/dev/null) PROFILES_PIN=$(git -C "$root" rev-parse HEAD)
    [[ -n $PROFILES_URL ]] || { warn "$root has no origin: the hosts keep their profiles/"; PROFILES_PIN=''; return 0; }
    PROFILES_SHOWN=$(hw_url_shown "$PROFILES_URL")
    [[ -z $(git -C "$root" status --porcelain) ]] || warn "$root has local changes; the hosts get its commit ${PROFILES_PIN:0:7} without them"
}

# Runs on the host (after heads_script's loop): profiles/ in WORKSPACE against PROFILES_PIN.
profiles_heads() {
    [[ -n $PROFILES_PIN ]] || return 0
    cat <<EOF
p=\$(git -C $1/profiles rev-parse -q --verify HEAD 2>/dev/null)
if [ "\$p" = $PROFILES_PIN ]; then echo "profiles OK ${PROFILES_PIN:0:7}"
else echo "profiles FAIL at \${p:-none}, this computer's is ${PROFILES_PIN:0:7}: $HW_PROGRAM hosts apply"; fi
EOF
}

# Runs on the host (after fast_forward's loop): profiles/ in WORKSPACE cloned or fetched from PROFILES_URL
# and checked out at PROFILES_PIN, dirty state backed up first. The old submodule checkout of
# astro_dock, left in place when astro_dock stopped tracking it, is reused as it is.
profiles_checkout() {
    [[ -n $PROFILES_PIN ]] || return 0
    cat <<EOF
cd $1 || { echo "FAIL profiles: no $1"; exit 1; }
if [ ! -e profiles/.git ]; then
    { rmdir profiles 2>/dev/null; git clone -q --no-checkout '$PROFILES_URL' profiles; } \
        || { echo "FAIL profiles: clone $PROFILES_SHOWN"; exit 1; }
elif [ -n "\$(git -C profiles status --porcelain)" ]; then
    mkdir -p "\$backup"; git -C profiles diff HEAD > "\$backup/profiles.diff"; echo "BACKUP profiles -> \$backup"
fi
if git -C profiles remote set-url origin '$PROFILES_URL' && git -C profiles fetch -q origin \
    && git -C profiles checkout -q --detach $PROFILES_PIN; then echo "OK profiles ${PROFILES_PIN:0:7}"
else echo "FAIL profiles: ${PROFILES_PIN:0:7} not checked out (pushed to $PROFILES_SHOWN? local changes?)"; rc=1; fi
EOF
}
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

# The registry's hosts as profile.py resolves them, one tab-separated line each:
# name user address ssh(yes|no) container workspace via roles ('-' for none).
registry_hosts() { python3 -I "$HW_ROOT/devices/profile.py" hosts "$ASTRO_REGISTRY" "${ASTRO_NETWORK_ENV:-}"; }

# "name container workspace" per ssh host of the registry (the given names, else all; a name the registry
# lacks gets "- -", which for_hosts fails). A registry that names neither gets this checkout's: its dev
# container (container.name, else the Compose default astro_dev; never this machine's ASTRO_CONTAINER), and its
# path under $HOME.
fleet_hosts() {
    local own workspace=- home name ssh c w rows
    declare -A line
    own=$(cfg container.name)
    own=${own:-astro_dev}
    home=$(cd -- "$HOME" 2>/dev/null && pwd -P) || home=$HOME     # HW_ROOT is physical (/home -> /var/home)
    [[ $HW_ROOT != "$home"/* ]] || workspace="~/${HW_ROOT#"$home"/}"
    rows=$(registry_hosts) || return 1
    while IFS=$'\t' read -r name _ _ ssh c w _; do
        [[ -n $name ]] || continue
        [[ $c != - ]] || c=${own:--}
        [[ $w != - ]] || w=$workspace
        line[$name]="$name $c $w"
        (($#)) || [[ $ssh == no ]] || printf '%s\n' "${line[$name]}"
    done <<<"$rows"
    for name; do printf '%s\n' "${line[$name]:-$name - -}"; done
}

# on_hosts FUNCTION ARGS -- HOST...: run each host's generated script, logging; rc 1 if any failed.
for_hosts() {
    local fn=$1 rc=0 name container workspace
    shift
    while read -r name container workspace; do
        [[ $container != - && $workspace != - ]] || { log "FAIL $name: not a registry host, or the registry gives no container/workspace"; rc=1; continue; }
        HW_HOST=$name "$fn" "$name" "$container" "${workspace/#\~/\$HOME}" || rc=1
    done < <(fleet_hosts "$@")
    return $rc
}

# The repositories of the host for_hosts is on (HW_HOST): astro_dock, then its roles' repos.
repos() { printf 'astro_dock %s' "$(role_repos $(host_roles "${HW_HOST:-}") | tr '\n' ' ')"; }

# Runs on the host: per repository "repo branch head clean|dirty at-origin|differs|unknown" (origin's
# main from ls-remote, so nothing is fetched), then the container's image and state.
heads_script() {
    cat <<EOF
for repo in $(repos); do
    dir=$2; [ "\$repo" = astro_dock ] || dir=$2/src/\$repo
    cd "\$dir" 2>/dev/null || { echo "\$repo missing \$dir"; continue; }
    dirty=clean; [ -z "\$(git status --porcelain)" ] || dirty=dirty
    remote=\$(git ls-remote origin main 2>/dev/null | cut -f1); sync=unknown
    [ -z "\$remote" ] || { [ "\$(git rev-parse HEAD)" = "\$remote" ] && sync=at-origin || sync=differs; }
    echo "\$repo \$(git rev-parse --abbrev-ref HEAD) \$(git rev-parse --short HEAD) \$dirty \$sync"
done
$(profiles_heads "$2")
docker inspect $1 --format 'container $1 {{.Config.Image}} running={{.State.Running}}' 2>/dev/null || echo "container $1 missing"
EOF
}

# Runs on the host, in a repository: its dirty state (diff, status, untracked files) into $backup.
backup_dirty() {
    cat <<'EOF'
    if [ -n "$(git status --porcelain)" ]; then
        mkdir -p "$backup"; git diff HEAD > "$backup/$repo.diff"; git status --porcelain > "$backup/$repo.status"
        git ls-files -o --exclude-standard -z | tar --null -T - -czf "$backup/$repo.untracked.tgz"
        echo "BACKUP $repo -> $backup"
    fi
EOF
}

# Runs on the host: one line per repository, OK / BACKUP / FAIL.
fast_forward() {
    cat <<EOF
rc=0; backup=\${XDG_STATE_HOME:-\$HOME/.local/state}/astro_dock/hosts/backup-$STAMP
for repo in $(repos); do
    dir=$1; [ "\$repo" = astro_dock ] || dir=$1/src/\$repo
    cd "\$dir" 2>/dev/null || { echo "FAIL \$repo: no \$dir"; rc=1; continue; }
    branch=\$(git rev-parse --abbrev-ref HEAD); head=\$(git rev-parse --short HEAD)
    mkdir -p "\$backup"; echo "\$repo \$branch \$head" >> "\$backup/heads.txt"
$(backup_dirty)
    git fetch -q origin || { echo "FAIL \$repo: fetch"; rc=1; continue; }
    [ "\$branch" = main ] || git checkout -q main || { echo "FAIL \$repo: checkout main from \$branch"; rc=1; continue; }
    if git merge --ff-only -q origin/main; then echo "OK \$repo \$branch@\$head -> main@\$(git rev-parse --short HEAD)"
    else echo "FAIL \$repo: main has local commits; not fast-forwarded"; rc=1; continue; fi
done
$(profiles_checkout "$1")
exit \$rc
EOF
}

# Runs on the host: README "standalone deployment" path, rollback image first.
recreate() {
    cat <<EOF
set -e; cd $3
if docker inspect $2 >/dev/null 2>&1; then
    docker commit $2 astro-rollback:$STAMP >/dev/null
    echo "rollback image astro-rollback:$STAMP ($HW_PROGRAM $HW_KIND rollback astro-rollback:$STAMP $4)"
fi
case $1 in */*) docker pull -q $1 ;; esac  # registry references refresh; local rollback images do not
.devcontainer/prebuild.sh
ASTRO_IMAGE=$1 docker compose -p $(cfg compose_project) -f .devcontainer/docker-compose.yml \
    -f .devcontainer/docker-compose.override.yml up -d dev
docker inspect $2 --format '{{.Config.Image}} volumes: {{range .Mounts}}{{.Name}} {{end}}'
EOF
}

# Runs in the container: the workspace's one build (devices/build.sh: dev.bashrc, stale links pruned,
# colcon build --symlink-install, from clean when the container's image changed since the last build).
# shellcheck source=devices/build.sh
source "$HW_ROOT/devices/build.sh"
build() { hw_build_body; }

build_on() {
    log "== $1: colcon build --symlink-install"
    build | $SSH "$1" "docker exec -i -e ASTRO_BUILD_IMAGE=\$(docker inspect --format '{{.Image}}' $2) $2 bash -s" 2>&1 | tee -a "$LOG"
    if [[ ${PIPESTATUS[1]} == 0 ]]; then log "OK $1 build"; else log "FAIL $1 build"; return 1; fi
}

recreate_on() {
    log "== $1: recreate $2 on $IMAGE"
    recreate "$IMAGE" "$2" "$3" "$1" | $SSH "$1" bash -s 2>&1 | tee -a "$LOG"
    [[ ${PIPESTATUS[1]} == 0 ]] || { log "FAIL $1: recreate"; return 1; }
}

deploy_one() {
    local rc=0
    log "== $1: fast-forward $(repos)"
    fast_forward "$3" | $SSH "$1" bash -s 2>&1 | tee -a "$LOG"
    [[ ${PIPESTATUS[1]} == 0 ]] || { log "WARN $1: repositories not all at origin/main"; rc=1; }
    if [[ -n $IMAGE ]]; then recreate_on "$@" || return 1; fi
    build_on "$1" "$2" || rc=1
    return $rc
}

image_arg() { IMAGE=''; if [[ ${1:-} == --image ]]; then IMAGE=${2:?--image needs a reference}; fi; }

driver_apply() {
    local rc=0
    fleet_init
    image_arg "$@"; [[ -z $IMAGE ]] || shift 2
    for_hosts deploy_one "$@" || rc=1
    log "deploy rc=$rc log=$LOG"; return $rc
}

driver_rollback() {
    local rc=0
    IMAGE=${1:?rollback needs the IMAGE to recreate on (astro-rollback:STAMP)}
    shift
    fleet_init
    rollback_one() { recreate_on "$@" && build_on "$1" "$2"; }
    for_hosts rollback_one "$@" || rc=1
    log "rollback rc=$rc log=$LOG"; return $rc
}

heads_on() { heads_script "$2" "$3" | $SSH "$1" bash -s 2>&1 | sed "s/^/$1: /"; }

driver_status() {
    fleet_init
    for_hosts heads_on "$@"
}

driver_plan() {
    fleet_init
    image_arg "$@"; [[ -z $IMAGE ]] || shift 2
    plan_one() {
        heads_on "$@"
        echo "$1: apply would back up dirty repos, fast-forward to origin/main${PROFILES_PIN:+, check out profiles ${PROFILES_PIN:0:7}}${IMAGE:+, recreate $2 on $IMAGE} and build in $2"
    }
    for_hosts plan_one "$@"
}

driver_verify() {
    local out rc=0
    fleet_init
    verify_one() {
        out=$(heads_on "$@") || return 1
        printf '%s\n' "$out"
        ! grep -q ' missing\| running=false\| FAIL ' <<<"$out" || return 1
        ! awk '$2 != "container" && $2 != "profiles" && ($3 != "main" || $5 == "dirty" || $6 != "at-origin")' <<<"$out" | grep -q .
    }
    for_hosts verify_one "$@" || { warn "Not every host is at origin/main with this computer's profiles and its container running"; rc=1; }
    return $rc
}

# "name user@address jump|-" per ssh host the TUI reaches, never this machine (ASTRO_HOST, or its
# hostname). Addresses as profile.py resolves them (registry_hosts).
key_hosts() {
    local me name user address ssh via rows
    declare -A target
    me=$(uname -n) rows=$(registry_hosts) || return 1
    while IFS=$'\t' read -r name user address _; do
        [[ -n $name ]] || continue
        target[$name]=$address
        [[ $user == - ]] || target[$name]=$user@$address
    done <<<"$rows"
    while IFS=$'\t' read -r name _ _ ssh _ _ via _; do
        [[ -n $name && $ssh == yes && $name != "${ASTRO_HOST:-}" && $name != "${me%%.*}" ]] || continue
        [[ $via == - ]] || via=${target[$via]:-$via}
        printf '%s %s %s\n' "$name" "${target[$name]}" "$via"
    done <<<"$rows"
}

driver_keys() {
    local config=${XDG_CONFIG_HOME:-$HOME/.config} rc=0 name target jump wanted=" $* "
    local dir=$config/$(cfg operator_key)
    local key=$dir/id_ed25519
    fleet_init
    mkdir -p "$dir" && chmod 700 "$dir" || return 1
    if [[ ! -f $key ]]; then
        ssh-keygen -q -t ed25519 -N '' -C "operator@$(uname -n)" -f "$key" || return 1
        log "created $key"
    fi
    # The TUI's own options: this key, its known_hosts, no prompts.
    local tui=(-i "$key" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10
               -o "UserKnownHostsFile=$dir/known_hosts")
    while read -r name target jump; do
        [[ $wanted == "  " || $wanted == *" $name "* ]] || continue
        local via=() mine=()
        [[ $jump == - ]] || via=(-o "ProxyJump=$jump")
        [[ $jump == - ]] || mine=(-o "ProxyCommand=ssh ${tui[*]} -W %h:%p $jump")
        if $SSH "${tui[@]}" "${mine[@]}" -o StrictHostKeyChecking=yes "$target" true </dev/null; then
            log "OK $name: the TUI reaches $target"; continue
        fi
        log "== $name: installing the operator key for $target"
        # Your own ssh and known_hosts verify the host (and may ask once); then its key is recorded
        # for the TUI on the connection that just verified it.
        if ! $SSH_COPY_ID -i "$key.pub" "${via[@]}" "$target" </dev/null 2>&1 | tee -a "$LOG" \
            || ! $SSH "${tui[@]}" "${mine[@]}" -o StrictHostKeyChecking=accept-new "$target" true </dev/null; then
            log "FAIL $name: the TUI cannot reach $target with $key"; rc=1; continue
        fi
        log "OK $name: key installed, host key in $dir/known_hosts"
    done < <(key_hosts)
    log "keys rc=$rc log=$LOG"; return $rc
}
