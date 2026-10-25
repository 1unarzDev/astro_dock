#!/usr/bin/env bash
# Deploy to the boat computers in config/devices.yaml (every host with ssh on; the name is the ssh
# destination, so it must resolve through ssh_config or DNS). Start parts with the boat_test TUI.
#   deploy [--image REF] [HOST...]  back up dirty state, fast-forward astro_dock and BOAT_REPOS to
#                                   origin/main, prune stale symlink-install links, build in roboboat_dev.
#                                   --image recreates roboboat_dev on REF through prebuild.sh and Compose
#                                   (named volumes kept) after committing the old one as a rollback image.
#   keys [HOST...]                  create the operator key ~/.config/mhseals/ssh/id_ed25519 if missing,
#                                   install it in each other host's authorized_keys (your own ssh, which
#                                   may ask once) and record its host key where the TUI checks it. Idempotent.
# BOAT_REPOS (space separated) and BOAT_DEVICES (a devices.yaml) override the defaults.
# Logs go to ~/.local/state/astro_dock/boat_hosts/ on this computer.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH=${BOAT_HOSTS_SSH:-ssh}
SSH_COPY_ID=${BOAT_HOSTS_SSH_COPY_ID:-ssh-copy-id}
BOAT_REPOS=${BOAT_REPOS:-mhseals_hardware mhseals_nav mhseals_testing}
BOAT_DEVICES=${BOAT_DEVICES:-$SCRIPT_DIR/config/devices.yaml}
BOAT_ENV=${BOAT_ENV:-$SCRIPT_DIR/config/boat-network.env}
MHSEALS_CONFIG=${XDG_CONFIG_HOME:-$HOME/.config}/mhseals

stamp=$(date +%Y%m%d-%H%M%S)
state=${XDG_STATE_HOME:-$HOME/.local/state}/astro_dock/boat_hosts
mkdir -p "$state"
LOG=$state/${1:-usage}-$stamp.log
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

# Runs on the host: one line per repository, OK / BACKUP / FAIL.
fast_forward() {
    cat <<EOF
rc=0; backup=\${XDG_STATE_HOME:-\$HOME/.local/state}/astro_dock/boat_hosts/backup-$stamp
for repo in astro_dock $BOAT_REPOS; do
    dir=\$HOME/astro_dock; [ "\$repo" = astro_dock ] || dir=\$HOME/astro_dock/src/\$repo
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
    else echo "FAIL \$repo: main has local commits; not fast-forwarded"; rc=1; fi
done
exit \$rc
EOF
}

# Runs on the host: README "standalone deployment" path, rollback image first.
recreate() {
    cat <<EOF
set -e; cd \$HOME/astro_dock
if docker inspect roboboat_dev >/dev/null 2>&1; then
    docker commit roboboat_dev astro-rollback:$stamp >/dev/null
    echo "rollback image astro-rollback:$stamp (boat_hosts.sh deploy --image astro-rollback:$stamp HOST)"
fi
case $1 in */*) docker pull -q $1 ;; esac  # registry references refresh; local rollback images do not
.devcontainer/prebuild.sh
ASTRO_IMAGE=$1 docker compose -p astro_dock_devcontainer -f .devcontainer/docker-compose.yml \
    -f .devcontainer/docker-compose.override.yml up -d dev
docker inspect roboboat_dev --format '{{.Config.Image}} volumes: {{range .Mounts}}{{.Name}} {{end}}'
EOF
}

# Containers created before Compose set BASH_ENV need dev.bashrc sourced explicitly.
ENV_SETUP='source "$ROS_WS/.devcontainer/dev.bashrc" >/dev/null 2>&1'

# Runs in roboboat_dev: links left by removed packages break symlink-install builds.
build() {
    echo "$ENV_SETUP"
    cat <<'EOF'
set -o pipefail; cd "$ROS_WS"
echo "pruned $(find build install -xtype l 2>/dev/null | wc -l) stale links"
find build install -xtype l -delete 2>/dev/null
colcon build --symlink-install 2>&1 | tail -20
EOF
}

# The ssh-reachable hosts of the device registry, one per line.
deploy_hosts() {
    python3 - "$BOAT_DEVICES" <<'PY'
import sys, yaml
data = yaml.safe_load(open(sys.argv[1])) or {}
default = (data.get('defaults') or {}).get('ssh', True)
for name, spec in (data.get('hosts') or {}).items():
    if (spec or {}).get('ssh', default):
        print(name)
PY
}

deploy() {
    local image="" rc=0 host status
    if [[ ${1:-} == --image ]]; then image=${2:?--image needs a reference}; shift 2; fi
    local hosts=("$@")
    [[ ${#hosts[@]} -gt 0 ]] || mapfile -t hosts < <(deploy_hosts) || return 1
    for host in "${hosts[@]}"; do
        log "== $host: fast-forward astro_dock $BOAT_REPOS"
        fast_forward | $SSH "$host" bash -s 2>&1 | tee -a "$LOG"; status=${PIPESTATUS[1]}
        [[ $status == 0 ]] || { log "WARN $host: repositories not all at origin/main"; rc=1; }
        if [[ -n $image ]]; then
            log "== $host: recreate roboboat_dev on $image"
            recreate "$image" | $SSH "$host" bash -s 2>&1 | tee -a "$LOG"
            [[ ${PIPESTATUS[1]} == 0 ]] || { log "FAIL $host: recreate"; rc=1; continue; }
        fi
        log "== $host: colcon build --symlink-install"
        build | $SSH "$host" docker exec -i roboboat_dev bash -s 2>&1 | tee -a "$LOG"
        if [[ ${PIPESTATUS[1]} == 0 ]]; then log "OK $host build"; else log "FAIL $host build"; rc=1; fi
    done
    log "deploy rc=$rc log=$LOG"; return $rc
}

# "name user@address jump|-" per ssh host the TUI reaches, never this machine (its name, or the
# operator file's self:). Addresses as mhseals_testing's registry finds them: a literal, a $KEY of
# boat-network.env, the host's STATIC_LEASES reservation, else name.local.
key_hosts() {
    python3 - "$BOAT_DEVICES" "$BOAT_ENV" "$MHSEALS_CONFIG/devices.yaml" "$(uname -n)" <<'PY'
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

keys() {
    local dir=$MHSEALS_CONFIG/ssh rc=0 name target jump wanted=" $* "
    local key=$dir/id_ed25519
    mkdir -p "$dir" && chmod 700 "$dir" || return 1
    if [[ ! -f $key ]]; then
        ssh-keygen -q -t ed25519 -N '' -C "mhseals-operator@$(uname -n)" -f "$key" || return 1
        log "created $key"
    fi
    # The TUI's own options (mhseals_testing harness/remote.py): this key, its known_hosts, no prompts.
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

case ${1:-} in
    deploy) shift; deploy "$@" ;;
    keys) shift; keys "$@" ;;
    *) usage ;;
esac
