"""astro: the one high-level tool. Every verb only composes plain ROS 2 (see `astro help`); robot facts come
from the vessel profile's astro: section (config.py) and the files it names."""
import argparse
import datetime
import importlib.util
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import time

from . import clock, config, fetch, field, rec, remote, stacks, status
from .engine import apply as applying
from .engine import log, procedures, results

VERBS = ('docs', 'on', 'up', 'down', 'logs', 'status', 'rec', 'mark', 'log', 'test', 'apply', 'undo', 'bundle', 'sim',
         'check', 'issue', 'sync', 'show', 'clock', 'help')
HEADER = ('up', 'down', 'status', 'rec', 'test', 'apply', 'undo', 'bundle', 'show')   # one context line
# Hidden: `_choices KIND` prints a picker's choices (help.yaml choices_from).
CHOICES = ('test', 'apply', 'on', 'stacks', 'options', 'check', 'bags', 'presets')
# Verbs that need ROS or the vessel's data dir and state: run outside ROS, they forward to the profile's
# astro.home (a registry host or role) unless --here or ASTRO_HERE=1. Every other verb runs where it is typed.
HOME_VERBS = ('status', 'rec', 'mark', 'log', 'test', 'apply', 'undo', 'bundle')
MARK_TIMEOUT = remote.MARK_TIMEOUT      # seconds a forwarded mark's ssh may take in the background
# The background half of a forwarded mark (remote.deliver_mark), run detached by this Python.
DELIVER = ('import sys; sys.path.insert(0, sys.argv[1]); from astro import config, remote; '
           'sys.exit(remote.deliver_mark(config.Vessel(), sys.argv[2], sys.argv[3], sys.argv[4:]))')
HELP = config.WS / '.devcontainer' / 'astro-help.py'


def help_module():
    """astro-help.py, with the vessel profile's astro.help files (~ and ${VAR:-default} expanded) as extra groups."""
    try:
        extra = config.Vessel().astro.get('help') or []
    except config.Error:
        extra = []
    spec = importlib.util.spec_from_file_location('astro_help', HELP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.EXTRA = [remote.expand(p) for p in extra]
    return module


def verb_help(verb):
    module = help_module()
    packages, _ = module.discover()
    color = sys.stdout.isatty() and 'NO_COLOR' not in os.environ
    sys.stdout.write(module.text(packages, color=color, exact=('astro', verb)))
    return 0


def test_needs(s, name):
    """The stacks `astro test NAME` needs: each test's own `needs` in the tests file when it names them, else
    the vessel profile's astro.test_needs (none when unset)."""
    default = [str(n) for n in s.vessel.astro.get('test_needs') or []]
    try:
        tests = s.spec.tests
        names = s.spec.expand(name)
    except Exception:                     # the tests file is read on astro.home; the profile's needs still hold
        names, tests = [], {}
    needs = []
    for test in names or [None]:
        own = (tests.get(test) or {}).get('needs') if test else None
        for need in (default if own is None else own):
            if str(need) not in needs:
                needs.append(str(need))
    return needs


def need_ros(verb):
    if not config.have_ros():
        raise config.Error(f'astro {verb} needs ROS: run it in the dev container (astro on HOST astro {verb} ...), '
                           'or set astro.home in the vessel profile so astro forwards it there')


def forwards(verb, here, ros, home):
    """True when VERB should run on the vessel's home host instead of here."""
    return verb in HOME_VERBS and not here and not ros and bool(home)


class NoChoice(Exception):
    """A picker had no terminal (the choices were printed) or nothing to choose from: exit 2."""


class Cancelled(Exception):
    """Esc or q in a picker: one line, exit 1."""


def choose(title, options, current=None):
    """The value picked from [(value, about)]: an arrow-key list on a terminal under TITLE (what is being
    chosen), CURRENT marked; Cancelled on Esc/q. Without a terminal the choices are printed and NoChoice
    is raised."""
    value = help_module().pick(title, options, current=current)
    if value is None:
        raise Cancelled if options and sys.stdin.isatty() and sys.stdout.isatty() else NoChoice
    return value


def forward(vessel, home, args, tty=None, env=None):
    """Run `astro ARGS` on HOME, then send any pending marks. A mark goes in the background with its time
    (ssh killed after MARK_TIMEOUT s; when it fails the mark is kept pending, see remote.deliver_mark)."""
    tty = remote._tty() if tty is None else tty
    verb = args[0]
    sys.stdout.flush()                    # what this side printed (the stacks) comes first in a pipe too
    dim = sys.stderr.isatty() and 'NO_COLOR' not in os.environ
    if verb == 'mark':
        host, _ = remote.place(vessel, home)
        print(f'\033[2m(on {host})\033[0m' if dim else f'(on {host})', file=sys.stderr)
        at = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
        subprocess.Popen([sys.executable, '-c', DELIVER, str(config.WS / 'tools'), home, at, *args[1:]],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, start_new_session=True)
        return 0
    host, argv, env = remote.home_words(vessel, home, args, tty, env)
    print(f'\033[2m(on {host})\033[0m' if dim else f'(on {host})', file=sys.stderr)
    if verb == 'test' and args[1:2] not in (['list'], ['status'], []):
        print(f'running on {host}; if the link drops the test continues \u2014 RC is the stop')
    try:
        code = subprocess.call(argv, env=env)
    except KeyboardInterrupt:
        return 130
    if code == 0:
        remote.replay_marks(vessel, home)
    return code


def field_home(vessel, here):
    """The profile's astro.home when sync/show must read the day's data there: set, reached over ssh (not this
    machine's own container), and not --here, ASTRO_HERE=1 or inside ROS."""
    home = vessel.astro.get('home')
    if here or not home or config.have_ros():
        return None
    try:
        _, spec = remote.place(vessel, str(home))
    except config.Error:
        return None
    return str(home) if spec['ssh'] else None


class Session:
    """The vessel, its tests file and state, loaded on first use."""

    def __init__(self):
        self._vessel = self._spec = None
        self.no_autostart = os.environ.get('ASTRO_NO_AUTOSTART') == '1'

    @property
    def vessel(self):
        if self._vessel is None:
            self._vessel = config.Vessel()
        return self._vessel

    @property
    def spec(self):
        if self._spec is None:
            self._spec = procedures.Spec(self.vessel.path('tests'), config.find)
        return self._spec

    @property
    def root(self):
        return self.vessel.data_root()

    @property
    def events(self):
        return self.vessel.astro.get('events')

    def state(self):
        path = self.vessel.state()
        return (config.load_yaml(path) or {}) if path.exists() else {}

    def rows(self):
        return results.statuses(self.spec.order(), self.root, self.state(), self.spec.defaults())

    def software(self):
        """{repo directory name: HEAD} of the workspace and the repo holding the tests file."""
        out = {}
        for where in (config.WS, Path(self.vessel.path('tests')).parent):
            run = subprocess.run(['git', '-C', str(where), 'rev-parse', '--show-toplevel', 'HEAD'],
                                 capture_output=True, text=True)
            if run.returncode == 0:
                top, head = run.stdout.split()[:2]
                out[Path(top).name] = head
        return out


def choices(s, kind):
    """[(value, about)] for a picker: tests in ladder order with their status, unapplied results, ssh hosts
    and local vessels, stacks, their with options, checklists, the home host's bags, or the bag presets."""
    if kind == 'test':
        rows = s.rows()
        out = [(name, f'{state:<8} {s.spec.layer(name)}') for name, state, _ in rows]
        return out + [(group, 'group: ' + ' '.join(items)) for group, items in s.spec.groups.items()]
    if kind == 'apply':
        return [(name, f'{result.get("date", "")} {result.get("time", "")}  {state}  '
                       f'{result.get("summary") or ""}'.rstrip())
                for name, state, result in s.rows()
                if result and result.get('status') == 'done' and result.get('changes') and not result.get('applied')]
    if kind == 'on':
        try:
            hosts = s.vessel.hosts()
        except config.Error:
            hosts = {}
        out = [(name, f'{spec["address"]}  {" ".join(spec["roles"])}') for name, spec in hosts.items() if spec['ssh']]
        return out + [(name, 'local vessel (here, with its environment)') for name in remote.local_vessels()]
    if kind == 'stacks':
        return [(st.name, f'on {st.host}' + ('' if st.default else '  (not default)'))
                for st in stacks.parse(s.vessel.astro).values()]
    if kind == 'options':
        return [(o, f'{st.name}: {args}') for st in stacks.parse(s.vessel.astro).values() for o, args in st.options.items()]
    if kind == 'presets':
        return [(name, 'all topics but the excluded ones' if topics == 'all' else f'{len(topics)} topics')
                for name, topics in rec.presets(s.vessel).items()]
    if kind == 'check':
        f = field.Field(s.vessel)
        return [(p.stem, '') for p in sorted((f.dir / 'checklists').glob('*.md'))]
    if kind == 'bags':
        items = fetch.rows(fetch.search(s.vessel, config.this_host(), say=lambda line: None))
        return list({b['id']: (b['id'], f'{b["host"]}  {fetch.size_text(b["size"])}') for b in items}.values())
    raise config.Error(f'no choices {kind!r} ({", ".join(CHOICES)})')


def print_choices(s, kind, here):
    """`astro _choices KIND`: one VALUE<TAB>about line each. Tests and results live on astro.home, so outside
    ROS those are asked there."""
    home = s.vessel.astro.get('home') if kind in ('test', 'apply') else None
    if forwards('test', here, config.have_ros(), home):
        _, argv, env = remote.home_words(s.vessel, str(home), ['_choices', kind], False)
        done = subprocess.run(argv, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        sys.stdout.write(done.stdout)
        return done.returncode
    for value, about in choices(s, kind):
        print(f'{value}\t{about}' if about else value)
    return 0


def stack_lines(s):
    """The stacks table for astro status ([] without stacks, or when the caller already showed them)."""
    if os.environ.get('ASTRO_NO_STACKS') == '1' or not s.vessel.astro.get('stacks'):
        return []
    try:
        return stacks.table(stacks.rows(s.vessel))
    except config.Error as error:
        return [f'({error})']


# ---- verbs ---------------------------------------------------------------------------------------

def cmd_test(s, args):
    spec = s.spec
    if not args.name:
        nxt = results.next_step(s.rows()).split()
        args.name = choose('Pick a test to run (ladder order; * is next):', choices(s, 'test'),
                           nxt[-1] if nxt[:2] == ['astro', 'test'] else None)
    if args.name == 'list':
        width = max(map(len, spec.tests), default=0)
        for name in spec.order():
            test = spec.tests[name]
            print(f'{name:<{width}}  {spec.layer(name):<16} {test["type"]:<7} {test.get("axis") or ""}')
        for group, items in spec.groups.items():
            print(f'group {group}: {" ".join(items)}')
        spaces = ', '.join(f'{k} (amp {v.get("amp")})' for k, v in spec.spaces.items())
        print(f'spaces: {spaces}')
        return 0
    if args.name == 'status':
        rows = s.rows()
        width = max((len(r[0]) for r in rows), default=0)
        step = results.next_step(rows)
        for name, state, result in rows:
            when = f'{result.get("date", "")} {result.get("time", "")}' if result else ''
            summary = (result or {}).get('summary') or (result or {}).get('reason') or ''
            arrow = '->' if step.split()[-1:] == [name] else '  '
            print(f'{arrow} {name:<{width}}  {state:<8} {spec.layer(name):<16} {when:<19} {summary}'[:200])
        print(f'next: {step}')
        return 0
    need_ros('test')
    try:
        names = spec.expand(args.name)
    except KeyError:
        raise config.Error(f'no test or group {args.name!r} (astro test list)') from None
    if not s.no_autostart:
        code = stacks.ensure(s.vessel, s.root, f'test {args.name}', test_needs(s, args.name))
        if code:
            return code
    from .engine.runner import Runner
    ctx = procedures.Context(spec, s.vessel.state(), s.root, s.events, s.vessel.sim_time(), Runner)
    ctx.software = s.software()
    group = args.name not in spec.tests
    waiting = []                          # (test, its changes): measured here or before, not yet applied
    for name in names:
        if group:
            state, defaults = s.state(), spec.defaults()
            result = results.latest(s.root, name)
            status_now = results.classify(result, state, defaults)
            if status_now == 'measured' and result.get('changes'):
                waiting.append((name, result['changes']))
            if status_now in ('measured', 'applied'):
                continue                  # already measured on the current state
            blocking = [w for w, changes in waiting if any(results.touches(c['key'], spec.tests[name].get('upstream'))
                                                           for c in changes)]
            if blocking:
                print(f'apply {" and ".join(blocking)} first ({name} depends on them): '
                      + '; '.join(f'astro apply {w}' for w in blocking)
                      + f', then run astro test {args.name} again (it resumes at {name})')
                return 0
        # a test with `rec: PRESET` records a bag while it runs, unless one is recording already
        preset = spec.tests[name].get('rec')
        recording = bool(preset) and not rec.running(s.root)
        if recording:
            rec.start(s.vessel, [str(preset), name])
        bag = (rec.running(s.root) or (None, None))[1]       # the bag this test lands in, started here or not
        path = None
        try:
            path = procedures.run(ctx, name, args.space, args.amp)
            done = config.load_yaml(path) or {}
            if done.get('changes'):
                waiting.append((name, done['changes']))
        except procedures.Abort as error:
            print(f'{name}: aborted: {error}', file=sys.stderr)
            field.auto(s.vessel, 'test', name, str(error))
            return 1
        except KeyboardInterrupt:
            print(f'\n{name}: stopped, neutral sent', file=sys.stderr)
            return 130
        finally:
            if recording:
                rec.stop(s.vessel)
            if bag and path:                  # the result's quality problems go into the bag's ISSUES.yaml
                done = config.load_yaml(path) or {}
                quality = done.get('quality') or {}
                rec.note_issues(bag, f'astro test {name}', [dict(what=p['what'], fix=p['fix'])
                                                            for p in quality.get('problems') or []],
                                result=str(path), quality=quality.get('score'), summary=done.get('summary'))
    if waiting:                           # what this run measured comes first: apply it before anything else
        print(f'next: astro apply {waiting[-1][0]}')
        return 0
    rows = s.rows()
    print(f'next: {results.next_step(rows)}')
    return 0


def find_result(s, what):
    path = Path(what)
    if path.suffix == '.yaml' and path.exists():
        data = config.load_yaml(path)
        return dict(data, path=str(path))
    result = results.latest(s.root, what)
    if not result:
        raise config.Error(f'no result for {what!r} under {s.root} (astro test {what})')
    return result


def cmd_apply(s, args):
    if not args.result:
        options = choices(s, 'apply')
        if not options:
            print('nothing measured waits to be applied (astro test status)')
            return 0
        args.result = choose('Pick a result to apply (measured, not yet applied):', options)
    result = find_result(s, args.result)
    name = result['test']
    if result.get('status') != 'done':
        raise config.Error(f'{name}: the latest run was {result.get("status")} ({result.get("reason", "")}); '
                           f'run astro test {name} again')
    if not result.get('changes'):
        print(f'{name}: nothing to apply')
        return 0
    spec, state_path = s.spec, s.vessel.state()
    defaults = spec.defaults()
    status_now = results.classify(result, s.state(), defaults)
    if status_now == 'stale':
        print(f'warning: {name} ran on a state whose upstream keys have changed since (stale)')
    if status_now == 'applied':
        print(f'note: {name} ({result["time"]}) was applied before; applying it again')
    live = list(result.get('live') or [])
    try:
        backup, diffs = applying.apply(state_path, result['changes'], defaults, s.vessel.astro.get('check'),
                                       spec.axes, live, test=name, result=result['path'])
    except applying.Refused as error:
        log.write(s.root, 'apply', f'{name}  refused by the check, state unchanged', s.events)
        field.auto(s.vessel, 'apply', name, str(error))
        raise config.Error(str(error)) from None
    layer = result.get('layer') or ''
    if backup is None:
        print(f'  {layer}: unchanged ({state_path} already holds these values)')
        results.mark_applied(result['path'], '')
        print(f'next: {results.next_step(s.rows())}')
        return 0
    keys = '; '.join(f'{key} {procedures.fmt(old)} -> {procedures.fmt(new)}' for key, old, new in diffs)
    log.write(s.root, 'apply', f'{name}  {keys}  backup {backup.name}', s.events)
    print(f'  {layer}: written to {state_path} (astro undo can reverse it)')
    results.mark_applied(result['path'], backup.name)
    for line in applying.set_live(live, state_path, defaults) if config.have_ros() else (
            ['not live: no ROS here; applies at the next launch'] if live else []):
        print('  ' + line)
    if result.get('restart'):
        print(f'  to use this, restart: {result["restart"]}')
    print(f'next: {results.next_step(s.rows())}')
    return 0


def when(entry, today=None):
    """An apply's time: the clock time today, else the date too."""
    day, _, clock_time = entry['time'].partition(' ')
    return clock_time if day == f'{today or datetime.date.today():%Y-%m-%d}' else entry['time']


def shown(value):
    return 'unset' if value is None else procedures.fmt(value)


def short(value, width=24):
    text = shown(value)
    return text if len(text) <= width else text[:width - 1] + '\u2026'


def undo_rows(entries):
    """[(value, about)]: one row per apply, newest first; the value its test name then its time (unique),
    the about the keys it changed and how many."""
    out, seen = [], set()
    for entry in entries:
        value = f'{entry.get("test") or "?"}  {when(entry)}'
        while value in seen:
            value += "'"
        seen.add(value)
        keys = [f'{k["label"]} {short(k["old"])} \u2192 {short(k["new"])}' for k in entry['keys']]
        about = keys[0] + (f'  (+{len(keys) - 1} more)' if len(keys) > 1 else '') if keys else ''
        about += f'  ({len(keys)} key{"s" if len(keys) != 1 else ""})'
        out.append((value, about + (f'  [undone {when(dict(entry, time=entry["undone"]))}]'
                                    if entry.get('undone') else '')))
    return out


def applied_at(entry, today=None):
    """When an apply was made, in words: 14:12 today, else 2026-11-23 14:12."""
    day, _, clock_time = entry['time'].partition(' ')
    return f'{clock_time[:5]} today' if day == f'{today or datetime.date.today():%Y-%m-%d}' \
        else f'{day} {clock_time[:5]}'


def ask(question):
    try:
        return input(f'{question} [y/N] ').strip().lower() in ('y', 'yes')
    except EOFError:
        return False


def cmd_undo(s, args):
    state_path = s.vessel.state()
    entries = applying.history(state_path)
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    if args.listing or not (args.last or tty):
        for value, about in undo_rows(entries):
            print(f'  {value}  {about}')
        if not entries:
            print(f'no applies recorded next to {state_path}')
        return 0 if args.listing else 2
    pending = [e for e in entries if not e.get('undone')]
    if not pending:
        print('nothing to undo: no apply recorded that is not undone yet (astro undo --list)')
        return 0
    if args.last:
        entry = pending[0]
    else:
        rows = undo_rows(pending)
        picked = choose('Pick an apply to undo (newest first):', rows)
        entry = pending[[v for v, _ in rows].index(picked)]
    defaults = s.spec.defaults()
    name, at = entry.get('test') or '?', when(entry)
    result = f' (result {entry["result"]})' if entry.get('result') else ''
    print(f'Undo: {name}, applied {applied_at(entry)}{result}')
    skip = []
    planned = applying.plan(state_path, entry, defaults)
    width = max((len(item['label']) for item, _, _ in planned), default=0)
    for item, now, later in planned:
        print(f'  {item["label"]:<{width}}   now {shown(now)}  \u2192  back to {shown(item["old"])}')
        if now == item['new']:
            continue
        since = ', '.join(f'{o.get("test") or "?"} ({when(o)})' for o in later) or 'a hand edit'
        print(f'    changed since by {since}; this apply wrote {shown(item["new"])}')
        if args.last or not tty:
            print('    skipped (run astro undo on a terminal to choose)')
            skip.append(item['label'])
        elif not ask('    restore it anyway?'):
            print('    skipped')
            skip.append(item['label'])
    if not args.last and not ask(f'Restore these values on the robot ({state_path.name})?'):
        print('nothing changed')
        return 1
    try:
        done = applying.revert(state_path, entry, defaults, s.vessel.astro.get('check'), skip)
    except applying.Refused as error:
        log.write(s.root, 'apply', f'not undone: {name} (applied {at})  refused by the check, state unchanged',
                  s.events)
        raise config.Error(str(error)) from None
    if not done:
        print('  nothing to restore: the state already holds those values')
        return 0
    keys = '; '.join(f'{label} \u2192 {shown(value)}' for label, _, value in done)
    log.write(s.root, 'apply', f'undone: {name} (applied {at}): {keys}', s.events)
    if not skip:
        results.unmark_applied(s.root, entry['backup'])
    print(f'undone: {name} (applied {applied_at(entry).removesuffix(" today")}), restored in {state_path}')
    live = entry.get('live') or []
    for line in applying.set_live(live, state_path, defaults) if config.have_ros() else (
            ['not live: no ROS here; applies at the next launch'] if live else []):
        print('  ' + line)
    return 0


def cmd_bundle(s, args):
    day = log.day_dir(s.root)
    out = day / f'bundle-{datetime.datetime.now():%H%M%S}.tgz'
    day.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, 'w:gz') as tar:
        state = s.vessel.state()
        if state.exists():
            tar.add(state, arcname=f'state/{state.name}')
        try:
            tar.add(s.vessel.path('tests'), arcname='state/tests.yaml')
        except config.Error:
            pass
        for item in ('log.txt', 'results'):
            if (day / item).exists():
                tar.add(day / item, arcname=f'{day.name}/{item}')
        if args.bags and (day / 'bags').exists():
            tar.add(day / 'bags', arcname=f'{day.name}/bags')
    print(out)
    nxt = s.vessel.astro.get('bundle_next')
    if nxt:
        print(str(nxt).replace('{bundle}', str(out)))
    return 0


def cmd_status(s, args):
    while True:
        try:
            spec = s.spec
            nxt = results.next_step(s.rows())
        except config.Error as error:
            spec, nxt = None, str(error)
        text = status.page(s.vessel, spec, nxt, stack_lines=stack_lines(s))
        if not args.watch:
            print(text)
            return 0
        sys.stdout.write('\033[H\033[J' + text + '\n')
        sys.stdout.flush()
        time.sleep(1.0)


def cmd_log(s, args):
    lines = log.read(s.root)
    for line in lines[-args.n:] if args.n else lines:
        print(line)
    return 0


def entry_host(vessel, command, here, me, inside):
    """(host, spec) a help.yaml command with `host:` is forwarded to, or None to run it here: --here, no host,
    or already inside a container on that host."""
    if here or not command.get('host'):
        return None
    host, spec = remote.place(vessel, command['host'])
    return None if host == me and inside else (host, spec)


def fill(module, command, args, pick=None):
    """ARGS with each missing required positional param that has choices_from picked (on a terminal; else the
    choices are printed and NoChoice raised)."""
    pick = pick or choose
    given = [a for a in args if not a.startswith('-')]
    args = list(args)
    for i, param in enumerate(p for p in command['params'] if p['form'] == 'positional'):
        if i < len(given):
            continue
        if not (param.get('choices_from') and module.is_required(param)):
            break
        options = module.run_choices(param['choices_from'], env=dispatch_env())
        value = pick(f'Pick the {param["name"]} for astro {command["name"]}'
                     + (f' ({param["description"]})' if param.get('description') else '') + ':',
                     options, current=shown_default(module, param))
        args.append(value)
        given.append(value)
    return args


def shown_default(module, param):
    return module.shown(param['default'], param) if 'default' in param else None


def dispatch_env():
    env = dict(os.environ)
    env.setdefault('ROS_WS', str(config.WS))
    try:                                  # the vessel's env, as for the built-in verbs
        env.update(config.Vessel().env())
    except config.Error:
        pass
    return env


def nouns(packages, word):
    """[(package, command)] whose name starts with WORD and a space: the subcommands of a noun."""
    return [(p, c) for p in packages for c in p['commands'] if c['name'].startswith(word + ' ')]


def dispatch(argv):
    """`astro NAME [args]`: the help.yaml command whose name is the longest prefix of argv. A noun alone
    (astro thrusters) lists its subcommands; a command with `host:` runs there, forwarded like the ROS verbs."""
    module = help_module()
    packages, _ = module.discover()
    best = None
    for package in packages:
        for command in package['commands']:
            words = command['name'].split()
            if argv[:len(words)] == words and (best is None or len(words) > len(best[0])):
                best = (words, command)
    if not best:
        subs = nouns(packages, argv[0])
        if not subs:
            print(f'astro: no verb or help.yaml command {argv[0]!r} (astro help)', file=sys.stderr)
            return 2
        width = max(len(c['name']) for _, c in subs)
        for _, command in subs:
            print(f'  astro {command["name"]:<{width}}  {command["summary"]}')
        return 0
    words, command = best
    rest = argv[len(words):]
    if any(a in ('-h', '--help') for a in rest):     # help asked: never run the command
        package = next(p['package'] for p in packages if command in p['commands'])
        sys.stdout.write(module.text(packages, exact=(package, command['name'])))
        return 0
    if command['run'].startswith('astro '):
        print(f'astro: {command["name"]} is an astro verb', file=sys.stderr)
        return 2
    here = os.environ.get('ASTRO_HERE') == '1'
    stacked = command['needs'] or command['conflicts']
    off = '--here' in rest or os.environ.get('ASTRO_NO_AUTOSTART') == '1'   # --here: no needs/conflicts either
    if (command['host'] or stacked) and '--here' in rest:
        rest, here = [a for a in rest if a != '--here'], True
    try:
        rest = fill(module, command, rest)
        if stacked and not off:           # its stacks first: conflicts stopped, needs started
            vessel = config.Vessel()
            code = stacks.ensure(vessel, vessel.data_root(), command['name'], command['needs'], command['conflicts'])
            if code:
                return code
        target = None
        if command['host'] and not here:
            vessel = config.Vessel()
            target = entry_host(vessel, command, here, config.this_host(), remote.in_container())
    except NoChoice:
        return 2
    except Cancelled:
        print(f'astro {command["name"]}: cancelled, nothing run', file=sys.stderr)
        return 1
    except config.Error as error:
        print(f'astro {command["name"]}: {error}', file=sys.stderr)
        return 1
    line = command['run'] + ''.join(' ' + shlex.quote(a) for a in rest)
    if target:
        host, spec = target
        dim = sys.stderr.isatty() and 'NO_COLOR' not in os.environ
        print(f'\033[2m(on {host})\033[0m' if dim else f'(on {host})', file=sys.stderr)
        tty = remote._tty()
        try:
            if host == config.this_host():           # this machine, outside its container: exec into it
                argv_, env = remote.local_words(vessel, spec['container'], line, tty)
            else:
                argv_, env = remote.ssh_command(vessel, host, spec, [line], tty), None
            return run_logged(command['name'], argv_, env, plain=command.get('interactive'))
        except config.Error as error:
            print(f'astro {command["name"]}: {error}', file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130
    return run_logged(command['name'], ['bash', '-c', line], dispatch_env(), plain=command.get('interactive'))


def run_logged(name, argv, env=None, plain=False):
    """Run a help.yaml command with everything it prints also kept in <data>/<date>/logs/NAME-HHMMSS.log (a
    pty through `script`, so colours, redraws and Ctrl-C behave as before), and say where in the day's log.
    Without `script` or a writable data dir it runs unlogged; PLAIN (an interactive entry: raw keys, full
    screen) runs straight in the terminal, its start still noted in the day's log."""
    import shutil
    if plain:
        try:
            log.write(Path(config.Vessel().data_root()), 'cmd', f'{name} (interactive, not logged)', echo=False)
        except Exception:
            pass
        try:
            return subprocess.call(argv, env=env)
        except KeyboardInterrupt:
            return 130
    try:
        root = Path(config.Vessel().data_root())
    except Exception:                     # no vessel (or a stand-in without a data dir): the user's state dir
        root = Path.home() / '.local' / 'state' / 'astro'
    try:
        directory = log.day_dir(root) / 'logs'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f'{name.replace(" ", "-")}-{datetime.datetime.now():%H%M%S}.log'
        log.write(root, 'cmd', f'{name} -> logs/{path.name}', echo=False)
    except OSError:
        path = None
    try:
        if path and shutil.which('script') and sys.stdout.isatty():
            return subprocess.call(['script', '-qefc', shlex.join(argv), str(path)], env=env)
        if path:                          # no terminal: tee, keeping the command's exit status
            return subprocess.call(['bash', '-c', f'{shlex.join(argv)} 2>&1 | tee -a {shlex.quote(str(path))}; '
                                    'exit ${PIPESTATUS[0]}'], env=env)
        return subprocess.call(argv, env=env)
    except KeyboardInterrupt:
        return 130


def parser():
    p = argparse.ArgumentParser(prog='astro', add_help=False)
    sub = p.add_subparsers(dest='verb')
    on = sub.add_parser('on', add_help=False)
    on.add_argument('host', nargs='?')
    on.add_argument('cmd', nargs=argparse.REMAINDER)
    u = sub.add_parser('up', add_help=False)
    u.add_argument('names', nargs='*')
    u.add_argument('--with', dest='options', action='append', default=[])
    u.add_argument('--force', action='store_true')    # start even when it runs outside astro
    sub.add_parser('down', add_help=False).add_argument('names', nargs='*')
    g = sub.add_parser('logs', add_help=False)
    g.add_argument('name', nargs='?')
    g.add_argument('-f', dest='follow', action='store_true')
    sub.add_parser('status', add_help=False).add_argument('--watch', action='store_true')
    r = sub.add_parser('rec', add_help=False)
    r.add_argument('action', choices=('start', 'stop', 'ls', 'fetch'))
    r.add_argument('words', nargs='*')
    m = sub.add_parser('mark', add_help=False)
    m.add_argument('text', nargs='+')
    m.add_argument('--at')                       # a forwarded or pending mark's original time (ISO)
    sub.add_parser('log', add_help=False).add_argument('-n', type=int, default=0)
    t = sub.add_parser('test', add_help=False)
    t.add_argument('name', nargs='?')
    t.add_argument('--space', default='large')
    t.add_argument('--amp', type=float)
    sub.add_parser('apply', add_help=False).add_argument('result', nargs='?')
    u = sub.add_parser('undo', add_help=False)
    u.add_argument('--last', action='store_true')
    u.add_argument('--list', dest='listing', action='store_true')
    sub.add_parser('bundle', add_help=False).add_argument('--bags', action='store_true')
    sim = sub.add_parser('sim', add_help=False)
    sim.add_argument('action')   # passed to the backend (up, down, status, check, restart …)
    sim.add_argument('args', nargs=argparse.REMAINDER)
    c = sub.add_parser('check', add_help=False)
    c.add_argument('name', nargs='?')
    c.add_argument('--list', dest='listing', action='store_true')
    i = sub.add_parser('issue', add_help=False)
    i.add_argument('text', nargs='+')
    i.add_argument('--label', dest='labels', action='append', default=[])
    sub.add_parser('sync', add_help=False).add_argument('--pack')   # DATE: the day's tar for a remote sync
    sub.add_parser('show', add_help=False).add_argument('date', nargs='?')
    sub.add_parser('docs', add_help=False)
    sub.add_parser('clock', add_help=False).add_argument('--fix', action='store_true')
    return p


def rec_fetch(args):
    """`astro rec fetch ...` (fetch.main), with astro's one-line failures."""
    try:
        return fetch.main(config.Vessel(), args, choose)
    except NoChoice:
        return 2
    except (Cancelled, fetch.Cancelled):
        print('astro rec fetch: cancelled, nothing copied', file=sys.stderr)
        return 1
    except config.Error as error:
        print(f'astro rec fetch: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help'):
        return help_module().main(['--list'])
    verb = argv[0]
    here = os.environ.get('ASTRO_HERE') == '1'
    if verb == '_choices':
        if '--here' in argv:
            argv, here = [a for a in argv if a != '--here'], True
        try:
            return print_choices(Session(), argv[1] if argv[1:] else '', here)
        except config.Error as error:
            print(f'astro _choices: {error}', file=sys.stderr)
            return 1
    if verb not in VERBS:
        return dispatch(argv)
    if verb == 'help':
        return help_module().main(argv[1:])
    if any(a in ('-h', '--help') for a in argv[1:]) and (verb != 'on' or argv[1:2] in (['-h'], ['--help'])):
        sub = argv[1] if verb == 'rec' and argv[1] in ('start', 'stop', 'ls', 'fetch') else None
        return verb_help(f'rec {sub}' if sub else verb)
    if verb == 'rec' and argv[1:2] == ['fetch']:          # runs here: it searches every computer itself
        return rec_fetch(argv[2:])
    flag_here = '--here' in argv
    if verb in HOME_VERBS and flag_here:
        argv, here = [a for a in argv if a != '--here'], True
    args = parser().parse_args(argv)
    s = Session()
    s.no_autostart = s.no_autostart or flag_here
    try:
        if verb in HOME_VERBS and not here and not config.have_ros():
            home = s.vessel.astro.get('home')
            if forwards(verb, here, False, home):
                if verb == 'status' and not args.watch:      # the stacks from here (as ssh reaches them)
                    shown = stack_lines(s)
                    if shown:
                        print('\n'.join(status.stacks_block(shown)))
                    return forward(s.vessel, str(home), argv, env={'ASTRO_NO_STACKS': '1'} if shown else None)
                if verb == 'test' and args.name not in (None, 'list', 'status') and not s.no_autostart:
                    code = stacks.ensure(s.vessel, s.root, f'test {args.name}', test_needs(s, args.name))
                    if code:
                        return code
                    s.no_autostart = True     # resolved here, once: not again on home
                return forward(s.vessel, str(home), argv,
                               env={'ASTRO_NO_AUTOSTART': '1'} if s.no_autostart else None)
        if verb in HEADER and not (verb == 'test' and args.name == 'list'):
            inst = f' · instance {s.vessel.instance}' if getattr(s.vessel, 'is_sim', False) else ''
            print(f'astro {verb} · vessel {s.vessel.name}{inst} · state {s.vessel.state()} · data {s.root}')
        if verb == 'on':
            return remote.on(args.host or choose('Pick a host to open a shell on:', choices(s, 'on'),
                                                 config.this_host()), args.cmd)
        if verb == 'sim':
            return remote.sim(args.action, args.args)
        if verb == 'up':
            return stacks.main_up(s.vessel, s.root, args)
        if verb == 'down':
            return stacks.down(s.vessel, s.root, args.names)
        if verb == 'logs':
            return stacks.logs(s.vessel, args.name or choose('Pick a stack to show its log:', choices(s, 'stacks')),
                               args.follow)
        if verb == 'check':
            return field.check(s.vessel, args.name or choose('Pick a checklist to go through:', choices(s, 'check')),
                               args.listing)
        if verb == 'clock':
            return clock.main(s.vessel, config.this_host(), args.fix)
        if verb == 'issue':
            return field.issue(s.vessel, ' '.join(args.text), args.labels)
        if verb == 'sync':
            if args.pack:
                return field.pack(s.vessel, args.pack, sys.stdout.buffer)
            return field.sync(s.vessel, field_home(s.vessel, here))
        if verb == 'show':
            return field.show(s.vessel, args.date, field_home(s.vessel, here))
        if verb == 'docs':
            from . import docs
            return docs.main(s.vessel)
        if verb == 'mark':
            log.write(s.root, 'mark', ' '.join(args.text), s.events,
                      when=remote.mark_time(args.at) if args.at else None)
            return 0
        if verb == 'rec':
            if args.action == 'ls':
                return rec.ls(s.vessel)
            need_ros('rec')
            return rec.start(s.vessel, args.words) if args.action == 'start' else rec.stop(s.vessel)
        return {'test': cmd_test, 'apply': cmd_apply, 'undo': cmd_undo, 'bundle': cmd_bundle,
                'status': cmd_status, 'log': cmd_log}[verb](s, args)
    except NoChoice:
        return 2
    except Cancelled:
        print(f'astro {verb}: cancelled, nothing run', file=sys.stderr)
        return 1
    except config.Error as error:
        print(f'astro {verb}: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
