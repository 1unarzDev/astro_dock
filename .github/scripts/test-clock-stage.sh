#!/usr/bin/env bash
# Checks for the setup.linux.sh clock stage on a stubbed chrony host with a scratch chrony config.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
# The shipped vessel (the profiles submodule), named, so the runner's hostname does not matter.
export ASTRO_PROFILES="$root/tests/data/profiles" ASTRO_VESSEL=a ASTRO_SECRETS=/nonexistent ASTRO_ENV_FILE=/dev/null
network_env="$("$root/hw.sh" env)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
stubs="$tmp/bin" etc="$tmp/etc"
mkdir -p "$stubs" "$etc/astro-sources"
sources="$etc/astro-sources"

# chronyd exists and timesyncd is inactive, so the chrony branch is taken; sudo runs the
# command as this user, and the clock reports synchronized.
printf '#!/bin/sh\nexit 0\n' > "$stubs/chronyd"
printf '#!/bin/sh\n[ "$1" = is-active ] && exit 3\nexit 0\n' > "$stubs/systemctl"
printf '#!/bin/sh\nexec "$@"\n' > "$stubs/sudo"
printf '#!/bin/sh\necho yes\n' > "$stubs/timedatectl"
chmod +x "$stubs"/*
# docker: records calls; "inspect" prints the image of the astro_clock container held in $legacy (fails when absent).
dlog="$tmp/docker.log" legacy="$tmp/legacy-container"
printf '#!/bin/sh\necho "docker $*" >> "%s"\ncase "$*" in\n  "inspect -f {{.Config.Image}} astro_clock") cat "%s";;\n  "rm -f astro_clock") rm -f "%s";;\nesac\n' "$dlog" "$legacy" "$legacy" > "$stubs/docker"
chmod +x "$stubs/docker"

fail() { echo "FAIL: $*"; exit 1; }
bash -n "$root/setup.linux.sh"
# clock [--dry-run]: run the stage against the scratch config, from a clean astro-sources.
clock() {
    rm -rf "$sources" && mkdir -p "$sources" && : > "$etc/chrony.conf"
    echo 'server old' > "$sources/boat-serve.conf"
    PATH="$stubs:$PATH" ASTRO_CHRONY_CONF="$etc/chrony.conf" \
        "$root/setup.linux.sh" --non-interactive --yes --components clock "$@" 2>&1
}

read -r server subnet < <(. "$network_env"; echo "$TIME_SERVER $LAN_SUBNET")

# The LAN time server: serves the subnet at local stratum 10, not orphan.
out="$(ASTRO_NTP_ALLOW=lan clock)"
grep -q "Serves time to $subnet " <<<"$out" || fail "LAN subnet not resolved"
grep -Fxq 'server time.cloudflare.com iburst' "$sources/boat.sources" || fail "public server missing"
grep -Fxq 'makestep 1 3' "$sources/astro.conf" || fail "makestep missing"
grep -Fxq "allow $subnet" "$sources/astro.conf" || fail "allow missing"
grep -Fxq 'local stratum 10' "$sources/astro.conf" || fail "local stratum missing"
! grep -q orphan "$sources/astro.conf" || fail "orphan still set"
[[ ! -e "$sources/boat-serve.conf" ]] || fail "boat-serve.conf not removed"
grep -Fxq "confdir $sources" "$etc/chrony.conf" || fail "confdir not added"
grep -Fxq "sourcedir $sources" "$etc/chrony.conf" || fail "sourcedir not added"

# The LAN server also follows the operator laptops, listed first and never preferred.
read -r -a operators < <(. "$network_env"; echo "$OPERATOR_TIME_SERVERS")
((${#operators[@]})) || fail "no operator time servers in the network env"
for op in "${operators[@]}"; do
    grep -Fxq "server $op iburst" "$sources/boat.sources" || fail "operator $op not a source"
done
[[ "$(head -1 "$sources/boat.sources")" == "server ${operators[0]} iburst" ]] || fail "operator not listed first"

# An operator laptop serves the subnet only while synchronized: no local stratum.
out="$(ASTRO_NTP_ALLOW=operator clock)"
grep -q "Serves time to $subnet (chrony allow) while" <<<"$out" || fail "operator serve message"
grep -Fxq "allow $subnet" "$sources/astro.conf" || fail "operator allow missing"
! grep -q 'local stratum' "$sources/astro.conf" || fail "operator serves at local stratum"
! grep -q "server ${operators[0]} " "$sources/boat.sources" || fail "operator follows itself"

# A LAN client: prefers the LAN time server, polls it fast, keeps the public fallback.
out="$(ASTRO_NTP_SERVER=lan clock)"
grep -q "at: $server\.$" <<<"$out" || fail "LAN server not resolved"
grep -q 'Prefers' <<<"$out" || fail "polling message missing"
grep -Fxq "server $server iburst prefer minpoll 2 maxpoll 4" "$sources/boat.sources" || fail "LAN server not preferred"
grep -Fxq 'pool time.cloudflare.com iburst' "$sources/boat.sources" || fail "public fallback missing"
grep -Fxq "allow $subnet" "$sources/astro.conf" || fail "client does not answer the subnet"
! grep -q 'local stratum' "$sources/astro.conf" || fail "client serves at local stratum"
# "boat" is lan's old name for one release: same result, one notice.
out="$(ASTRO_NTP_SERVER=boat clock 2>&1)"
grep -q 'ASTRO_NTP_SERVER=boat is now ASTRO_NTP_SERVER=lan' <<<"$out" || fail "boat alias notice missing"
grep -Fxq "server $server iburst prefer minpoll 2 maxpoll 4" "$sources/boat.sources" || fail "boat alias did not follow the LAN server"

# The BOAT_ key names are read for one release; lan refuses a network env without a time server.
old="$tmp/old-profiles"
cp -r "$root/tests/data/profiles" "$old"
sed -i -E 's/^(LAN_SUBNET|TIME_SERVER|OPERATOR_TIME_SERVERS|TRANSPORT_IP)=/BOAT_\1=/; s/^BOAT_LAN_SUBNET=/BOAT_SUBNET=/' "$old/a/network.env"
grep -q '^BOAT_SUBNET=' "$old/a/network.env" && ! grep -q '^LAN_SUBNET=' "$old/a/network.env" || fail "old-name fixture"
out="$(ASTRO_PROFILES="$old" ASTRO_NTP_SERVER=lan ASTRO_NTP_ALLOW=lan clock)"
grep -Fxq "server $server iburst prefer minpoll 2 maxpoll 4" "$sources/boat.sources" || fail "BOAT_TIME_SERVER not read"
grep -Fxq "server ${operators[0]} iburst" "$sources/boat.sources" || fail "BOAT_OPERATOR_TIME_SERVERS not read"
grep -Fxq "allow $subnet" "$sources/astro.conf" || fail "BOAT_SUBNET not read"
sed -i '/^BOAT_TIME_SERVER=/d' "$old/a/network.env"
if out="$(ASTRO_PROFILES="$old" ASTRO_NTP_SERVER=lan clock)"; then fail "lan accepted an empty TIME_SERVER"; fi
grep -q 'ASTRO_NTP_SERVER=lan needs TIME_SERVER' <<<"$out" || fail "empty TIME_SERVER message: $out"

# A public server is neither preferred nor polled fast.
out="$(ASTRO_NTP_SERVER=time.google.com clock)"
! grep -q 'Prefers' <<<"$out" || fail "polling message for a public server"
grep -Fxq 'server time.google.com iburst' "$sources/boat.sources" || fail "public server preferred"

# A private address is preferred like the LAN server.
ASTRO_NTP_SERVER=10.1.2.3 clock >/dev/null
grep -Fxq 'server 10.1.2.3 iburst prefer minpoll 2 maxpoll 4' "$sources/boat.sources" || fail "private server not preferred"

# Defaults: no serving, and a dry run writes nothing.
out="$(clock)"
! grep -q 'Serves time' <<<"$out" || fail "allow without variable"
! grep -q 'allow' "$sources/astro.conf" || fail "allow written without variable"
out="$(ASTRO_NTP_SERVER=10.1.2.3 clock --dry-run)"
grep -q 'Prefers' <<<"$out" || fail "dry run polling message missing"
[[ ! -e "$sources/boat.sources" && -e "$sources/boat-serve.conf" ]] || fail "dry run wrote files"

# Serving time installs chrony when missing and disables systemd-timesyncd, once: the stub package
# managers create chronyd and the stub systemctl keeps timesyncd's state; PATH hides a real chronyd.
safe="$tmp/safe" state="$tmp/state" log="$tmp/calls.log"
mkdir -p "$safe" "$state"
for f in /usr/bin/* /bin/*; do
    case "${f##*/}" in chronyd|sudo|systemctl|pacman|apt-get|dnf|timedatectl) ;; *) ln -sf "$f" "$safe/${f##*/}" 2>/dev/null || true ;; esac
done
rm -f "$stubs/chronyd"
for pm in pacman apt-get dnf; do
    printf '#!/bin/sh\necho "%s $*" >> "%s"\ncase "$*" in *install*|*-S*) printf "#!/bin/sh\\nexit 0\\n" > "%s/chronyd"; chmod +x "%s/chronyd" ;; esac\n' \
        "$pm" "$log" "$stubs" "$stubs" > "$stubs/$pm"
done
cat > "$stubs/systemctl" <<EOF
#!/bin/sh
echo "systemctl \$*" >> "$log"
case "\$1:\$*" in
    list-unit-files:*) echo systemd-timesyncd.service enabled ;;
    is-active:*|is-enabled:*) [ -e "$state/timesyncd" ] && exit 0; exit 3 ;;
    disable:*timesyncd*) rm -f "$state/timesyncd" ;;
esac
exit 0
EOF
chmod +x "$stubs"/*
touch "$state/timesyncd"
operator() {
    : > "$log"
    PATH="$stubs:$safe" ASTRO_NTP_ALLOW=operator ASTRO_CHRONY_CONF="$etc/chrony.conf" \
        "$root/setup.linux.sh" --non-interactive --yes --components clock 2>&1
}
printf 'confdir x\n' > "$etc/chrony.conf"
operator >/dev/null || fail "operator run failed"
grep -Eq '^(pacman .*-S .*chrony|apt-get install .*chrony|dnf .*install chrony)' "$log" || fail "chrony not installed"
grep -Fq 'systemctl disable --now systemd-timesyncd' "$log" || fail "timesyncd not disabled"
[[ ! -e "$state/timesyncd" ]] || fail "timesyncd still enabled"
# The second run installs and disables nothing.
operator >/dev/null || fail "second operator run failed"
! grep -Eq '^(pacman|apt-get|dnf)' "$log" || fail "second run touched the package manager"
! grep -Fq 'disable' "$log" || fail "second run disabled again"
grep -Fq 'enable --now' "$log" || fail "chrony not enabled"
# A LAN client switches from timesyncd to chrony the same way; a public client does not.
for mode in lan time.google.com; do
    rm -f "$stubs/chronyd"; touch "$state/timesyncd"
    : > "$log"
    PATH="$stubs:$safe" ASTRO_NTP_SERVER=$mode ASTRO_CHRONY_CONF="$etc/chrony.conf" \
        "$root/setup.linux.sh" --non-interactive --yes --dry-run --components clock >"$tmp/out.txt" 2>&1 || fail "$mode client run failed"
    if [[ $mode == lan ]]; then
        grep -Eq '^ +\$ sudo (pacman|apt-get|dnf)' "$tmp/out.txt" || fail "LAN client: chrony not installed"
        grep -Fq 'disable --now systemd-timesyncd' "$tmp/out.txt" || fail "LAN client: timesyncd not disabled"
    else
        ! grep -Eq 'sudo (pacman|apt-get|dnf)|disable' "$tmp/out.txt" || fail "public client switched to chrony"
    fi
done
rm -f "$stubs"/{pacman,apt-get,dnf}
printf '#!/bin/sh\nexit 0\n' > "$stubs/chronyd"
printf '#!/bin/sh\n[ "$1" = is-active ] && exit 3\nexit 0\n' > "$stubs/systemctl"

# Without chrony (timesyncd active) the chrony polling message is not printed.
printf '#!/bin/sh\n[ "$1" = list-unit-files ] && echo systemd-timesyncd.service enabled\nexit 0\n' > "$stubs/systemctl"
out="$(ASTRO_NTP_SERVER=time.google.com clock --dry-run)"
! grep -q 'Prefers' <<<"$out" || fail "polling message on the timesyncd path"
grep -q 'timesyncd.conf.d/60-astro.conf' <<<"$out" || fail "timesyncd path not taken"

# The retired astro_clock container is removed once when the host runs chrony, never on a dry run.
printf '#!/bin/sh\nexit 0\n' > "$stubs/chronyd"
printf '#!/bin/sh\n[ "$1" = is-active ] && exit 3\nexit 0\n' > "$stubs/systemctl"
echo astro:clock-local > "$legacy" && : > "$dlog"
clock --dry-run >/dev/null
! grep -q 'rm -f' "$dlog" && [[ -e "$legacy" ]] || fail "dry run removed the legacy container"
clock >/dev/null
grep -Fxq 'docker rm -f astro_clock' "$dlog" || fail "legacy astro_clock container not removed"
[[ ! -e "$legacy" ]] || fail "legacy container still present"
: > "$dlog"
clock >/dev/null
! grep -q 'rm -f' "$dlog" || fail "second run removed a container again"
# A container named astro_clock from another image is not ours: kept.
echo other:image > "$legacy" && : > "$dlog"
clock >/dev/null
! grep -q 'rm -f' "$dlog" && [[ -e "$legacy" ]] || fail "removed an astro_clock container of another image"

# Without a network env (an unmatched machine, or generic mode without the profiles submodule) the
# plain clock stage still runs; only the lan and operator modes need the env, and they say so.
generic="$tmp/generic"
mkdir -p "$generic/profiles"
git -C "$root" ls-files -z | grep -zv '^profiles$' | tar -C "$root" --null -T - -cf - | tar -C "$generic" -xf -
for mode in unmatched generic; do
    dir="$root" vars=(ASTRO_HOST=unlisted-host)
    [[ $mode == generic ]] && dir="$generic" vars=()
    unclock() {
        rm -rf "$sources" && mkdir -p "$sources" && : > "$etc/chrony.conf"
        env -u ASTRO_VESSEL "${vars[@]}" PATH="$stubs:$PATH" ASTRO_CHRONY_CONF="$etc/chrony.conf" \
            "$dir/setup.linux.sh" --non-interactive --yes --components clock "$@" 2>&1
    }
    unclock --dry-run >/dev/null || fail "$mode: plain clock stage failed"
    unclock >/dev/null || fail "$mode: plain clock stage failed"
    out="$(ASTRO_NTP_ALLOW=lan unclock --dry-run)" && fail "$mode: lan mode ran without a network env"
    grep -q "need the vessel's network env" <<<"$out" || fail "$mode: lan mode error unclear"
    out="$(env -u ASTRO_VESSEL "${vars[@]}" ASTRO_NETWORK_ENV="$tmp/missing.env" python3 "$dir/.devcontainer/astro-clock.py" 2>&1)" \
        && fail "$mode: astro-clock.py ran without a network env"
    [[ $(wc -l <<<"$out") == 1 && $out == astro-clock:* ]] || fail "$mode: astro-clock.py did not print one error line: $out"
done

python3 "$root/.devcontainer/astro-clock.py" --help >/dev/null
printf 'BOAT_TIME_SERVER=10.0.1.10\nSTATIC_LEASES="02:aa:00:00:00:01=a-pc=10.0.1.10"\n' > "$tmp/old-names.env"
python3 - "$root" "$tmp/old-names.env" <<'EOF'
import importlib.util, sys
spec = importlib.util.spec_from_file_location('clock', sys.argv[1] + '/.devcontainer/astro-clock.py')
clock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clock)
hosts = clock.lan_hosts()
assert hosts[0][0] == 'time server' and len(hosts) >= 2, hosts
hosts = clock.lan_hosts(sys.argv[2])                    # BOAT_TIME_SERVER: the old name, for one release
assert hosts == [('time server', '10.0.1.10'), ('a-pc', '10.0.1.10')], hosts
EOF
echo "clock stage ok"
