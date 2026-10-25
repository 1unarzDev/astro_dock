#!/usr/bin/env bash
# On-demand hardware validation over SSH, followed by hosted CI promotion.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case "${1:-}" in
    jp5) family=jetson-jp5-foxy; host="${2:-roboboat@squirtle-jetson.local}"; ref=legacy/jp5-foxy ;;
    jp6) family=jetson-jp6-jazzy; host="${2:-}"; ref=main ;;
    *) echo 'Usage: ./promote_jetson.sh jp5|jp6 [USER@HOST] [BUILD_RUN_ID]' >&2; exit 2 ;;
esac
gateway="${ASTRO_JETSON_GATEWAY-tranquility}"
for destination in "$host" ${gateway:+"$gateway"}; do
    [[ "$destination" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.@-]*$ ]] || {
        echo 'Specify an SSH host (JP6 requires a compatible Jetson).' >&2; exit 2;
    }
done
cd "$root"
export GH_REPO="${GH_REPO:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
candidate="$(bash "$root/.github/scripts/jetson-candidate.sh" "$family" "${3:-}")"
digest="$(jq -r .digest <<<"$candidate")"
build_run="$(jq -r .run_id <<<"$candidate")"
image="lunarzdev/astro@$digest"
ssh_options=(-o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=2)
remote() {
    if [[ -n "$gateway" ]]; then
        # The inner SSH uses Tranquility's keys and LAN name resolution.
        ssh "${ssh_options[@]}" "$gateway" \
            "ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=2 $host $(printf '%q' "$1")"
    else
        ssh "${ssh_options[@]}" "$host" "$1"
    fi
}
directory="$(mktemp -d)"
remote_dir=""
cleanup() {
    rm -rf -- "$directory"
    if [[ -n "$remote_dir" ]]; then
        remote "rm -rf -- $remote_dir" || true
    fi
}
trap cleanup EXIT
remote_dir="$(remote 'mktemp -d /tmp/astro-jetson.XXXXXX')"
[[ "$remote_dir" =~ ^/tmp/astro-jetson\.[a-zA-Z0-9]+$ ]] || { remote_dir=""; exit 1; }
remote "cat > $remote_dir/boat-validate.sh" < "$root/.devcontainer/boat-validate.sh"
echo "Validating $image on $host"
remote "bash $remote_dir/boat-validate.sh $image" | tee "$directory/validation.log"
validation="$(printf 'Host: %s\n' "$host"; grep '^PASS:' "$directory/validation.log")"
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
gh workflow run promote-jetson.yml --ref "$ref" \
    -f family="$family" -f run_id="$build_run" -f digest="$digest" \
    -f validation="$validation"
promotion_run=""
for attempt in {1..15}; do
    promotion_run="$(gh run list --workflow promote-jetson.yml --branch "$ref" \
        --event workflow_dispatch --limit 20 --json databaseId,displayTitle,createdAt \
        | jq -r --arg title "$family promotion $digest" --arg start "$started" \
            '[.[] | select(.displayTitle == $title and .createdAt >= $start)] | max_by(.databaseId).databaseId // empty')"
    [[ -z "$promotion_run" ]] || break
    sleep 2
done
[[ -n "$promotion_run" ]] || { echo 'Promotion dispatched; inspect gh run list for status.' >&2; exit 1; }
gh run watch "$promotion_run" --exit-status
remote "docker pull lunarzdev/astro:$family >&2 && docker image inspect --format '{{json .RepoDigests}}' lunarzdev/astro:$family" \
    | jq -e --arg image "$image" 'index($image) != null'
echo "Promoted and pulled lunarzdev/astro:$family on $host ($digest)."
