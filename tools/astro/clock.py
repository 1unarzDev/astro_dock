"""`astro clock [--fix]`: each registry host's clock against this machine's, with its chrony source.

Each ssh host of the vessel's registry is asked over ssh, in its own shell (a container keeps its host's
clock): `date +%s.%N` and `chronyc -n -c sources`. The offset is its time minus this machine's when the
answer arrives, so it is good to the one-way ssh latency (tens of ms on a LAN), plenty to see a host that
booted days behind. --fix sets each robot host (every ssh host without the operator role) that is off by
more than FIX_S from this machine's clock (`sudo -n date -u -s @EPOCH; sudo -n chronyc makestep`) and says
what it did; chrony keeps a host right only while it reaches its source, which is why the source is shown."""
import concurrent.futures
import select
import subprocess
import sys
import time

from . import config, remote

FIX_S = 0.5            # a robot host further off than this is set by --fix
OK_S = 0.2             # within this a host counts as in step (the measurement's own error)
ASK = ('echo "astro-clock $(date +%s.%N)"; '
       'if command -v chronyc >/dev/null 2>&1; then echo astro-chrony; '
       'chronyc -n -c sources 2>/dev/null | sed "s/^/astro-src /"; fi')
# Waits for this machine's epoch on stdin, so the time sent is taken when the host is ready for it.
SET = ('echo astro-ready; read -r e; '
       'if sudo -n date -u -s "@$e" >/dev/null 2>&1; then r=set; else r=no-sudo; fi; '
       'sudo -n chronyc makestep >/dev/null 2>&1 || true; echo "astro-fixed $r"; ' + ASK)


def hosts(vessel, me=''):
    """[(name, spec)] of the registry's ssh hosts with an address, this machine left out."""
    return [(name, spec) for name, spec in vessel.hosts().items()
            if spec['ssh'] and spec['address'] and name != me]


def robot(spec):
    return 'operator' not in spec['roles']


def parse_sources(lines):
    """[{state, address, reach}] from `chronyc -n -c sources` CSV lines (mode,state,address,stratum,poll,reach,...);
    reach is the octal register of the last 8 polls (0: never reached)."""
    out = []
    for line in lines:
        fields = line.split(',')
        if len(fields) >= 6 and fields[0] in '^=#':
            out.append({'state': fields[1], 'address': fields[2], 'reach': fields[5]})
    return out


def source_text(sources, chrony=True):
    """One phrase: which source the host follows, or why it follows none."""
    if not chrony:
        return 'no chronyc'
    if not sources:
        return 'chrony: no sources (chronyd down?)'
    chosen = [s for s in sources if s['state'] == '*']
    if chosen:
        return f'synced to {chosen[0]["address"]}'
    parts = [f'{s["address"]} {"unreachable" if s["reach"] in ("0", "") else "reached, not selected"}'
             for s in sources]
    return 'NOT synced: ' + ', '.join(parts)


def offset_text(seconds):
    """+12 ms, -3.4 s, +2 h 05 min, -342 d 3 h."""
    sign, size = ('-' if seconds < 0 else '+'), abs(seconds)
    if size < 1:
        return f'{sign}{size * 1e3:.0f} ms'
    if size < 120:
        return f'{sign}{size:.1f} s'
    if size < 86400:
        return f'{sign}{int(size // 3600)} h {int(size % 3600 // 60):02d} min'
    return f'{sign}{int(size // 86400)} d {int(size % 86400 // 3600)} h'


def parse(lines, received):
    """{offset, sources, chrony, fixed} from a host's tagged lines; RECEIVED maps a line index to this
    machine's time when that line arrived."""
    out = {'offset': None, 'sources': [], 'chrony': False, 'fixed': None}
    raw = []
    for i, line in enumerate(lines):
        if line.startswith('astro-clock '):
            try:
                out['offset'] = float(line.split()[1]) - received[i]
            except (ValueError, IndexError, KeyError):
                pass
        elif line == 'astro-chrony':
            out['chrony'] = True
        elif line.startswith('astro-src '):
            raw.append(line[len('astro-src '):])
        elif line.startswith('astro-fixed '):
            out['fixed'] = line.split()[1]
    out['sources'] = parse_sources(raw)
    return out


def ask(argv, send_epoch=False, timeout=20):
    """(lines, {index: arrival time}, error) of ARGV; with SEND_EPOCH, this machine's time is written to its
    stdin when it prints astro-ready."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
    except OSError as error:
        return [], {}, str(error)
    lines, received, end = [], {}, time.monotonic() + timeout
    while time.monotonic() < end:
        ready, _, _ = select.select([proc.stdout], [], [], max(0.0, end - time.monotonic()))
        if not ready:
            break
        line = proc.stdout.readline()
        now = time.time()
        if not line:
            break
        line = line.rstrip('\n')
        received[len(lines)] = now
        lines.append(line)
        if send_epoch and line == 'astro-ready':
            proc.stdin.write(f'{time.time():.3f}\n')
            proc.stdin.flush()
    try:
        proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=max(0.1, end - time.monotonic()))
    except subprocess.TimeoutExpired:
        proc.kill()
        return lines, received, 'timed out'
    err = proc.stderr.read().strip().splitlines()
    return lines, received, ('' if proc.returncode == 0 else (err[-1] if err else f'exit {proc.returncode}'))


def measure(vessel, name, spec, fix=False):
    lines, received, error = ask(remote.host_shell(vessel, name, spec, SET if fix else ASK), send_epoch=fix)
    found = parse(lines, received)
    found['error'] = error if found['offset'] is None else ''
    return found


def row(name, spec, found):
    if found['offset'] is None:
        return f'{name:<20} {spec["address"]:<15} unreachable ({found["error"] or "no answer"})'
    return (f'{name:<20} {spec["address"]:<15} {offset_text(found["offset"]):>12}  '
            f'{source_text(found["sources"], found["chrony"])}')


def local_sync():
    """This machine's own chrony state, one phrase."""
    try:
        done = subprocess.run(['chronyc', '-n', '-c', 'sources'], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return 'no chronyc here'
    return source_text(parse_sources(done.stdout.splitlines()))


def main(vessel, me, fix=False):
    targets = hosts(vessel, me)
    if not targets:
        raise config.Error(f'{vessel.name}: no ssh hosts in {vessel.registry or "its registry (none)"}')
    print(f'clocks vs this machine ({me or "unlisted host"}, {local_sync()}); offset = host - here')
    with concurrent.futures.ThreadPoolExecutor(len(targets)) as pool:
        found = dict(zip([n for n, _ in targets], pool.map(lambda t: measure(vessel, *t), targets)))
    bad = 0
    for name, spec in targets:
        print(row(name, spec, found[name]))
        offset = found[name]['offset']
        bad += offset is None or abs(offset) > OK_S
    if not fix:
        off = [n for n, s in targets if robot(s) and found[n]['offset'] is not None
               and abs(found[n]['offset']) > FIX_S]
        if off:
            print(f'astro clock --fix sets {", ".join(off)} from this machine')
        return 1 if bad else 0
    print('fix:')
    failed = 0
    for name, spec in targets:
        before = found[name]['offset']
        if not robot(spec):
            continue
        if before is None:
            print(f'  {name}: not reached, left as is')
            failed += 1
            continue
        if abs(before) <= FIX_S:
            print(f'  {name}: {offset_text(before)}, left as is')
            continue
        after = measure(vessel, name, spec, fix=True)
        if after['fixed'] != 'set':
            print(f'  {name}: NOT set ({"sudo -n refused: needs passwordless sudo for date" if after["fixed"] == "no-sudo" else after["error"] or "no answer"})')
            failed += 1
            continue
        now = offset_text(after['offset']) if after['offset'] is not None else '?'
        print(f'  {name}: set from this machine (was {offset_text(before)}, now {now}); chrony makestep; '
              f'{source_text(after["sources"], after["chrony"])}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(config.Vessel(), config.this_host()))
