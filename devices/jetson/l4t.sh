# shellcheck shell=bash
# Jetson (L4T R36) image promotion from jetson.yaml: validate a CI candidate on the hardware over ssh,
# promote it in hosted CI, then pull it. The registry tag is the only state, so rollback promotes an
# earlier build (its BUILD_RUN_ID) through the same validation.

driver_usage() {
    cat <<EOF
Usage: $HW_PROGRAM $HW_KIND status USER@HOST
       $HW_PROGRAM $HW_KIND plan|apply|verify USER@HOST [BUILD_RUN_ID]
       $HW_PROGRAM $HW_KIND rollback USER@HOST BUILD_RUN_ID

status    Read-only: the host's L4T release and the digests of $(cfg image):$(cfg family).
plan      Resolve the candidate (latest, or BUILD_RUN_ID) and its provenance; touches no host.
apply     Validate the candidate on HOST ($(cfg validator)), dispatch $(cfg workflow), watch it,
          then pull the family tag on HOST and check it is the candidate.
verify    Read-only: HOST's family tag is the candidate.
rollback  apply for an earlier BUILD_RUN_ID.
SSH goes through gateway '$(cfg gateway)' (HW_GATEWAY= for direct).
EOF
}

# jetson_args HOST [RUN]: check the arguments before anything (gh or ssh) runs.
jetson_args() {
    local destination
    (($# >= 1 && $# <= 2)) || die "Usage: $HW_PROGRAM $HW_KIND ${FUNCNAME[1]#driver_} USER@HOST [BUILD_RUN_ID] (an L4T R36 Jetson)"
    HOST=$1 RUN=${2:-} GATEWAY=$(cfg gateway) FAMILY=$(cfg family) IMAGE=$(cfg image)
    for destination in "$HOST" ${GATEWAY:+"$GATEWAY"}; do
        [[ $destination =~ ^[a-zA-Z0-9][a-zA-Z0-9_.@-]*$ ]] || die "Not an ssh destination: $destination"
    done
    [[ -z $RUN || $RUN =~ ^[0-9]+$ ]] || die "BUILD_RUN_ID must be a number, not $RUN"
}

SSH_OPTIONS='-o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=2'

remote() {
    if [[ -n $GATEWAY ]]; then
        # The inner ssh uses the gateway's keys and LAN name resolution.
        # shellcheck disable=SC2086
        ssh $SSH_OPTIONS "$GATEWAY" "ssh $SSH_OPTIONS $HOST $(printf '%q' "$1")"
    else
        # shellcheck disable=SC2086
        ssh $SSH_OPTIONS "$HOST" "$1"
    fi
}

# candidate: DIGEST and BUILD_RUN from CI, provenance checked by jetson-candidate.sh (no host contact).
candidate() {
    local json
    export GH_REPO="${GH_REPO:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
    json=$(bash "$HW_ROOT/.github/scripts/jetson-candidate.sh" "$FAMILY" "$RUN")
    DIGEST=$(jq -r .digest <<<"$json") BUILD_RUN=$(jq -r .run_id <<<"$json")
    CANDIDATE=$IMAGE@$DIGEST
}

tag_is() {
    remote "$1docker image inspect --format '{{json .RepoDigests}}' $IMAGE:$FAMILY" | jq -e --arg image "$2" 'index($image) != null'
}

driver_status() {
    jetson_args "$@"
    remote "cat /etc/nv_tegra_release; docker image inspect --format '{{json .RepoDigests}}' $IMAGE:$FAMILY || true"
}

driver_plan() {
    jetson_args "$@"
    candidate
    info "Candidate $CANDIDATE (build run $BUILD_RUN)"
    info "apply would validate it on $HOST${GATEWAY:+ via $GATEWAY}, dispatch $(cfg workflow) and pull $IMAGE:$FAMILY there"
}

driver_verify() {
    jetson_args "$@"
    candidate
    tag_is '' "$CANDIDATE" >/dev/null || die "$IMAGE:$FAMILY on $HOST is not $CANDIDATE"
    info "$IMAGE:$FAMILY on $HOST is $CANDIDATE"
}

driver_rollback() {
    [[ -n ${2:-} ]] || die "rollback needs the BUILD_RUN_ID of the earlier build to promote"
    driver_apply "$@"
}

driver_apply() {
    local validation started promotion_run='' attempt
    # Globals: the EXIT trap runs after this function returns.
    jetson_args "$@"
    cd "$HW_ROOT"
    candidate
    JETSON_TMP=$(mktemp -d) JETSON_REMOTE=''
    cleanup() {
        rm -rf -- "$JETSON_TMP"
        [[ -z $JETSON_REMOTE ]] || remote "rm -rf -- $JETSON_REMOTE" || true
    }
    trap cleanup EXIT
    JETSON_REMOTE=$(remote 'mktemp -d /tmp/astro-jetson.XXXXXX')
    [[ $JETSON_REMOTE =~ ^/tmp/astro-jetson\.[a-zA-Z0-9]+$ ]] || { JETSON_REMOTE=''; die "Bad remote directory"; }
    remote "cat > $JETSON_REMOTE/boat-validate.sh" <"$HW_ROOT/$(cfg validator)"
    echo "Validating $CANDIDATE on $HOST"
    remote "bash $JETSON_REMOTE/boat-validate.sh $CANDIDATE" | tee "$JETSON_TMP/validation.log"
    validation="$(printf 'Host: %s\n' "$HOST"; grep '^PASS:' "$JETSON_TMP/validation.log")"
    started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    gh workflow run "$(cfg workflow)" --ref main \
        -f family="$FAMILY" -f run_id="$BUILD_RUN" -f digest="$DIGEST" -f validation="$validation"
    for attempt in {1..15}; do
        promotion_run="$(gh run list --workflow "$(cfg workflow)" --branch main \
            --event workflow_dispatch --limit 20 --json databaseId,displayTitle,createdAt \
            | jq -r --arg title "$FAMILY promotion $DIGEST" --arg start "$started" \
                '[.[] | select(.displayTitle == $title and .createdAt >= $start)] | max_by(.databaseId).databaseId // empty')"
        [[ -z $promotion_run ]] || break
        sleep 2
    done
    [[ -n $promotion_run ]] || die "Promotion dispatched; inspect gh run list for status."
    gh run watch "$promotion_run" --exit-status
    tag_is "docker pull $IMAGE:$FAMILY >&2 && " "$CANDIDATE"
    echo "Promoted and pulled $IMAGE:$FAMILY on $HOST ($DIGEST)."
}
