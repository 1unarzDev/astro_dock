#!/usr/bin/env bash
# Resolve only immutable candidates from successful builds of the family's release branch.
set -euo pipefail
family="${1:?Specify a Jetson image family}"
run_id="${2:-}"
case "$family" in
    jetson-jp5-foxy) workflow=jetson-jp5.yml; branch=legacy/jp5-foxy ;;
    jetson-jp6-jazzy) workflow=images.yml; branch=main ;;
    *) echo 'Unknown Jetson image family.' >&2; exit 2 ;;
esac
export GH_REPO="${GH_REPO:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
if [[ -z "$run_id" ]]; then
    run_id="$(gh run list --workflow "$workflow" --branch "$branch" \
        --status success --limit 1 --json databaseId --jq '.[0].databaseId // empty')"
fi
[[ "$run_id" =~ ^[0-9]+$ ]] || { echo 'No successful build found.' >&2; exit 2; }
metadata="$(gh api "repos/${GH_REPO}/actions/runs/${run_id}")"
jq -e --arg path ".github/workflows/$workflow" --arg branch "$branch" \
    '.path == $path and .head_branch == $branch and .conclusion == "success"' \
    <<<"$metadata" >/dev/null
revision="$(jq -r .head_sha <<<"$metadata")"
directory="$(mktemp -d)"
trap 'rm -rf -- "$directory"' EXIT
gh run download "$run_id" --name "${family}-candidate" --dir "$directory"
jq -e --arg family "$family" --arg revision "$revision" \
    '.family == $family and .revision == $revision and (.digest | test("^sha256:[0-9a-f]{64}$"))' \
    "$directory/candidate.json" >/dev/null
jq --arg run_id "$run_id" '. + {run_id: $run_id}' "$directory/candidate.json"
