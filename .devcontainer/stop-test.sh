#!/usr/bin/env bash
# Proves `docker stop` reaches a `docker exec`'d keeper through stop-children.sh.
# Runs a container with the compose stop settings, execs a process that traps TERM and
# writes a marker, stops the container, and asserts the marker (and that the stop outlasted
# the keeper's cleanup, not an instant SIGKILL). A keeper with a child (a bag recorder stand-in, which
# gets TERM from stop-children.sh as well as the keeper's own SIGINT) must see that child finish its
# slower cleanup before the container exits. The same keeper in a second --pid host container must NOT
# be signalled (mount-namespace filter).
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
cat > "$tmp/fake-recorder.sh" <<'RECORDER'
#!/usr/bin/env bash
stopping=""
stop() { [[ -z $stopping ]] || return 0; stopping=1; sleep 3; echo recorder-closed > /out/recorder; exit 0; }
trap stop TERM INT
sleep infinity & wait
RECORDER
cat > "$tmp/parent-keeper.sh" <<'KEEPER'
#!/usr/bin/env bash
bash /out/fake-recorder.sh & child=$!
trap 'kill -INT "$child"; wait "$child"; echo parent-ran > /out/parent; exit 0' TERM
sleep infinity & wait
KEEPER
cp "$tmp/fake-keeper.sh" "$tmp/other/"
chmod 777 "$tmp" "$tmp/other"

# Same stop settings as the dev service: pid host, 30s grace, stop-children.sh as PID 1.
docker run -d --name "$name" --pid host --stop-timeout 30 \
    -e KEEPER_WAIT=20 \
    -v "$here/stop-children.sh:/stop-children.sh:ro" -v "$tmp:/out" \
    "$image" bash /stop-children.sh >/dev/null

# Bystander: same keeper, same uid, own container; it must survive the first stop.
docker run -d --name "$other" --pid host \
    -v "$tmp/other:/out" "$image" sleep infinity >/dev/null
sleep 1
docker exec -d "$name" bash /out/fake-keeper.sh
docker exec -d "$name" bash /out/parent-keeper.sh
docker exec -d "$other" bash /out/fake-keeper.sh
sleep 1

start=$SECONDS
docker stop "$name" >/dev/null
elapsed=$((SECONDS - start))

[[ "$(cat "$tmp/marker" 2>/dev/null)" == trap-ran ]] || { echo "FAIL: keeper trap never ran"; exit 1; }
echo "ok: docker stop reached the exec'd keeper (${elapsed}s)"
[[ "$(cat "$tmp/recorder" 2>/dev/null)" == recorder-closed ]] || { echo "FAIL: the keeper's child never finished its cleanup"; exit 1; }
[[ "$(cat "$tmp/parent" 2>/dev/null)" == parent-ran ]] || { echo "FAIL: the keeper with a child never finished"; exit 1; }
((elapsed >= 3)) || { echo "FAIL: the stop (${elapsed}s) did not outlast the child's 3s cleanup"; exit 1; }
echo "ok: a keeper's child got TERM with its keeper and finished its cleanup before the container exited"
[[ ! -e "$tmp/other/marker" ]] || { echo "FAIL: keeper in another container was signalled"; exit 1; }
echo "ok: keeper in another --pid host container was left alone"
