"""`astro up|down|logs` and the stacks part of `astro status`: the vessel's long-running stacks, each started on its own host, so an ssh drop never
stops one. The vessel profile's `astro.stacks` is an ordered map:

    stacks:
      NAME: {host: HOST|ROLE, run: 'COMMAND', ready: [topics], needs: [NAMES], default: true,
             with: {OPTION: 'ARGS'}}

`host` is a registry host or role; `run: null` marks a stack something else starts (up only waits for its
ready topics); `with` names extra arguments `astro up --with OPTION` appends. On its host a stack runs in
the dev container in a private tmux server (`tmux -L astro`, session NAME) when tmux is there, else under
setsid; either way its process group is in DATA/stacks/NAME.pid (`PID EPOCH LOG`) and its output in
DATA/DATE/stacks/NAME.log."""
from dataclasses import dataclass, field
import datetime
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

from . import config, remote
from . import field as fieldlog
from .engine import log

PROBE = Path(__file__).with_name('probe.py')
KEYS = {'host', 'run', 'ready', 'needs', 'default', 'with'}
READY_S = 60.0


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

def run_on(vessel, stack, script, stdin=None, tag='astro-'):
    """(returncode, stdout lines) of SCRIPT on the stack's host; its stderr is shown only when no line
    carries TAG (the host was not reached, or the script broke)."""
    host, spec = remote.place(vessel, stack.host)
    argv, env = remote.words(vessel, host, spec, script)
    done = subprocess.run(argv, env=env, input=stdin or '', capture_output=True, text=True)
    lines = done.stdout.splitlines()
    if not any(line.startswith(tag) for line in lines):
        sys.stderr.write(done.stderr[-2000:])
    return done.returncode, lines


def tagged(lines, tag):
    return [line[len(tag) + 1:] for line in lines if line.startswith(tag + ' ')]


def rate_text(rates):
    def hz(value):
        return 'none' if not value else f'{value:.0f}Hz' if value >= 1 else f'{value:.1f}Hz'
    return ' '.join(f'{t} {hz(v)}' for t, v in rates.items() if not t.startswith('_'))


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


def up(vessel, root, names, options):
    stacks = parse(vessel.astro)
    chosen = select(stacks, names)
    unknown = [o for o in options if not any(o in s.options for s in chosen)]
    if unknown:
        raise config.Error(f'--with {", ".join(unknown)}: no chosen stack has that option '
                           f'({", ".join(sorted({o for s in stacks.values() for o in s.options})) or "none"})')
    width, failed = max(len(s.name) for s in chosen), 0
    for stack in chosen:
        host = host_of(vessel, stack)
        how, tail = 'external', []
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
    """[(name, state, host, uptime, rates text, log path)] of every stack, measured on its host: state is up,
    down or unreachable, with * when the stack is started outside astro (run: null: its ready topics only)."""
    out = []
    for stack in parse(vessel.astro).values():
        host = host_of(vessel, stack)
        state, since, logfile = 'external', '', ''
        if stack.run:
            _, lines = run_on(vessel, stack, ps_script([stack.name], data_expr(vessel.astro)))
            row = (tagged(lines, 'astro-ps') or [f'{stack.name} unreachable - - -'])[-1].split()
            state, epoch, logfile = row[1], row[3], row[4] if row[4] != '-' else ''
            if state == 'up' and epoch.isdigit():
                since = str(datetime.timedelta(seconds=int(time.time()) - int(epoch)))
        rates = probe(vessel, stack, 3.0) if state in ('up', 'external') else {}
        if state == 'external':
            state = ('up' if all(rates.get(t) for t in stack.ready) else 'down') + '*'
        out.append((stack.name, state, host, since, rate_text(rates), logfile))
    return out


def table(found):
    """The stack rows as text lines (astro status)."""
    width = max((len(r[0]) for r in found), default=0)
    wide = max((len(r[1]) for r in found), default=0)
    lines = [f'{name:<{width}}  {state:<{wide}} {host}  {since:>8}  {rates}  {logfile}'.rstrip()
             for name, state, host, since, rates, logfile in found]
    if any(r[1].endswith('*') for r in found):
        lines.append('* started outside astro (run: null): its ready topics only')
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


def main_up(vessel, root, args):
    try:
        return up(vessel, root, args.names, [o for words in args.options for o in words.split(',') if o])
    except KeyboardInterrupt:
        print('\ninterrupted: started stacks keep running (astro status, astro down)', file=sys.stderr)
        return 130
