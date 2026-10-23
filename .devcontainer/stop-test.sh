#!/usr/bin/env bash
# Proves `docker stop` reaches a `docker exec`'d keeper through stop-children.sh.
# Runs a container with the compose stop settings, execs a process matching the keeper
# pattern that traps TERM and writes a marker, stops the container, and asserts the marker
# (and that the stop outlasted the keeper's cleanup, not an instant SIGKILL). A second
# --pid host container with a matching keeper must NOT be signalled (mount-namespace filter).
# Usage: stop-test.sh [image]   (needs bash and pgrep in the image; default archlinux)
set -euo pipefail

image="${1:-archlinux:latest}"
here="$(cd "$(dirname "$0")" && pwd)"
tmp="$(mktemp -d)"
name="stop-test-$$"
other="$name-other"
trap 'docker rm -f "$name" "$other" >/dev/null 2>&1 || true; rm -rf "$tmp"' EXIT

mkdir "$tmp/other"
cat > "$tmp/fake-keeper.sh" <<'KEEPER'
#!/usr/bin/env bash
trap 'sleep 1; echo trap-ran > /out/marker; exit 0' TERM
sleep infinity & wait
KEEPER
chmod +x "$tmp/fake-keeper.sh"
cp "$tmp/fake-keeper.sh" "$tmp/other/"
chmod 777 "$tmp" "$tmp/other"

# Same stop settings as the dev service: pid host, 30s grace, stop-children.sh as PID 1.
docker run -d --name "$name" --pid host --stop-timeout 30 \
    -e KEEPER_PATTERN=fake-keeper.sh -e KEEPER_WAIT=20 \
    -v "$here/stop-children.sh:/stop-children.sh:ro" -v "$tmp:/out" \
    "$image" bash /stop-children.sh >/dev/null

# Bystander: same pattern, same uid, own container; it must survive the first stop.
docker run -d --name "$other" --pid host -e KEEPER_PATTERN=fake-keeper.sh \
    -v "$tmp/other:/out" "$image" sleep infinity >/dev/null
sleep 1
docker exec -d "$name" bash /out/fake-keeper.sh
docker exec -d "$other" bash /out/fake-keeper.sh
sleep 1

start=$SECONDS
docker stop "$name" >/dev/null
elapsed=$((SECONDS - start))

[[ "$(cat "$tmp/marker" 2>/dev/null)" == trap-ran ]] || { echo "FAIL: keeper trap never ran"; exit 1; }
echo "ok: docker stop reached the exec'd keeper (${elapsed}s)"
[[ ! -e "$tmp/other/marker" ]] || { echo "FAIL: keeper in another container was signalled"; exit 1; }
echo "ok: keeper in another --pid host container was left alone"
