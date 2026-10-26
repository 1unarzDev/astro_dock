# shellcheck shell=bash
# The robot's computers: every ssh host of the registry (ASTRO_REGISTRY, e.g. config/devices.yaml; the
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
       $HW_PROGRAM $HW_KIND project

status    Read-only: each repository's branch, head and dirty state, and the container's image.
plan      Read-only: what apply would do (heads against origin/main via ls-remote; no fetch).
apply     Back up dirty state, fast-forward astro_dock (and init its profiles submodule) and the host's
          repos to origin/main, prune stale symlink-install links, build in the container. --image
          recreates it on REF. A host's repos: repos.all and repos.ROLE of each of its registry roles.
verify    Read-only: every repository is at its origin/main and the container runs.
rollback  Recreate the container on IMAGE (an astro-rollback:STAMP that apply printed), then build.
keys      Create the operator key (XDG_CONFIG_HOME/$(cfg operator_key)/id_ed25519) if missing, install
          it where the TUI is refused (your own ssh may ask once) and record each host key. Idempotent.
repos     Read-only, local: "NAME URL BRANCH" per repository of this computer (from sources): repos.all
          and those of its roles (--role ROLE, else this host's registry roles, else none);
          ./setup.linux.sh --components repos clones and builds them on this computer.
project   The Compose project of the dev container (compose_project).
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

driver_repos() {
    local name url branch roles=()
    while (($#)); do
        [[ $1 == --role && -n ${2:-} ]] || die "Usage: $HW_PROGRAM $HW_KIND repos [--role ROLE...]"
        roles+=("$2")
        shift 2
    done
    ((${#roles[@]})) || mapfile -t roles < <(host_roles "${ASTRO_HOST:-}")
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
}
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

# "name container workspace" per ssh host of the registry (the given names, else all).
fleet_hosts() {
    python3 -I - "$ASTRO_REGISTRY" "$@" <<'PY'
import sys, yaml
data = yaml.safe_load(open(sys.argv[1])) or {}
defaults, hosts, wanted = data.get('defaults') or {}, data.get('hosts') or {}, sys.argv[2:]
for name in wanted or hosts:
    spec = {**defaults, **(hosts.get(name) or {})}
    if wanted or spec.get('ssh', True):
        print(name, spec.get('container') or '-', spec.get('workspace') or '-')
PY
}

# on_hosts FUNCTION ARGS -- HOST...: run each host's generated script, logging; rc 1 if any failed.
for_hosts() {
    local fn=$1 rc=0 name container workspace
    shift
    while read -r name container workspace; do
        [[ $container != - && $workspace != - ]] || { log "FAIL $name: registry gives no container/workspace"; rc=1; continue; }
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
docker inspect $1 --format 'container $1 {{.Config.Image}} running={{.State.Running}}' 2>/dev/null || echo "container $1 missing"
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
    if [ -n "\$(git status --porcelain)" ]; then
        git diff HEAD > "\$backup/\$repo.diff"; git status --porcelain > "\$backup/\$repo.status"
        git ls-files -o --exclude-standard -z | tar --null -T - -czf "\$backup/\$repo.untracked.tgz"
        echo "BACKUP \$repo -> \$backup"
    fi
    git fetch -q origin || { echo "FAIL \$repo: fetch"; rc=1; continue; }
    [ "\$branch" = main ] || git checkout -q main || { echo "FAIL \$repo: checkout main from \$branch"; rc=1; continue; }
    if git merge --ff-only -q origin/main; then echo "OK \$repo \$branch@\$head -> main@\$(git rev-parse --short HEAD)"
    else echo "FAIL \$repo: main has local commits; not fast-forwarded"; rc=1; continue; fi
    # astro_dock's pinned profiles submodule follows its commit (hw.sh reads the profiles from it).
    if [ "\$repo" = astro_dock ] && git config -f .gitmodules submodule.profiles.path >/dev/null 2>&1; then
        git submodule update --init -q profiles && echo "OK profiles \$(git -C profiles rev-parse --short HEAD)" \
            || { echo "FAIL profiles: submodule update"; rc=1; }
    fi
done
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

# Runs in the container: links left by removed packages break symlink-install builds. Containers
# created before Compose set BASH_ENV need dev.bashrc sourced explicitly.
build() {
    cat <<'EOF'
source "$ROS_WS/.devcontainer/dev.bashrc" >/dev/null 2>&1
set -o pipefail; cd "$ROS_WS"
echo "pruned $(find build install -xtype l 2>/dev/null | wc -l) stale links"
find build install -xtype l -delete 2>/dev/null
colcon build --symlink-install 2>&1 | tail -20
EOF
}

build_on() {
    log "== $1: colcon build --symlink-install"
    build | $SSH "$1" docker exec -i "$2" bash -s 2>&1 | tee -a "$LOG"
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
        echo "$1: apply would back up dirty repos, fast-forward to origin/main${IMAGE:+, recreate $2 on $IMAGE} and build in $2"
    }
    for_hosts plan_one "$@"
}

driver_verify() {
    local out rc=0
    fleet_init
    verify_one() {
        out=$(heads_on "$@") || return 1
        printf '%s\n' "$out"
        ! grep -q ' missing\| running=false' <<<"$out" || return 1
        ! awk '$2 != "container" && ($3 != "main" || $5 == "dirty" || $6 != "at-origin")' <<<"$out" | grep -q .
    }
    for_hosts verify_one "$@" || { warn "Not every host is at origin/main with its container running"; rc=1; }
    return $rc
}

# "name user@address jump|-" per ssh host the TUI reaches, never this machine (its name, or the
# operator file's self:). Addresses as the registry readers find them: a literal, a $KEY of the
# network env, the host's STATIC_LEASES reservation, else name.local.
key_hosts() {
    python3 -I - "$ASTRO_REGISTRY" "$ASTRO_NETWORK_ENV" "$1" "$(uname -n)" <<'PY'
import os, shlex, sys, yaml
devices, env_file, operator, me = sys.argv[1:]
env = {}
if os.path.exists(env_file):
    for line in open(env_file):
        key, sep, value = line.strip().partition('=')
        if sep and not key.startswith('#'):
            env[key] = ' '.join(shlex.split(value))
leases = dict(entry.split('=')[1:] for entry in env.get('STATIC_LEASES', '').split() if entry.count('=') == 2)
data = yaml.safe_load(open(devices)) or {}
mine = {me.split('.')[0]}
if os.path.exists(operator):
    mine.add((yaml.safe_load(open(operator)) or {}).get('self'))
defaults, hosts = data.get('defaults') or {}, data.get('hosts') or {}

def target(name):
    spec = {**defaults, **(hosts.get(name) or {})}
    address = spec.get('address') or ''
    address = env.get(address[1:], '') if address.startswith('$') else address
    address = address or leases.get(name) or f'{name}.local'
    return f"{spec['user']}@{address}" if spec.get('user') else address

for name, spec in hosts.items():
    spec = spec or {}
    if spec.get('ssh', defaults.get('ssh', True)) and name not in mine:
        print(name, target(name), target(spec['via']) if spec.get('via') else '-')
PY
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
    done < <(key_hosts "$config/$(cfg operator_registry)")
    log "keys rc=$rc log=$LOG"; return $rc
}
