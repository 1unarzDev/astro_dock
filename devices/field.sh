# shellcheck shell=bash
# Field commands over the hosts kind (hw.sh sources this, then devices/hosts/fleet.sh):
#   hot [REPO...] [--all] [--host NAME]... [--untracked] [--dry-run]
#                            put this computer's code on the robot: each REPO (src/REPO, astro_dock or
#                            profiles; none: pick from those with local changes or commits a host lacks;
#                            --all: every one) as its working tree: committed and uncommitted edits of tracked files
#                            (untracked files only with --untracked),
#                            on the boat hosts that run it. Each host is reached straight, else through its
#                            registry jump or the vessel's operator station (the route is printed first),
#                            so it works with no internet on the boat. A git bundle over ssh, checked out on
#                            field/YYYYMMDD after a backup of the host's dirty state; profiles also pins the
#                            host's ASTRO_PROFILES_REF. Then one build per host of the packages that need
#                            it (an edit of an existing .py/.yaml/launch file is live under symlink-install)
#                            and one result line per host. Runs on a machine's own shell, not in a container.
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
# net_value KEY: one value of the vessel's network env as profile.py reads it (`hw.sh env KEY`), else empty.
net_value() { python3 -I "$HW_ROOT/devices/profile.py" value "$ASTRO_NETWORK_ENV" "${ASTRO_REGISTRY:-}" "$1" 2>/dev/null; }

# "name container workspace target jump roles" per boat host: key_hosts' hosts without the operator role.
field_hosts() {
    local name target jump container workspace roles
    while read -r name target jump container workspace roles; do
        [[ ,$roles, != *,operator,* ]] || continue
        printf '%s %s %s %s %s %s\n' "$name" "$container" "${workspace/#\~/\$HOME}" "$target" "$jump" "$roles"
    done < <(key_hosts)
}

# field_ssh TARGET JUMP COMMAND...: no prompts, 5 s to connect, through JUMP (an ssh destination; - for
# none); as the TUI does (operator key, its known_hosts) once the key exists, else with the user's ssh.
field_ssh() {
    local target=$1 jump=$2 opts=(-o ConnectTimeout=5 -o BatchMode=yes)
    shift 2
    [[ ! -f $KEY ]] || opts+=(-i "$KEY" -o IdentitiesOnly=yes -o "UserKnownHostsFile=$KEYDIR/known_hosts"
                              -o StrictHostKeyChecking=yes)
    # A ProxyCommand rather than -J: the jump gets the same timeout, key and known_hosts.
    [[ $jump == - ]] || opts+=(-o "ProxyCommand=$SSH ${opts[*]} -W %h:%p $jump")
    $SSH "${opts[@]}" "$target" "$@"
}

hot_usage() { command_usage hot | sed '1s/^      /Usage:/'; exit "${1:-0}"; }

# Inside a container the way to the robot (ssh config, mesh, keys) is the host's. HW_IN_CONTAINER=0|1: tests.
hot_in_container() {
    case ${HW_IN_CONTAINER:-} in 1) return 0 ;; 0) return 1 ;; esac
    [[ -f /.dockerenv || -f /run/.containerenv ]]
}

# hot_dir REPO: this computer's checkout of REPO: astro_dock (this workspace), profiles (the profile set,
# when it is a checkout of its own) or src/REPO; fails for anything else. HW_DOCK, HW_SRC: tests.
hot_dir() {
    local root src=${HW_SRC:-$HW_ROOT/src}
    case $1 in
        astro_dock) printf '%s\n' "${HW_DOCK:-$HW_ROOT}" ;;
        profiles)
            root=$(cd -- "${HW_PROFILES_ROOT:-/nonexistent}" 2>/dev/null && pwd -P) || return 1
            [[ $(git -C "$root" rev-parse --show-toplevel 2>/dev/null) == "$root" ]] || return 1
            printf '%s\n' "$root" ;;
        *)
            [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ && -e $src/$1/.git ]] || return 1
            printf '%s\n' "$src/$1" ;;
    esac
}

# hot_known: every repo hot can send from here: astro_dock, profiles, then each src/ checkout.
hot_known() {
    local d
    for d in astro_dock profiles "${HW_SRC:-$HW_ROOT/src}"/*/; do
        d=${d%/} d=${d##*/}
        hot_dir "$d" >/dev/null && printf '%s\n' "$d"
    done | awk '!seen[$0]++'
}

# hot_remote REPO WORKSPACE: where REPO lives on a host.
hot_remote() {
    case $1 in
        astro_dock) printf '%s\n' "$2" ;;
        profiles) printf '%s/profiles\n' "$2" ;;
        *) printf '%s/src/%s\n' "$2" "$1" ;;
    esac
}

# hot_runs ROLES REPO: a host with ROLES (comma-separated, - for none) runs REPO: astro_dock and profiles
# everywhere, a src repo by the hosts profile's repos per role.
hot_runs() {
    local roles=${1#-}
    [[ $2 == astro_dock || $2 == profiles ]] && return 0
    # shellcheck disable=SC2086  # one role per word
    [[ " $(role_repos ${roles//,/ } | tr '\n' ' ') " == *" $2 "* ]]
}

# Runs on the host: "REPO SHA clean|dirty", or "REPO missing", per REPO of WORKSPACE.
hot_heads() {
    local ws=$1 repo
    shift
    for repo; do
        # shellcheck disable=SC2016  # expanded on the host
        printf 'd=%s; if h=$(git -C "$d" rev-parse -q --verify HEAD 2>/dev/null); then s=clean; [ -z "$(git -C "$d" status --porcelain --untracked-files=no 2>/dev/null)" ] || s=dirty; echo "%s $h $s"; else echo "%s missing"; fi\n' \
            "$(hot_remote "$repo" "$ws")" "$repo" "$repo"
    done
}

# Runs on the host: "CONTAINER NAME", its dev container as astro on finds it: the registry's container: (FIXED
# yes), else the running dev service of the hosts profile's compose project, else any running compose dev
# service, else NAME (this checkout's default).
hot_container() {
    local dev='--filter label=com.docker.compose.service=dev' project
    if [[ $2 == yes ]]; then printf 'echo "CONTAINER %s"\n' "$1"; return; fi
    project=$(cfg compose_project)
    [[ -z $project ]] || printf 'c=$(docker ps --format "{{.Names}}" --filter label=com.docker.compose.project=%s %s 2>/dev/null | head -n1)\n' "$project" "$dev"
    printf '[ -n "$c" ] || c=$(docker ps --format "{{.Names}}" %s 2>/dev/null | head -n1)\necho "CONTAINER ${c:-%s}"\n' "$dev" "$1"
}

# hot_route TARGET VIA WORKSPACE FIND REPO...: reach a host and read its container (FIND: hot_container's
# script) and heads: straight to TARGET first, else
# through its registry jump (VIA), else through the vessel's operator station (HOT_STATION "NAME TARGET":
# its address, then its name, which ssh config or DNS may resolve, e.g. on a mesh). Prints "ROUTE JUMP HOP"
# (JUMP and HOP - when direct; "ROUTE none TRIED" when nothing answers), then the heads.
hot_route() {
    local target=$1 via=$2 ws=$3 script out i sname starget tried=direct hops=(-) names=(-)
    script=$4$'\n'$(hot_heads "$ws" "${@:5}")
    if [[ $via != - ]]; then hops+=("$via") names+=("$via")
    elif [[ -n ${HOT_STATION:-} ]]; then
        read -r sname starget <<<"$HOT_STATION"
        hops+=("$starget") names+=("$sname")
        [[ $starget != *@* ]] || sname=${starget%@*}@$sname
        [[ $sname == "$starget" ]] || { hops+=("$sname") names+=("${names[1]}"); }
    fi
    for i in "${!hops[@]}"; do
        if out=$(field_ssh "$target" "${hops[i]}" bash -s <<<"$script" 2>/dev/null) && [[ -n $out ]]; then
            printf 'ROUTE %s %s\n%s\n' "${hops[i]}" "${names[i]}" "$out"
            return 0
        fi
        [[ $i == 0 || $tried == *"via ${names[i]}"* ]] || tried+=", via ${names[i]}"
    done
    printf 'ROUTE none %s\n' "$tried"
    return 1
}

# hot_route_line NAME CONTAINER WORKSPACE PROBE: "route  ME → [HOP →] NAME (container C, WORKSPACE)".
hot_route_line() {
    local jump hop
    read -r _ jump hop <"$4"
    if [[ $jump == none ]]; then log "route  ${ASTRO_HOST:-$ME} → $1: unreachable (tried $hop)"; return 1; fi
    [[ $hop == - ]] && hop='' || hop="$hop → "
    log "route  ${ASTRO_HOST:-$ME} → $hop$1 (container $2, ${3/#\$HOME/\~})"
}

# hot_snapshot DIR: a commit of DIR's working tree on HEAD, made through a scratch index (the index,
# branch and files stay as they are); HEAD itself when the tree is clean.
hot_snapshot() {
    local dir=$1 index tree head name email
    head=$(git -C "$dir" rev-parse -q --verify 'HEAD^{commit}') || die "$dir has no commit"
    index=$(mktemp) || exit 1
    # -p: the index keeps its mtime, so git still re-reads a file edited in the tick of its last stat (racy git)
    cp -p -- "$(git -C "$dir" rev-parse --path-format=absolute --git-path index)" "$index" 2>/dev/null
    # Tracked files only, unless --untracked: notes and scratch files never ride along to the robot.
    tree=$(GIT_INDEX_FILE=$index git -C "$dir" add "$( [[ ${HOT_UNTRACKED:-} == 1 ]] && echo -A || echo -u )" \
        && GIT_INDEX_FILE=$index git -C "$dir" write-tree)
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

# Runs on the host: its ~/.config/astro/profiles.env pinned to SHA (ASTRO_PROFILES_REF), the rest kept.
hot_profiles_ref() {
    cat <<EOF
f=\${XDG_CONFIG_HOME:-\$HOME/.config}/astro/profiles.env
mkdir -p "\${f%/*}" && { grep -v '^ASTRO_PROFILES_REF=' "\$f" 2>/dev/null; echo ASTRO_PROFILES_REF=$1; } > "\$f.tmp" \
    && mv "\$f.tmp" "\$f" && echo "OK profiles.env ASTRO_PROFILES_REF=${1:0:7}"
EOF
}

# hot_files NAME LINES: name-status lines, each on the terminal when few, else counted (all in the log).
hot_files() {
    local n
    [[ -n $2 ]] || return 0
    n=$(wc -l <<<"$2")
    if ((n <= 20)); then sed "s/^/$1    /" <<<"$2" | tee -a "$LOG"
    else log "$1    $n files"; sed "s/^/$1    /" <<<"$2" >>"$LOG"; fi
}

# hot_send NAME DIR TARGET JUMP: HOT_REPO's snapshot onto one host's DIR (a bundle of what it lacks, then
# the checkout on HOT_BRANCH). HOT_PKGS: the packages its changes need built ('' when none).
hot_send() {
    local name=$1 dir=$2 target=$3 jump=$4 have bases=() sha bundle='' file='' out plan rc
    HOT_PKGS=''
    have=$(hot_have "$dir" | field_ssh "$target" "$jump" bash -s) || { log "FAIL $name: unreachable"; return 1; }
    for sha in $have; do git -C "$HOT_DIR" cat-file -e "$sha^{commit}" 2>/dev/null && bases+=("^$sha"); done
    if [[ -n $(git -C "$HOT_DIR" rev-list -n1 "$HOT_SHA" "${bases[@]}") ]]; then
        file=$(mktemp --suffix=.bundle) || return 1
        git -C "$HOT_DIR" bundle create -q "$file" "$HOT_REF" "${bases[@]}" 2>>"$LOG" || { log "FAIL $name: bundle $HOT_REPO"; return 1; }
        bundle="\$HOME/.cache/astro_dock/hot-$STAMP-$HOT_REPO.bundle"
        log "$name  sending $HOT_REPO: $(du -h "$file" | cut -f1) bundle${bases[*]:+ (on what $name has)}"
        field_ssh "$target" "$jump" "mkdir -p \"\$HOME/.cache/astro_dock\" && cat > \"$bundle\"" <"$file" \
            || { rm -f -- "$file"; log "FAIL $name: send the $HOT_REPO bundle"; return 1; }
        rm -f -- "$file"
    fi
    out=$(hot_checkout "$dir" "$HOT_REPO" "$HOT_SHA" "$HOT_BRANCH" "$bundle" "$HOT_ORIGIN" \
        | field_ssh "$target" "$jump" bash -s 2>&1)
    rc=$?
    grep -v '^CHANGED ' <<<"$out" | sed "s/^/$name: /" | tee -a "$LOG"
    ((rc == 0)) || { log "FAIL $name: checkout $HOT_REPO"; return 1; }
    out=$(sed -n 's/^CHANGED //p' <<<"$out" | sort -u)
    [[ -n $out ]] || return 0
    hot_files "$name" "$out"
    plan=$(field_py plan "$HOT_DIR" <<<"$out") || { log "FAIL $name: build plan of $HOT_REPO"; return 1; }
    HOT_PKGS=$(head -1 <<<"$plan")
    [[ -z $HOT_PKGS ]] || tail -n +2 <<<"$plan" | sed "s/^/$name: builds for /" | tee -a "$LOG"
}

# hot_build NAME CONTAINER TARGET JUMP PKG...: the workspace's one build (hw.sh build's body) of PKGs in
# the host's CONTAINER.
hot_build() {
    local name=$1 container=$2 target=$3 jump=$4
    shift 4
    log "== $name: colcon build --symlink-install --packages-select $* (in $container)"
    hw_build_body | field_ssh "$target" "$jump" docker exec -i -e "ASTRO_BUILD_IMAGE=\$(docker inspect --format '{{.Image}}' $container)" \
        "$container" bash -s -- --packages-select "$@" 2>&1 | tee -a "$LOG"
    [[ ${PIPESTATUS[1]} == 0 ]]
}

# hot_dry NAME CONTAINER PROBE REPO...: per repo what the host has, the commit it would get and the files
# that change; then whether it would build. Nothing is sent.
hot_dry() {
    local name=$1 container=$2 probe=$3 repo sha state dir mine base what files plan pkgs=() reasons=''
    shift 3
    for repo; do
        dir=${HOT_DIRS[$repo]} mine=${HOT_SNAP[$repo]} sha='' state=''
        read -r _ sha state < <(grep "^$repo " "$probe")
        if hot_same "$dir" "$mine" "$sha" "$state"; then log "$name  $repo ${mine:0:7} already there"; continue; fi
        if [[ $sha == missing || -z $sha ]]; then
            base=$(git -C "$dir" hash-object -t tree /dev/null) what='new'
        elif git -C "$dir" cat-file -e "$sha^{commit}" 2>/dev/null; then
            base=$sha what="was ${sha:0:7}"
        else
            base=$(git -C "$dir" rev-parse -q --verify 'refs/remotes/origin/main^{commit}' || git -C "$dir" rev-parse HEAD)
            what="was ${sha:0:7}, unknown here: files against ${base:0:7}"
        fi
        [[ $state != dirty ]] || what+=", its own edits backed up and replaced"
        files=$(git -C "$dir" diff --no-renames --name-status "$base" "$mine")
        log "$name  $repo ${mine:0:7} ($what)"
        hot_files "$name" "$files"
        [[ -n $files ]] || continue
        plan=$(field_py plan "$dir" <<<"$files") || die "Cannot plan the build of $repo"
        # shellcheck disable=SC2206  # one package per word
        pkgs+=($(head -1 <<<"$plan"))
        reasons+=$(tail -n +2 <<<"$plan")$'\n'
    done
    if ((${#pkgs[@]})); then
        log "$name  would build ${pkgs[*]} in $container"
        grep . <<<"$reasons" | sed "s/^/$name    builds for /" | tee -a "$LOG"
    else log "$name  no build needed"; fi
}

# hot_same DIR SNAP SHA STATE: the host's checkout (SHA, clean|dirty) holds exactly SNAP's files.
hot_same() {
    [[ $4 == clean && -n $3 && $3 != missing ]] || return 1
    [[ $3 == "$2" ]] && return 0
    git -C "$1" cat-file -e "$3^{commit}" 2>/dev/null \
        && [[ $(git -C "$1" rev-parse "$3^{tree}") == "$(git -C "$1" rev-parse "$2^{tree}")" ]]
}

# hot_candidates PROBES REPO...: "REPO<TAB>why" for each REPO with local changes or commits a host that
# runs it lacks (no host reached: commits not on origin/main).
hot_candidates() {
    local probes=$1 repo dir snap n line name roles sha state why lack reached ahead
    shift
    for repo; do
        dir=$(hot_dir "$repo") snap=$(hot_snapshot "$(hot_dir "$repo")") lack='' reached=false why=''
        n=$(git -C "$dir" status --porcelain | wc -l)
        for line in "${HOT_HOSTS[@]}"; do
            read -r name _ _ _ _ roles <<<"$line"
            hot_runs "$roles" "$repo" && [[ -f $probes/$name && $(head -c 10 "$probes/$name") != 'ROUTE none' ]] || continue
            reached=true
            read -r _ sha state < <(grep "^$repo " "$probes/$name")
            hot_same "$dir" "$snap" "$sha" "$state" || { [[ $state == clean ]] \
                && git -C "$dir" merge-base --is-ancestor "$snap" "$sha" 2>/dev/null; } || lack+=" $name"
        done
        if $reached; then
            [[ -z $lack ]] || { ((n == 0)) || why="$n local changes; "; why+="not on$lack"; }
        else
            ahead=$(git -C "$dir" rev-list --count 'refs/remotes/origin/main..HEAD' 2>/dev/null || echo 0)
            ((n == 0)) || why="$n local changes"
            ((ahead == 0)) || why+="${why:+; }$ahead commits not on origin (robot not reached)"
        fi
        [[ -z $why ]] || printf '%s\t%s\n' "$repo" "$why"
    done
}

hot_main() {
    local names=() only=() all=false dry=false choices=false known want=() line name container ws target via roles
    local repo probes picked candidates n rc=0 results=() result pkgs jump hop sha state fixed
    while (($#)); do
        case $1 in
            --host) [[ -n ${2:-} ]] || hot_usage 2; only+=("$2"); shift 2 ;;
            --all) all=true; shift ;;
            --dry-run) dry=true; shift ;;
            --untracked) HOT_UNTRACKED=1; shift ;;
            --choices) choices=true; shift ;;     # the picker's list (help.yaml choices_from)
            -h|--help) hot_usage ;;
            -*) hot_usage 2 ;;
            *) names+=("$1"); shift ;;
        esac
    done
    if hot_in_container; then
        $choices && exit 0
        die "Run astro hot from this machine's terminal, outside the container (in astro_dock: tools/bin/astro hot, or ./hw.sh hot)"
    fi
    field_init
    known=$(hot_known)
    $all && mapfile -t names <<<"$known"
    for repo in "${names[@]}"; do
        hot_dir "$repo" >/dev/null || die "No repo $repo here (these can go: $(tr '\n' ' ' <<<"$known"))"
    done
    ((${#names[@]})) && want=("${names[@]}") || mapfile -t want <<<"$known"
    # The boat hosts (--host: those named) that run any of them, with the repos each runs.
    declare -gA HOT_REPOS=()
    HOT_HOSTS=()
    local boat; boat=$(field_hosts)
    for name in "${only[@]}"; do
        grep -q "^$name " <<<"$boat" || die "Not one of the robot's computers: $name (they are: $(cut -d' ' -f1 <<<"$boat" | tr '\n' ' '))"
    done
    while read -r name container ws target via roles; do
        [[ -n $name ]] || continue
        ((${#only[@]} == 0)) || [[ " ${only[*]} " == *" $name "* ]] || continue
        HOT_REPOS[$name]=''
        for repo in "${want[@]}"; do hot_runs "$roles" "$repo" && HOT_REPOS[$name]+=" $repo"; done
        [[ -n ${HOT_REPOS[$name]} ]] && HOT_HOSTS+=("$name $container $ws $target $via $roles")
    done <<<"$boat"
    ((${#HOT_HOSTS[@]})) || die "No robot computer runs ${want[*]}${only[*]:+ among ${only[*]}}"
    # The route to each, in parallel: direct, else through the registry jump or the operator station.
    declare -A HOT_FIXED=()           # the registry's own container: per host ('-' when it names none)
    while IFS=$'\t' read -r name _ _ _ container _; do HOT_FIXED[$name]=$container; done < <(registry_hosts)
    HOT_STATION=$(key_hosts | awk '$6 ~ /(^|,)operator(,|$)/ {print $1, $2; exit}')
    probes=$(mktemp -d) || exit 1
    for line in "${HOT_HOSTS[@]}"; do
        read -r name container ws target via roles <<<"$line"
        # shellcheck disable=SC2086  # one repo per word
        [[ ${HOT_FIXED[$name]:--} == - ]] && fixed=no || fixed=yes
        hot_route "$target" "$via" "$ws" "$(hot_container "$container" "$fixed")" ${HOT_REPOS[$name]} >"$probes/$name" </dev/null &
    done
    wait
    if ((${#names[@]} == 0)); then             # none named: the repos that changed
        candidates=$(hot_candidates "$probes" "${want[@]}")
        if $choices; then [[ -z $candidates ]] || printf '%s\n' "$candidates"; rm -rf -- "$probes"; return 0; fi
        if [[ -z $candidates ]]; then
            log "Nothing to send: every repo is on the robot's computers as it is here (name repos, or --all)"
            rm -rf -- "$probes"; return 0
        fi
        if [[ -t 0 && -t 2 ]]; then
            printf '%s\n' "$candidates" >"$probes/choices"
            picked=$(field_py pick 'Pick the repo to put on the robot:' "$probes/choices" 3>&1 1>&2) \
                || { rm -rf -- "$probes"; echo "astro hot: cancelled, nothing sent" >&2; exit 1; }
            names=("$picked")
        else
            { echo "Name the repos to send (or --all); these changed:"; sed 's/^/  /' <<<"$candidates"; } >&2
            rm -rf -- "$probes"; exit 2
        fi
    fi
    # Each repo's working tree as a commit (tracked files' edits; untracked only with --untracked).
    declare -gA HOT_DIRS=() HOT_SNAP=()
    HOT_BRANCH=field/$(date +%Y%m%d)
    local sent=()
    for repo in "${names[@]}"; do
        [[ " ${HOT_REPOS[*]} " == *" $repo "* ]] || { log "$repo: no robot computer runs it; skipped"; continue; }
        sent+=("$repo")
        HOT_DIRS[$repo]=$(hot_dir "$repo")
        HOT_SNAP[$repo]=$(hot_snapshot "${HOT_DIRS[$repo]}") || exit 1
        n=$(git -C "${HOT_DIRS[$repo]}" status --porcelain | wc -l)
        log "$repo ${HOT_SNAP[$repo]:0:7} ($n local changes on $(git -C "${HOT_DIRS[$repo]}" symbolic-ref -q --short HEAD || echo 'a detached HEAD'))"
    done
    names=("${sent[@]}")
    ((${#names[@]})) || { rm -rf -- "$probes"; die "No robot computer runs any of them"; }
    for line in "${HOT_HOSTS[@]}"; do
        read -r name container ws target via roles <<<"$line"
        local mine=()
        for repo in "${names[@]}"; do [[ " ${HOT_REPOS[$name]} " == *" $repo "* ]] && mine+=("$repo"); done
        ((${#mine[@]})) || continue
        container=$(sed -n 's/^CONTAINER //p' "$probes/$name" | head -n1)
        container=${container:-${line#* }} container=${container%% *}
        if ! hot_route_line "$name" "$container" "$ws" "$probes/$name"; then
            results+=("$name  unreachable"); rc=1; continue
        fi
        read -r _ jump hop <"$probes/$name"
        if $dry; then hot_dry "$name" "$container" "$probes/$name" "${mine[@]}"; continue; fi
        result=$name pkgs=()
        for repo in "${mine[@]}"; do
            HOT_REPO=$repo HOT_DIR=${HOT_DIRS[$repo]} HOT_SHA=${HOT_SNAP[$repo]} HOT_REF=refs/hot/$STAMP-$repo
            HOT_ORIGIN=$(git -C "$HOT_DIR" remote get-url origin 2>/dev/null)
            read -r _ sha state < <(grep "^$repo " "$probes/$name")
            if hot_same "$HOT_DIR" "$HOT_SHA" "$sha" "$state"; then
                result+="  $repo ${sha:0:7}"; continue             # already there
            fi
            git -C "$HOT_DIR" update-ref "$HOT_REF" "$HOT_SHA" || die "Cannot record $HOT_REF"
            if hot_send "$name" "$(hot_remote "$repo" "$ws")" "$target" "$jump"; then
                result+="  $repo ${HOT_SHA:0:7}"
                # shellcheck disable=SC2206  # one package per word
                pkgs+=($HOT_PKGS)
                if [[ $repo == profiles ]]; then
                    hot_profiles_ref "$HOT_SHA" | field_ssh "$target" "$jump" bash -s 2>&1 | sed "s/^/$name: /" | tee -a "$LOG"
                    [[ ${PIPESTATUS[1]} == 0 ]] || { result+=" (profiles.env FAIL)"; rc=1; }
                fi
            else result+="  $repo FAIL"; rc=1; fi
            git -C "$HOT_DIR" update-ref -d "$HOT_REF"
        done
        if ((${#pkgs[@]} == 0)); then result+='  build none'
        elif hot_build "$name" "$container" "$target" "$jump" $(printf '%s\n' "${pkgs[@]}" | awk '!seen[$0]++'); then
            result+='  build ok'
        else result+='  build FAIL'; rc=1; fi
        results+=("$result")
    done </dev/null
    rm -rf -- "$probes"
    if $dry; then log "dry run: nothing sent"; return $rc; fi
    printf '%s\n' "${results[@]}" | tee -a "$LOG"
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
    subnet=$(net_value LAN_SUBNET)
    [[ -n $subnet ]] || die "$ASTRO_NETWORK_ENV has no LAN_SUBNET"
    link=$(field_py link "$subnet") || exit 1
    eval "$link"
    python3 -I "$HW_ROOT/devices/profile.py" roles "$ASTRO_REGISTRY" | grep -qx rtk_base && roles+=(rtk_base)
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
    while read -r name container workspace target jump _; do
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
    c=$(hw_dev_container "$HW_ROOT")
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
    domain=$(net_value ROS_DOMAIN_ID)
    printf '[Unit]\nDescription=Operator heartbeat of %s for %s (hw.sh station take)\n\n' "${ASTRO_HOST:-$ME}" "$ASTRO_VESSEL"
    printf '[Service]\nEnvironment="ASTRO_HEARTBEAT=%s"\n' "$command"
    printf 'Environment=ASTRO_HOST=%s%s\n' "${ASTRO_HOST:-$ME}" "${domain:+ ROS_DOMAIN_ID=$domain}"
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
