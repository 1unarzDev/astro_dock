#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose="$root/.devcontainer/docker-compose.yml"
overlay="$(mktemp)"
trap 'rm -f "$overlay"' EXIT

cat >"$overlay" <<'YAML'
services:
  dev:
    image: vsc-local-uid-image
YAML

policy="$(
    docker compose -f "$compose" -f "$overlay" config --format json \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["dev"].get("pull_policy", "missing"))'
)"

if [[ "$policy" == always ]]; then
    echo "devcontainer compose check: local UID images would be pulled from a registry" >&2
    exit 1
fi

hostname_record="$(
    docker compose -f "$compose" -f "$overlay" config --format json \
        | python3 -c 'import json,sys; s=json.load(sys.stdin)["services"]["dev"]; print("{}:{}".format(s.get("hostname"), s.get("extra_hosts", [""])[0]))'
)"
if [[ "$hostname_record" != "roboboat:roboboat=127.0.1.1" ]]; then
    echo "devcontainer compose check: service hostname is not resolvable: $hostname_record" >&2
    exit 1
fi

echo "devcontainer compose check: pass ($policy, $hostname_record)"
