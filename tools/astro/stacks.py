"""`astro up|down|logs` and the stacks part of `astro status`: the vessel's long-running stacks, each started on its own host, so an ssh drop never
stops one. The vessel profile's `astro.stacks` is an ordered map:

    stacks:
      NAME: {host: HOST|ROLE, run: 'COMMAND', ready: [topics], needs: [NAMES], default: true,
             with: {OPTION: 'ARGS'}}

`host` is a registry host or role; `run: null` marks a stack something else starts (up only waits for its
ready topics); `with` names extra arguments `astro up --with OPTION` appends. On its host a stack runs in
the dev container in a private tmux server (`tmux -L astro`, session NAME) when tmux is there, else under
setsid; either way its process group is in DATA/stacks/NAME.pid (`PID EPOCH LOG`) and its output in
DATA/DATE/stacks/NAME.log.

A stack whose own process is down but whose ready topics already have publishers was started outside astro:
up and a command's needs use it rather than start a second copy (`astro up --force` starts one anyway), and
status shows it `up (external)`. Before up starts a stack on a host it removes the Fast DDS shared-memory
files (/dev/shm/fastrtps_*) that no live process holds, left by dead ROS processes."""
from collections import Counter
from dataclasses import dataclass, field
import datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

from . import config, remote
from . import field as fieldlog
from .engine import log

PROBE = Path(__file__).with_name('probe.py')
KEYS = {'host', 'run', 'ready', 'needs', 'default', 'with'}
READY_S = 60.0
SETTLE_S = 5.0                # at most this long for discovery to show a topic's publishers
USING = '{} already running (started outside astro); using it'


@dataclass
class Stack:
    name: str
    host: str
    run: str = ''
    ready: list = field(default_factory=list)
    needs: list = field(default_factory=list)
    default: bool = True
    options: dict = field(default_factory=dict)


def parse(section):
    """{name: Stack} in profile order from the astro section's `stacks` map."""
    raw = section.get('stacks') or {}
    if not isinstance(raw, dict):
        raise config.Error('astro.stacks must be a map NAME: {host, run, ready, needs, default, with}')
    out = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict) or not spec.get('host'):
            raise config.Error(f'astro.stacks.{name}: needs host (a registry host or role)')
        unknown = set(spec) - KEYS
        if unknown:
            raise config.Error(f'astro.stacks.{name}: unknown {", ".join(sorted(unknown))} (keys: {", ".join(sorted(KEYS))})')
        out[str(name)] = Stack(str(name), str(spec['host']), str(spec.get('run') or ''),
                               [str(t) for t in spec.get('ready') or []], [str(n) for n in spec.get('needs') or []],
                               spec.get('default', True) is not False,
                               {str(k): str(v) for k, v in (spec.get('with') or {}).items()})
    for stack in out.values():
        for need in stack.needs:
            if need not in out:
                raise config.Error(f'astro.stacks.{stack.name}: needs {need!r}, which is not a stack')
    return out


def select(stacks, names=()):
    """The stacks to start, needs first, else profile order: NAMES (none: the default ones) and their needs."""
    for name in names:
        if name not in stacks:
            raise config.Error(f'no stack {name!r} (stacks: {", ".join(stacks) or "none in astro.stacks"})')
    out, visiting = [], set()

    def visit(name):
        if name in out:
            return
        if name in visiting:
            raise config.Error(f'astro.stacks: {name} needs itself (through {", ".join(sorted(visiting))})')
        visiting.add(name)
        for need in stacks[name].needs:
            visit(need)
        visiting.discard(name)
        out.append(name)
    for name in stacks:
        if (name in names) if names else stacks[name].default:
            visit(name)
    return [stacks[n] for n in out]


def command(stack, options=()):
    """The stack's run line with the `--with` options it knows appended."""
    return ' '.join([stack.run] + [stack.options[o] for o in options if o in stack.options])


# ---- the shell run on the stack's host -------------------------------------------------------------

def data_expr(section):
    """The data root as the host's shell sees it: astro.data (workspace-relative: under $ROS_WS), else
    $ASTRO_DATA, else ~/.local/state/astro."""
    data = section.get('data')
    if not data:
        return '"${ASTRO_DATA:-$HOME/.local/state/astro}"'
    data = str(data)
    if data.startswith('~/'):
        return '"$HOME"/' + shlex.quote(data[2:])
    return shlex.quote(data) if data.startswith('/') else '"${ROS_WS:-$PWD}"/' + shlex.quote(data)


def prelude(data):
    """Shell: d (data root), s (pid dir), and alive NAME (sets p, t, l from its pid file; true while its
    process group runs and the pid is not a later process's)."""
    return (f'd={data}; s="$d/stacks"; mkdir -p "$s"\n'
            'alive() { p=; t=; l=; [ -f "$s/$1.pid" ] || return 1; read -r p t l < "$s/$1.pid"; '
            'kill -0 -- "-$p" 2>/dev/null || return 1; '
            'e=$(ps -o etimes= -p "$p" 2>/dev/null | tr -d " ") || return 0; '
            '[ -z "$e" ] || [ "$e" -le $(( $(date +%s) - t + 5 )) ]; }\n')


def start_script(name, line, data, env=None):
    """Start LINE as stack NAME unless it runs: a wrapper script, then tmux -L astro or setsid. Prints
    `astro-stack running|started PID EPOCH LOG` (or `astro-stack exited` and `astro-log` lines)."""
    exports = ''.join(f'export {k}={shlex.quote(str(v))}\n' for k, v in (env or {}).items())
    n = shlex.quote(name)
    return prelude(data) + f'''n={n}
if alive "$n"; then echo "astro-stack running $p $t $l"; exit 0; fi
day="$d/$(date +%F)/stacks"; mkdir -p "$day"; l="$day/$n.log"
cat > "$s/$n.sh" <<'ASTRO_STACK_EOF'
echo "$$ $(date +%s) $1" > "$2"
{exports}{{ {line}
}} 2>&1 | tee -ia "$1"
ASTRO_STACK_EOF
printf '=== %s  %s\\n' "$(date '+%F %T')" {shlex.quote(line)} >> "$l"
rm -f "$s/$n.pid"
if command -v tmux >/dev/null 2>&1; then
  tmux -L astro kill-session -t "=$n" 2>/dev/null
  tmux -L astro new-session -d -s "$n" bash "$s/$n.sh" "$l" "$s/$n.pid"
else   # in the foreground: an & job would ignore SIGINT, so down could not stop it cleanly
  setsid -f bash "$s/$n.sh" "$l" "$s/$n.pid" > /dev/null 2>&1 < /dev/null
fi
i=0; while [ ! -s "$s/$n.pid" ] && [ $i -lt 50 ]; do sleep 0.1; i=$((i+1)); done; sleep 1
if alive "$n"; then echo "astro-stack started $p $t $l"
else echo "astro-stack exited - - $day/$n.log"; tail -n 5 "$day/$n.log" | sed 's/^/astro-log /'; exit 1; fi
'''


def probe_script(name, topics, data, timeout, window):
    """Shell: the probe (stdin) on NAME's process group, then on failure the log's last lines."""
    words = ' '.join(shlex.quote(t) for t in topics)
    return prelude(data) + f'''n={shlex.quote(name)}; alive "$n" || p=0; [ -n "$p" ] || p=0
python3 - {timeout:g} {window:g} "$p" {words} || echo "astro-rates {{}}"
'''


def stop_script(name, data):
    """SIGINT to the process group, 20 s, then SIGTERM (5 s), then SIGKILL; prints `astro-stack ...`."""
    return prelude(data) + f'''n={shlex.quote(name)}
if ! alive "$n"; then echo "astro-stack down"; exit 0; fi
kill -INT -- "-$p"; i=0
while kill -0 -- "-$p" 2>/dev/null && [ $i -lt 100 ]; do sleep 0.2; i=$((i+1)); done
how=stopped
if kill -0 -- "-$p" 2>/dev/null; then how=terminated; kill -TERM -- "-$p"; i=0
  while kill -0 -- "-$p" 2>/dev/null && [ $i -lt 25 ]; do sleep 0.2; i=$((i+1)); done
  kill -0 -- "-$p" 2>/dev/null && {{ how=killed; kill -KILL -- "-$p"; }}
fi
command -v tmux >/dev/null 2>&1 && tmux -L astro kill-session -t "=$n" 2>/dev/null
echo "astro-stack $how $p $t $l"
'''


def outside_script(name, topics, data, settle):
    """Shell: ps_script's `astro-ps NAME up|down PID EPOCH LOG` line; when NAME's own process group is down,
    also the probe's (stdin) publishers of TOPICS."""
    n, words = shlex.quote(name), ' '.join(shlex.quote(t) for t in topics)
    pubs = f'python3 - pubs {settle:g} {words} || echo "astro-pubs {{}}"' if topics else 'echo "astro-pubs {}"'
    return prelude(data) + (f'if alive {n}; then echo "astro-ps {name} up $p $t $l"; '
                            f'else echo "astro-ps {name} down - - ${{l:--}}"; {pubs}; fi\n')


# Fast DDS files that no process holds open or mapped (fd and maps; fuser too where it exists), older than 10 s
SHM_SCRIPT = r'''held=$( { find /proc/[0-9]*/fd -maxdepth 1 -type l -printf '%l\n' 2>/dev/null | grep '^/dev/shm/'
  grep -ho '/dev/shm/[^ ]*' /proc/[0-9]*/maps 2>/dev/null; } | sort -u)
n=0; now=$(date +%s)
for f in /dev/shm/fastrtps_* /dev/shm/sem.fastrtps_*; do
  [ -f "$f" ] || continue
  printf '%s\n' "$held" | grep -qxF -- "$f" && continue
  command -v fuser >/dev/null 2>&1 && fuser -s "$f" 2>/dev/null && continue
  [ $(( now - $(stat -c %Y "$f" 2>/dev/null || echo "$now") )) -ge 10 ] || continue
  rm -f -- "$f" 2>/dev/null; [ -e "$f" ] || n=$((n+1))
done
echo "astro-shm $n"
'''


def ps_script(names, data):
    body = ''.join(f'if alive {shlex.quote(n)}; then echo "astro-ps {n} up $p $t $l"; '
                   f'else echo "astro-ps {n} down - - ${{l:--}}"; fi\n' for n in names)
    return prelude(data) + body


def logs_script(name, data, follow):
    return prelude(data) + f'''alive {shlex.quote(name)}
[ -n "$l" ] || {{ echo "no log for {name} (astro up {name})" >&2; exit 1; }}
exec tail -n 40 {"-F " if follow else ""}"$l"
'''


# ---- verbs --------------------------------------------------------------------------------------------

def run_on(vessel, stack, script, stdin=None, tag='astro-', quiet=False):
    """(returncode, stdout lines) of SCRIPT on the stack's host: right here when this is a dev container on
    that host (or a container without docker), else ssh + docker exec. Its stderr is shown only when no line
    carries TAG (the host was not reached, or the script broke), unless QUIET."""
    host, spec = remote.place(vessel, stack.host)
    if remote.in_container() and (host == config.this_host() or not spec['ssh'] and not shutil.which('docker')):
        argv, env = ['bash', '-c', script], dict(os.environ, **remote.local_env(vessel, True))
    else:
        argv, env = remote.words(vessel, host, spec, script)
    try:
        done = subprocess.run(argv, env=env, input=stdin or '', capture_output=True, text=True)
    except OSError as error:              # no ssh or docker here
        done = subprocess.CompletedProcess(argv, 127, '', f'{host}: {error}\n')
    lines = done.stdout.splitlines()
    if not quiet and not any(line.startswith(tag) for line in lines):
        sys.stderr.write(done.stderr[-2000:])
    return done.returncode, lines


def tagged(lines, tag):
    return [line[len(tag) + 1:] for line in lines if line.startswith(tag + ' ')]


def rate_text(rates, pubs=None):
    """`topic 30Hz ...`, with each topic's publisher count when PUBS ({topic: [node]}) is given."""
    def hz(value):
        return 'none' if not value else f'{value:.0f}Hz' if value >= 1 else f'{value:.1f}Hz'

    def count(topic):
        if pubs is None:
            return ''
        n = len(pubs.get(topic) or [])
        return f' ({n} pub{"" if n == 1 else "s"})'
    return ' '.join(f'{t} {hz(v)}{count(t)}' for t, v in rates.items() if not t.startswith('_'))


def classify(own_up, pubs):
    """'up' when the stack's own process runs, 'external' when it does not but one of its ready topics has a
    publisher (started outside astro), else 'down'. PUBS: {topic: [node]}."""
    if own_up:
        return 'up'
    return 'external' if any(pubs.values()) else 'down'


def duplicates(pubs):
    """A DUPLICATE line per ready topic with more than one publisher (a ready topic has one source; two mean
    two copies of a stack, e.g. one started by hand and one by astro)."""
    out = []
    for topic, nodes in pubs.items():
        if len(nodes) > 1:
            counts = Counter('?' if 'UNKNOWN' in n else n.rsplit('/', 1)[-1] for n in nodes)
            who = ', '.join(f'{n} x{c}' if c > 1 else n for n, c in sorted(counts.items()))
            out.append(f'DUPLICATE: {topic} has {len(nodes)} publishers ({who})')
    return out


def outside(vessel, stack):
    """(classify(...) or 'unreachable', {topic: [node]}, [name, up|down, pid, epoch, log]) of the stack on
    its host, in one call."""
    _, lines = run_on(vessel, stack, outside_script(stack.name, stack.ready, data_expr(vessel.astro), SETTLE_S),
                      PROBE.read_text())
    row = tagged(lines, 'astro-ps')
    if not row:
        return 'unreachable', {}, [stack.name, 'unreachable', '-', '-', '-']
    found = tagged(lines, 'astro-pubs')
    pubs = json.loads(found[-1]) if found else {}
    fields = (row[-1].split() + ['-'] * 5)[:5]
    return classify(fields[1] == 'up', pubs), pubs, fields


def clean_shm(vessel, stack):
    """Remove the host's stale Fast DDS shared-memory files (in its container); one line with the count.
    Never for a simulated vessel: its worlds share /dev/shm across containers (host IPC), so files another
    container holds look unheld from here and removing them kills that world's DDS."""
    if vessel.astro.get('sim') or os.environ.get('ASTRO_SIM_INSTANCE'):
        return
    _, lines = run_on(vessel, stack, SHM_SCRIPT, tag='astro-shm')
    found = tagged(lines, 'astro-shm')
    if found and found[-1].isdigit():
        n = int(found[-1])
        print(f'{host_of(vessel, stack)}: removed {n} stale Fast DDS shared-memory file{"" if n == 1 else "s"}')


def probe(vessel, stack, timeout, window=2.0):
    """{topic: hz, '_dead': bool} measured on the stack's host."""
    if not stack.ready:
        return {}
    _, lines = run_on(vessel, stack, probe_script(stack.name, stack.ready, data_expr(vessel.astro), timeout, window),
                      PROBE.read_text())
    found = tagged(lines, 'astro-rates')
    return json.loads(found[-1]) if found else {}


def host_of(vessel, stack):
    return remote.place(vessel, stack.host)[0]


def up(vessel, root, names, options, force=False):
    """Start NAMES (none: the default stacks) and their needs; a stack already running outside astro is used,
    not started again, unless FORCE."""
    stacks = parse(vessel.astro)
    chosen = select(stacks, names)
    unknown = [o for o in options if not any(o in s.options for s in chosen)]
    if unknown:
        raise config.Error(f'--with {", ".join(unknown)}: no chosen stack has that option '
                           f'({", ".join(sorted({o for s in stacks.values() for o in s.options})) or "none"})')
    width, failed, cleaned = max(len(s.name) for s in chosen), 0, set()
    for stack in chosen:
        host = host_of(vessel, stack)
        how, tail = 'external', []
        if stack.run and not force and outside(vessel, stack)[0] == 'external':
            print(USING.format(stack.name))
            log.write(root, 'stack', f'outside {stack.name}  {host}', vessel.astro.get('events'), echo=False)
            continue
        if stack.run and host not in cleaned:
            cleaned.add(host)
            clean_shm(vessel, stack)
        if stack.run:
            line = command(stack, options)
            code, lines = run_on(vessel, stack, start_script(stack.name, line, data_expr(vessel.astro)))
            state = (tagged(lines, 'astro-stack') or ['failed'])[-1].split()[0]
            tail = tagged(lines, 'astro-log')
            how = state
        if how in ('failed', 'exited'):
            rates, ok = {}, False
        else:
            rates = probe(vessel, stack, READY_S)
            ok = not rates.get('_dead') and all(rates.get(t) for t in stack.ready)
            if not ok and stack.run:
                _, lines = run_on(vessel, stack, prelude(data_expr(vessel.astro))
                                  + f'alive {shlex.quote(stack.name)}; [ -n "$l" ] && tail -n 5 "$l" | sed "s/^/astro-log /"')
                tail = tagged(lines, 'astro-log')
        text = rate_text(rates)
        if ok:
            note = '  (already running; astro down first to change it)' if how == 'running' else ''
            print(f'{stack.name:<{width}}  up      {host}  {text}{note}'.rstrip())
            log.write(root, 'stack', f'up    {stack.name}  {host}  {text}', vessel.astro.get('events'), echo=False)
        else:
            failed += 1
            why = 'not running' if how in ('failed', 'exited') or rates.get('_dead') else f'not ready in {READY_S:.0f} s'
            print(f'{stack.name:<{width}}  FAILED  {host}  {why}  {text}'.rstrip())
            for line in tail:
                print(f'    {line}')
            log.write(root, 'stack', f'fail  {stack.name}  {host}  {why}  {text}', vessel.astro.get('events'),
                      echo=False)
            fieldlog.auto(vessel, 'stack', stack.name, why, '\n'.join([f'{host}: {command(stack, options)}'] + tail))
            later = [s.name for s in chosen if stack.name in s.needs]
            if later:
                print(f'stopping here: {", ".join(later)} need {stack.name} (astro logs {stack.name})')
                return 1
    return 1 if failed else 0


def down(vessel, root, names):
    stacks = parse(vessel.astro)
    chosen = [s for s in (select(stacks, names) if names else stacks.values()) if s.run]
    if names:          # what needs a stopped stack goes first
        chosen += [s for s in stacks.values() if s.run and s not in chosen and any(n in names for n in s.needs)]
        chosen = [s for s in stacks.values() if s in chosen]
    for stack in reversed(chosen):
        host = host_of(vessel, stack)
        _, lines = run_on(vessel, stack, stop_script(stack.name, data_expr(vessel.astro)))
        how = (tagged(lines, 'astro-stack') or ['unreachable'])[-1].split()[0]
        print(f'{stack.name}  {how}  {host}')
        if how != 'down':
            log.write(root, 'stack', f'{how:<5} {stack.name}  {host}', vessel.astro.get('events'), echo=False)
    return 0


def rows(vessel):
    """[(name, state, host, uptime, rates text, log path, DUPLICATE lines)] of every stack, measured on its
    host: state is up, up (external) (its ready topics publish but its own process does not run), down or
    unreachable, with * when the stack is started outside astro (run: null: its ready topics only)."""
    out = []
    for stack in parse(vessel.astro).values():
        host = host_of(vessel, stack)
        state, since, logfile = 'external', '', ''
        if stack.run:
            how, _, row = outside(vessel, stack)
            state, epoch, logfile = row[1], row[3], row[4] if row[4] != '-' else ''
            if state == 'up' and epoch.isdigit():
                since = str(datetime.timedelta(seconds=int(time.time()) - int(epoch)))
            if how == 'external':
                state = 'up (external)'
        rates = probe(vessel, stack, 3.0) if state in ('up', 'external', 'up (external)') else {}
        pubs = rates.get('_pubs') or {}
        if state == 'external':
            state = ('up' if all(rates.get(t) for t in stack.ready) else 'down') + '*'
        out.append((stack.name, state, host, since, rate_text(rates, pubs if '_pubs' in rates else None), logfile,
                    duplicates(pubs)))
    return out


def table(found):
    """The stack rows as text lines (astro status)."""
    width = max((len(r[0]) for r in found), default=0)
    wide = max((len(r[1]) for r in found), default=0)
    lines = [f'{name:<{width}}  {state:<{wide}} {host}  {since:>8}  {rates}  {logfile}'.rstrip()
             for name, state, host, since, rates, logfile, *_ in found]
    if any(r[1].endswith('*') for r in found):
        lines.append('* started outside astro (run: null): its ready topics only')
    lines += [warning for r in found for warning in (r[6] if len(r) > 6 else [])]
    return lines


def logs(vessel, name, follow):
    stacks = parse(vessel.astro)
    if name not in stacks:
        raise config.Error(f'no stack {name!r} (stacks: {", ".join(stacks)})')
    stack = stacks[name]
    host, spec = remote.place(vessel, stack.host)
    argv, env = remote.words(vessel, host, spec, logs_script(name, data_expr(vessel.astro), follow))
    try:
        return subprocess.call(argv, env=env)
    except KeyboardInterrupt:
        return 0


# ---- a command's stacks: help.yaml needs/conflicts ------------------------------------------------------
# A help.yaml entry may name stacks: `needs: [nav, drive+controller]` (NAME+OPTION: started with that `with`
# option) start first when down; `conflicts: [drive]` or `[{drive: why}]` stop first when up. Stacks the
# vessel does not have are skipped. --here or ASTRO_NO_AUTOSTART=1 skips it all.

def split_need(word):
    """('drive', ['controller']) from 'drive+controller'."""
    name, *options = str(word).split('+')
    return name, [o for o in options if o]


def split_conflict(item):
    """(stack, why) from 'drive' or {drive: 'why'}."""
    if isinstance(item, dict) and len(item) == 1:
        (name, why), = item.items()
        return str(name), str(why or '')
    return str(item), ''


def plan(stacks, needs, conflicts, states):
    """(stop, start, restart, options) for a command: STOP [(name, why)] of its conflicts that are up; START
    the needed stacks (and their needs, needs first) that are down; RESTART those up without a needed
    option; OPTIONS the `with` options asked. STATES maps a stack name to (up, {option: present}), or None when its host was not reached (skipped)."""
    stop = [(n, why) for n, why in map(split_conflict, conflicts) if n in stacks and (states.get(n) or (False,))[0]]
    wanted = {}
    for name, options in map(split_need, needs):
        if name in stacks:
            wanted.setdefault(name, []).extend(o for o in options if o in stacks[name].options)
    start, restart = [], []
    for stack in select(stacks, list(wanted)) if wanted else []:
        if not stack.run or (stack.name in states and states[stack.name] is None):
            continue                      # started elsewhere (up only waits for it), or its host not reached
        up, present = states.get(stack.name, (False, {}))
        if not up:
            start.append(stack.name)
        elif not all(present.get(o) for o in wanted.get(stack.name, [])):
            restart.append(stack.name)
    options = sorted({o for opts in wanted.values() for o in opts})
    return stop, start, restart, options


def states_of(vessel, stacks, options):
    """{name: (up, {option: in its running command})} for STACKS, one ssh per host."""
    data, out = data_expr(vessel.astro), {}
    by_host = {}
    for stack in stacks:
        if stack.run:
            by_host.setdefault(stack.host, []).append(stack)
    for group in by_host.values():
        body = ''
        for stack in group:
            n = shlex.quote(stack.name)
            body += f'if alive {n}; then echo "astro-ps {stack.name} up"; else echo "astro-ps {stack.name} down"; fi\n'
            for o in options:
                if o in stack.options:
                    body += (f'grep -qF -- {shlex.quote(stack.options[o])} "$s/"{n}.sh 2>/dev/null '
                             f'&& echo "astro-opt {stack.name} {o}"\n')
        _, lines = run_on(vessel, group[0], prelude(data) + body, quiet=True)
        if not tagged(lines, 'astro-ps'):     # not reached: neither started nor stopped
            print(f'{host_of(vessel, group[0])} not reached: skipping {", ".join(s.name for s in group)}')
            out.update({s.name: None for s in group})
            continue
        found = {s.name: (False, {}) for s in group}
        for row in tagged(lines, 'astro-ps'):
            name, state = row.split()[:2]
            found[name] = (state == 'up', {})
        for row in tagged(lines, 'astro-opt'):
            name, option = row.split()[:2]
            if name in found:
                found[name][1][option] = True
        out.update(found)
    return out


def ensure(vessel, root, by, needs=(), conflicts=()):
    """Before command BY runs: stop its conflicts that are up, start its needs that are down (one line each
    saying why). Returns 0, or up's exit code when a needed stack failed."""
    if not needs and not conflicts:
        return 0
    stacks = parse(vessel.astro)
    names = {split_need(w)[0] for w in needs} | {split_conflict(c)[0] for c in conflicts}
    if not names & set(stacks):
        return 0
    asked = sorted({o for w in needs for o in split_need(w)[1]})
    needed = {s.name for s in select(stacks, [n for n in {split_need(w)[0] for w in needs} if n in stacks])} \
        if any(split_need(w)[0] in stacks for w in needs) else set()
    involved = [s for s in stacks.values() if s.name in names or s.name in needed]
    stop, start, restart, options = plan(stacks, needs, conflicts, states_of(vessel, involved, asked))
    for name, why in stop:
        print(f'stopping {name} ({why or by + " conflicts with it"})')
        down(vessel, root, [name])
    for name in restart:
        print(f'restarting {name} with {", ".join(o for o in options if o in stacks[name].options)} (needed by {by})')
        down(vessel, root, [name])
    for name in list(start):          # one started outside astro is used, never started a second time
        if outside(vessel, stacks[name])[0] == 'external':
            print(USING.format(name))
            start.remove(name)
    for name in start:
        print(f'starting {name} (needed by {by})')
    if not start and not restart:
        return 0
    return up(vessel, root, start + restart, options, force=True)     # start: just checked


def main_up(vessel, root, args):
    try:
        return up(vessel, root, args.names, [o for words in args.options for o in words.split(',') if o],
                  args.force)
    except KeyboardInterrupt:
        print('\ninterrupted: started stacks keep running (astro status, astro down)', file=sys.stderr)
        return 130
