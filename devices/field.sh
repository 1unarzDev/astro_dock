# shellcheck shell=bash
# Field commands over the hosts kind (hw.sh sources this, then devices/hosts/fleet.sh):
#   hot REPO [--host NAME] [--dry-run]
#                            this computer's working tree of src/REPO (uncommitted and untracked files
#                            too) to the boat hosts that run it, over the LAN only: a git bundle over ssh,
#                            checked out on field/YYYYMMDD in each host's workspace after a backup of its
#                            dirty state. Only edits of existing .py/.yaml/launch files: no colcon
#                            (symlink-install makes them live); else only the touched packages build.
#   station take [--dry-run] make this computer the operator station: kernel modules first, the boat
#                            link by the vessel subnet, the gateway and operator time roles (setup's
#                            gateway and clock stages), the operator key on the boat hosts and in the dev
#                            container, the station file on every boat host, and the hosts profile's
#                            operator_heartbeat command as a systemd --user service (devices/heartbeat.sh
#                            runs it in the dev container).
#   station release          stop and disable that heartbeat service.
# Boat hosts: the registry's ssh hosts without the operator role. They are reached as the TUI reaches
# them (key_hosts: LAN address, operator key) once the key exists, else by registry name.

# shellcheck source=devices/build.sh
source "$HW_ROOT/devices/build.sh"     # hw_build_body: the workspace's one build, as hosts apply runs it

field_init() {
    set +e
    SSH=${HW_SSH:-ssh} DOCKER=${HW_DOCKER:-docker} SYSTEMCTL=${HW_SYSTEMCTL:-systemctl}
    [[ -r ${ASTRO_REGISTRY:-} ]] || die "The profile names no readable registry (ASTRO_REGISTRY)"
    STAMP=$(date +%Y%m%d-%H%M%S)
    LOG=$(hw_state hosts)/${FUNCNAME[1]%_main}-$STAMP.log
    KEYDIR=${XDG_CONFIG_HOME:-$HOME/.config}/$(cfg operator_key)
    KEY=$KEYDIR/id_ed25519
    ME=$(uname -n) ME=${ME%%.*}
}

field_py() { python3 -I "$HW_ROOT/devices/field.py" "$@"; }

# "name container workspace target jump" per boat host (the given names, else all).
field_hosts() {
    local name container workspace target jump
    declare -A to via
    while read -r name target jump; do to[$name]=$target via[$name]=$jump; done \
        < <(key_hosts)
    local boat=" $(field_py boat-hosts "$ASTRO_REGISTRY" | tr '\n' ' ') "
    while read -r name container workspace; do
        [[ $boat == *" $name "* && $name != "$ME" && $name != "${ASTRO_HOST:-}" ]] || continue
        printf '%s %s %s %s %s\n' "$name" "$container" "${workspace/#\~/\$HOME}" "${to[$name]:-$name}" "${via[$name]:--}"
    done < <(fleet_hosts "$@")
}

# field_ssh TARGET JUMP COMMAND...: as the TUI does (operator key, its known_hosts, no prompts) once the
# key exists, else plain ssh.
field_ssh() {
    local target=$1 jump=$2 opts=()
    shift 2
    if [[ -f $KEY ]]; then
        opts=(-i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10
              -o "UserKnownHostsFile=$KEYDIR/known_hosts" -o StrictHostKeyChecking=yes)
        [[ $jump == - ]] || opts+=(-o "ProxyCommand=ssh ${opts[*]} -W %h:%p $jump")
    fi
    $SSH "${opts[@]}" "$target" "$@"
}

hot_usage() { command_usage hot | sed '1s/^      /Usage:/'; exit "${1:-0}"; }

# hot_snapshot DIR: a commit of DIR's working tree on HEAD, made through a scratch index (the index,
# branch and files stay as they are); HEAD itself when the tree is clean.
hot_snapshot() {
    local dir=$1 index tree head name email
    head=$(git -C "$dir" rev-parse -q --verify 'HEAD^{commit}') || die "$dir has no commit"
    index=$(mktemp) || exit 1
    # -p: the index keeps its mtime, so git still re-reads a file edited in the tick of its last stat (racy git)
    cp -p -- "$(git -C "$dir" rev-parse --path-format=absolute --git-path index)" "$index" 2>/dev/null
    tree=$(GIT_INDEX_FILE=$index git -C "$dir" add -A && GIT_INDEX_FILE=$index git -C "$dir" write-tree)
    rm -f -- "$index"
    [[ -n $tree ]] || die "Cannot snapshot $dir"
    if [[ $tree == "$(git -C "$dir" rev-parse 'HEAD^{tree}')" ]]; then printf '%s\n' "$head"; return; fi
    name=$(git -C "$dir" config user.name || echo "$ME") email=$(git -C "$dir" config user.email || echo "hot@$ME")
    git -C "$dir" -c "user.name=$name" -c "user.email=$email" commit-tree "$tree" -p "$head" \
        -m "hot: working tree of $ME at $STAMP"
}

# Runs on the host: the commits its checkout has, for the bundle's prerequisites (MISSING: none).
hot_have() {
    cat <<EOF
cd $1 2>/dev/null && git rev-parse -q --verify HEAD >/dev/null || { echo MISSING; exit 0; }
for ref in HEAD refs/remotes/origin/main refs/heads/main; do git rev-parse -q --verify "\$ref^{commit}"; done | sort -u
EOF
}

# Runs on the host: back up, fetch the bundle (or clone it), check SHA out on BRANCH, then
# "CHANGED STATUS<tab>PATH" per file that differs from what the host had.
hot_checkout() {
    local dir=$1 repo=$2 sha=$3 branch=$4 bundle=$5 origin=$6
    cat <<EOF
repo=$repo; backup=\${XDG_STATE_HOME:-\$HOME/.local/state}/astro_dock/hosts/hot-$STAMP; bundle=$bundle; old=
if [ ! -e $dir/.git ]; then
    [ -n "\$bundle" ] && git init -q $dir || { echo "FAIL \$repo: no $dir to patch"; exit 1; }
    [ -z '$origin' ] || git -C $dir remote add origin '$origin'
fi
cd $dir || exit 1
if old=\$(git rev-parse -q --verify HEAD); then
    mkdir -p "\$backup"; echo "\$repo \$(git rev-parse --abbrev-ref HEAD) \$old" >> "\$backup/heads.txt"
    git status --porcelain --no-renames | grep -v '^??' | cut -c4- | sed 's/^/CHANGED M	/'
$(backup_dirty)
fi
[ -z "\$bundle" ] || git fetch -q "\$bundle" $HOT_REF || { echo "FAIL \$repo: fetch the bundle"; exit 1; }
git checkout -q -f -B $branch $sha || { echo "FAIL \$repo: checkout $branch"; exit 1; }
[ -z "\$bundle" ] || rm -f "\$bundle"
echo "OK \$repo $branch@${sha:0:7}\${old:+ (was \$(git rev-parse --short \$old))}"
if [ -n "\$old" ]; then git diff --no-renames --name-status \$old $sha; else git ls-tree -r --name-only $sha | sed 's/^/A	/'; fi \
    | sed 's/^/CHANGED /'
EOF
}

# hot_one NAME CONTAINER WORKSPACE TARGET JUMP: patch one host, then build what needs it.
hot_one() {
    local name=$1 container=$2 target=$4 jump=$5 dir=$3 have bases=() sha bundle='' file='' out pkgs reasons
    [[ $HOT_REPO == astro_dock ]] || dir=$3/src/$HOT_REPO
    log "== $name ($target): $HOT_REPO -> $HOT_BRANCH"
    have=$(hot_have "$dir" | field_ssh "$target" "$jump" bash -s) || { log "FAIL $name: unreachable"; return 1; }
    for sha in $have; do git -C "$HOT_DIR" cat-file -e "$sha^{commit}" 2>/dev/null && bases+=("^$sha"); done
    if [[ -n $(git -C "$HOT_DIR" rev-list -n1 "$HOT_SHA" "${bases[@]}") ]]; then
        file=$(mktemp --suffix=.bundle) || return 1
        git -C "$HOT_DIR" bundle create -q "$file" "$HOT_REF" "${bases[@]}" 2>>"$LOG" || { log "FAIL $name: bundle"; return 1; }
        bundle="\$HOME/.cache/astro_dock/hot-$STAMP.bundle"
        log "sending $(du -h "$file" | cut -f1) bundle${bases[*]:+ (on what $name has)}"
        field_ssh "$target" "$jump" "mkdir -p \"\$HOME/.cache/astro_dock\" && cat > \"$bundle\"" <"$file" \
            || { rm -f -- "$file"; log "FAIL $name: send the bundle"; return 1; }
        rm -f -- "$file"
    fi
    out=$(hot_checkout "$dir" "$HOT_REPO" "$HOT_SHA" "$HOT_BRANCH" "$bundle" "$HOT_ORIGIN" \
        | field_ssh "$target" "$jump" bash -s 2>&1)
    local rc=$?
    grep -v '^CHANGED ' <<<"$out" | sed "s/^/$name: /" | tee -a "$LOG"
    ((rc == 0)) || { log "FAIL $name: checkout"; return 1; }
    out=$(sed -n 's/^CHANGED //p' <<<"$out" | sort -u)
    if [[ -z $out ]]; then log "$name: nothing changed"; return 0; fi
    sed "s/^/$name: changed /" <<<"$out" | tee -a "$LOG"
    reasons=$(field_py plan "$HOT_DIR" <<<"$out") || { log "FAIL $name: build plan"; return 1; }
    pkgs=$(head -1 <<<"$reasons") reasons=$(tail -n +2 <<<"$reasons")
    if [[ -z $pkgs ]]; then log "OK $name: no build (only .py/.yaml/launch edits, live under symlink-install)"; return 0; fi
    sed "s/^/$name: builds for /" <<<"$reasons" | tee -a "$LOG"
    log "== $name: colcon build --symlink-install --packages-select $pkgs"
    # shellcheck disable=SC2086  # pkgs: one package per word
    hw_build_body | field_ssh "$target" "$jump" docker exec -i -e "ASTRO_BUILD_IMAGE=\$(docker inspect --format '{{.Image}}' $container)" \
        "$container" bash -s -- --packages-select $pkgs 2>&1 | tee -a "$LOG"
    if [[ ${PIPESTATUS[1]} == 0 ]]; then log "OK $name build $pkgs"; else log "FAIL $name build $pkgs"; return 1; fi
}

# hot_dry HOSTS: the hosts, the files the snapshot changes against origin/main (HEAD without one) and
# their build plan; no host is contacted.
hot_dry() {
    local base files plan name container workspace target jump
    base=$(git -C "$HOT_DIR" rev-parse -q --verify 'refs/remotes/origin/main^{commit}' || git -C "$HOT_DIR" rev-parse HEAD)
    while read -r name container workspace target jump; do
        [[ $jump == - ]] && jump='' || jump=" via $jump"
        log "dry run: $name ($target$jump) container $container workspace $workspace"
    done <<<"$1"
    files=$(git -C "$HOT_DIR" diff --no-renames --name-status "$base" "$HOT_SHA")
    if [[ -z $files ]]; then log "dry run: no change against ${base:0:7}; nothing sent"; return 0; fi
    sed "s/^/dry run: changed /" <<<"$files" | tee -a "$LOG"
    plan=$(field_py plan "$HOT_DIR" <<<"$files") || die "Cannot plan the build"
    if [[ -z $(head -1 <<<"$plan") ]]; then
        log "dry run: no build (only .py/.yaml/launch edits, docs or tests)"
    else
        tail -n +2 <<<"$plan" | sed "s/^/dry run: builds for /" | tee -a "$LOG"
        log "dry run: colcon build --symlink-install --packages-select $(head -1 <<<"$plan")"
    fi
    log "dry run: against ${base:0:7}; nothing sent"
}

hot_main() {
    local only=() rc=0 hosts name repos dry=false
    [[ -n ${1:-} && $1 != -* ]] || hot_usage 2
    HOT_REPO=$1
    shift
    while (($#)); do
        case $1 in
            --host) [[ -n ${2:-} ]] || hot_usage 2; only+=("$2"); shift 2 ;;
            --dry-run) dry=true; shift ;;
            -h|--help) hot_usage ;;
            *) hot_usage 2 ;;
        esac
    done
    [[ $HOT_REPO =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "Not a repository name: $HOT_REPO"
    HOT_DIR=${HW_SRC:-$HW_ROOT/src}/$HOT_REPO           # HW_SRC: tests
    [[ $HOT_REPO != astro_dock ]] || HOT_DIR=$HW_ROOT
    [[ -e $HOT_DIR/.git ]] || die "No git checkout at $HOT_DIR"
    field_init
    HOT_BRANCH=field/$(date +%Y%m%d) HOT_SHA=$(hot_snapshot "$HOT_DIR") || exit 1
    HOT_REF=refs/hot/$STAMP HOT_ORIGIN=$(git -C "$HOT_DIR" remote get-url origin 2>/dev/null)
    hosts=$(field_hosts "${only[@]}")
    if ((${#only[@]} == 0)); then           # the boat hosts whose roles' repos include it
        hosts=$(while read -r name rest; do
            [[ -n $name ]] || continue
            repos=" $(role_repos $(host_roles "$name") | tr '\n' ' ') "
            [[ $HOT_REPO == astro_dock || $repos == *" $HOT_REPO "* ]] && printf '%s %s\n' "$name" "$rest"
        done <<<"$hosts")
    fi
    if [[ -z $hosts ]]; then
        die "No boat host runs $HOT_REPO${only[*]:+ among ${only[*]}} (boat hosts: $(field_py boat-hosts "$ASTRO_REGISTRY" | tr '\n' ' '))"
    fi
    log "hot $HOT_REPO ${HOT_SHA:0:7} ($(git -C "$HOT_DIR" status --porcelain | wc -l) local changes on $(git -C "$HOT_DIR" rev-parse --abbrev-ref HEAD)) -> $(cut -d' ' -f1 <<<"$hosts" | tr '\n' ' ')"
    if $dry; then hot_dry "$hosts"; return; fi
    git -C "$HOT_DIR" update-ref "$HOT_REF" "$HOT_SHA" || die "Cannot record $HOT_REF"
    while read -r name rest; do hot_one "$name" $rest </dev/null || rc=1; done <<<"$hosts"
    git -C "$HOT_DIR" update-ref -d "$HOT_REF"
    log "hot rc=$rc log=$LOG (land it through git; $HW_PROGRAM hosts apply returns the hosts to main)"
    return $rc
}

# The running kernel needs its modules (NAT, forwarding, USB adapters): an upgrade without a reboot
# removes them.
station_kernel() {
    local release=${HW_KERNEL_RELEASE:-$(uname -r)} root=${HW_MODULES_ROOT:-/lib/modules}
    [[ -d $root/$release ]] && return 0
    die "Reboot needed: the running kernel $release has no modules in $root (installed: $(ls -- "$root" 2>/dev/null | tr '\n' ' ')); networking roles fail until this computer reboots"
}

station_take() {
    local dry=false subnet='' link file roles=(operator gateway) boat rc=0 name container workspace target jump body heartbeat
    case ${1:-} in --dry-run) dry=true ;; '') ;; *) die "Usage: $HW_PROGRAM station take [--dry-run]" ;; esac
    station_kernel
    need_scope
    field_init
    subnet=$(hw_load_env "$ASTRO_NETWORK_ENV" LAN_SUBNET; printf '%s' "${LAN_SUBNET:-}")
    [[ -n $subnet ]] || die "$ASTRO_NETWORK_ENV has no LAN_SUBNET"
    link=$(field_py link "$subnet") || exit 1
    eval "$link"
    field_py roles "$ASTRO_REGISTRY" | grep -qx rtk_base && roles+=(rtk_base)
    file=$(cfg station_file)          # under XDG_CONFIG_HOME on each host
    [[ -n $file ]] || die "The hosts profile names no station_file (a path under ~/.config)"
    boat=$(field_hosts)
    body=$(printf '# hw.sh station take: this vessel'"'"'s operator station.\noperator: %s\nsince: %s\nroles: [%s]\n' \
        "$ME" "$(date -Iseconds)" "$(IFS=,; echo "${roles[*]}" | sed 's/,/, /g')")
    log "station $ME for $ASTRO_VESSEL: boat link $BOAT_IFACE ($BOAT_ADDR in $subnet), internet ${UPLINK_IFACE:-none}"
    [[ -n $UPLINK_IFACE ]] || warn "No default route besides $BOAT_IFACE: the boat gets no internet through this station"
    log "roles ${roles[*]}: setup's clock (ASTRO_NTP_ALLOW=operator) and gateway stages"
    log "operator key $KEY -> $(cut -d' ' -f1 <<<"$boat" | tr '\n' ' ')and the dev container"
    log "station file ~/.config/$file on this computer and each boat host:"
    sed 's/^/    /' <<<"$body"
    heartbeat=$(heartbeat_unit)
    if [[ -n $heartbeat ]]; then
        log "operator heartbeat: $HW_HEARTBEAT_UNIT, a systemd --user service:"
        sed 's/^/    /' <<<"$heartbeat"
    else log "operator heartbeat: none (the hosts profile names no operator_heartbeat command)"; fi
    if $dry; then log "dry run: nothing changed"; return 0; fi

    ASTRO_NTP_ALLOW=operator "${HW_SETUP:-$HW_ROOT/setup.linux.sh}" --components clock,gateway --yes 2>&1 | tee -a "$LOG"
    [[ ${PIPESTATUS[0]} == 0 ]] || { log "FAIL roles: setup's clock and gateway stages"; rc=1; }
    if [[ -n $boat ]]; then                     # keys logs to its own file
        local log=$LOG
        (driver_keys $(cut -d' ' -f1 <<<"$boat")) || rc=1
        LOG=$log
    fi
    station_container_key || rc=1
    station_write_local "$file" "$body" || rc=1
    while read -r name container workspace target jump; do
        [[ -n $name ]] || continue
        if field_ssh "$target" "$jump" "d=\${XDG_CONFIG_HOME:-\$HOME/.config}/$(dirname -- "$file") && mkdir -p \"\$d\" && cat > \"\$d/.station.tmp\" && mv \"\$d/.station.tmp\" \"\$d/$(basename -- "$file")\"" \
            <<<"$body" 2>>"$LOG"; then log "OK $name: station file"
        else log "FAIL $name: station file (unreachable?)"; rc=1; fi
    done <<<"$boat"
    [[ -z $heartbeat ]] || heartbeat_install "$heartbeat" || rc=1
    log "station take rc=$rc log=$LOG"
    return $rc
}

station_write_local() {
    local path=${XDG_CONFIG_HOME:-$HOME/.config}/$1
    mkdir -p -- "$(dirname -- "$path")" && printf '%s\n' "$2" >"$path.tmp" && mv -- "$path.tmp" "$path" \
        && log "OK this computer: $path"
}

# The dev container of this computer gets the operator key unless it already sees it (a mount).
station_container_key() {
    local c dir f
    c=$($DOCKER ps -q --filter "label=com.docker.compose.project=$(cfg compose_project)" \
        --filter label=com.docker.compose.service=dev 2>/dev/null | head -1)
    [[ -n $c ]] || { log "WARN no dev container running: rerun station take once it is up for its key"; return 0; }
    dir="\${XDG_CONFIG_HOME:-\$HOME/.config}/$(cfg operator_key)"
    if [[ $($DOCKER exec "$c" sh -c "cat \"$dir/id_ed25519.pub\"" 2>/dev/null) == "$(cat -- "$KEY.pub" 2>/dev/null)" ]]; then
        log "OK dev container: sees the operator key"; return 0
    fi
    for f in id_ed25519 id_ed25519.pub known_hosts; do
        [[ -f $KEYDIR/$f ]] || continue
        $DOCKER exec -i "$c" sh -c "mkdir -p \"$dir\" && chmod 700 \"$dir\" && cat > \"$dir/.$f.tmp\" && chmod 600 \"$dir/.$f.tmp\" && mv \"$dir/.$f.tmp\" \"$dir/$f\"" \
            <"$KEYDIR/$f" || { log "FAIL dev container: $f"; return 1; }
    done
    log "OK dev container: operator key installed"
}

# The unit of the hosts profile's operator_heartbeat command (none without one): this station's
# registry name (else its hostname) on the vessel's ROS domain, wherever the TUI runs; it retries while
# the dev container is down.
heartbeat_unit() {
    local domain command
    command=$(cfg operator_heartbeat)
    [[ -n $command ]] || return 0
    domain=$(unset ROS_DOMAIN_ID; hw_load_env "$ASTRO_NETWORK_ENV" ROS_DOMAIN_ID; printf '%s' "${ROS_DOMAIN_ID:-}")
    printf '[Unit]\nDescription=Operator heartbeat of %s for %s (hw.sh station take)\n\n' "${ASTRO_HOST:-$ME}" "$ASTRO_VESSEL"
    printf '[Service]\nEnvironment="ASTRO_HEARTBEAT=%s"\n' "$command"
    printf 'Environment=ASTRO_HOST=%s ASTRO_COMPOSE_PROJECT=%s%s\n' \
        "${ASTRO_HOST:-$ME}" "$(cfg compose_project)" "${domain:+ ROS_DOMAIN_ID=$domain}"
    printf 'ExecStart=%s run\nExecStop=%s stop\nRestart=always\nRestartSec=3\n\n' \
        "$HW_ROOT/devices/heartbeat.sh" "$HW_ROOT/devices/heartbeat.sh"
    printf '[Install]\nWantedBy=default.target\n'
}

heartbeat_install() {
    local dir=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
    mkdir -p -- "$dir" && printf '%s\n' "$1" >"$dir/.$HW_HEARTBEAT_UNIT.tmp" \
        && mv -- "$dir/.$HW_HEARTBEAT_UNIT.tmp" "$dir/$HW_HEARTBEAT_UNIT" \
        && $SYSTEMCTL --user daemon-reload && $SYSTEMCTL --user enable "$HW_HEARTBEAT_UNIT" >>"$LOG" 2>&1 \
        && $SYSTEMCTL --user restart "$HW_HEARTBEAT_UNIT" \
        || { log "FAIL this computer: operator heartbeat service $HW_HEARTBEAT_UNIT"; return 1; }
    log "OK this computer: operator heartbeat service $HW_HEARTBEAT_UNIT"
}

station_release() {
    [[ -z ${1:-} ]] || die "Usage: $HW_PROGRAM station release"
    field_init
    if $SYSTEMCTL --user disable --now "$HW_HEARTBEAT_UNIT" 2>/dev/null; then
        log "OK this computer: operator heartbeat service $HW_HEARTBEAT_UNIT stopped"
    else
        log "operator heartbeat service $HW_HEARTBEAT_UNIT: not installed here"
    fi
}

station_main() {
    case ${1:-} in
        take) shift; station_take "$@" ;;
        release) shift; station_release "$@" ;;
        -h|--help|'') command_usage station | sed '1s/^      /Usage:/' ;;
        *) die "Usage: $HW_PROGRAM station take [--dry-run] | station release" ;;
    esac
}
