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

from . import config, rec, remote, status
from .engine import apply as applying
from .engine import log, procedures, results

VERBS = ('on', 'status', 'rec', 'mark', 'log', 'test', 'apply', 'undo', 'bundle', 'sim', 'help')
HELP = config.WS / '.devcontainer' / 'astro-help.py'


def help_module():
    spec = importlib.util.spec_from_file_location('astro_help', HELP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verb_help(verb):
    module = help_module()
    packages, _ = module.discover()
    color = sys.stdout.isatty() and 'NO_COLOR' not in os.environ
    sys.stdout.write(module.text(packages, color=color, exact=('astro', verb)))
    return 0


def need_ros(verb):
    if not config.have_ros():
        raise config.Error(f'astro {verb} needs ROS: run it in the dev container (astro on HOST astro {verb} ...)')


class Session:
    """The vessel, its tests file and state, loaded on first use."""

    def __init__(self):
        self._vessel = self._spec = None

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


# ---- verbs ---------------------------------------------------------------------------------------

def cmd_test(s, args):
    spec = s.spec
    if args.name == 'list':
        width = max(map(len, spec.tests), default=0)
        for name in spec.order():
            test = spec.tests[name]
            print(f'{name:<{width}}  {spec.layer(name):<16} {test["type"]:<7} {test.get("axis") or ""}')
        for group, items in spec.groups.items():
            print(f'group {group}: {" ".join(items)}')
        print(f'spaces: {", ".join(f"{k} (amp {v.get("amp")})" for k, v in spec.spaces.items())}')
        return 0
    if args.name == 'status':
        rows = s.rows()
        width = max((len(r[0]) for r in rows), default=0)
        for name, state, result in rows:
            when = f'{result.get("date", "")} {result.get("time", "")}' if result else ''
            summary = (result or {}).get('summary') or (result or {}).get('reason') or ''
            print(f'{name:<{width}}  {state:<8} {spec.layer(name):<16} {when:<19} {summary}'[:200])
        print(f'next: {results.next_step(rows)}')
        return 0
    need_ros('test')
    try:
        names = spec.expand(args.name)
    except KeyError:
        raise config.Error(f'no test or group {args.name!r} (astro test list)') from None
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
        try:
            path = procedures.run(ctx, name, args.space, args.amp)
            done = config.load_yaml(path) or {}
            if done.get('changes'):
                waiting.append((name, done['changes']))
        except procedures.Abort as error:
            print(f'{name}: aborted: {error}', file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print(f'\n{name}: stopped, neutral sent', file=sys.stderr)
            return 130
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
                                       spec.axes, live)
    except applying.Refused as error:
        log.write(s.root, 'apply', f'{name}  refused by the check, state unchanged', s.events)
        raise config.Error(str(error)) from None
    layer = result.get('layer') or ''
    if backup is None:
        print(f'  {layer}: unchanged ({state_path} already holds these values)')
        results.mark_applied(result['path'], '')
        print(f'next: {results.next_step(s.rows())}')
        return 0
    keys = '; '.join(f'{key} {procedures.fmt(old)} -> {procedures.fmt(new)}' for key, old, new in diffs)
    log.write(s.root, 'apply', f'{name}  {keys}  backup {backup.name}', s.events)
    print(f'  {layer}: written to {state_path} (astro undo restores {backup.name})')
    results.mark_applied(result['path'], backup.name)
    for line in applying.set_live(live, state_path, defaults) if config.have_ros() else (
            ['not live: no ROS here; applies at the next launch'] if live else []):
        print('  ' + line)
    if result.get('restart'):
        print(f'  restart {result["restart"]} to use this')
    print(f'next: {results.next_step(s.rows())}')
    return 0


def cmd_undo(s, args):
    state_path = s.vessel.state()
    try:
        name, live = applying.undo(state_path, s.vessel.astro.get('check'))
    except applying.Refused as error:
        raise config.Error(str(error)) from None
    log.write(s.root, 'apply', f'undo  {state_path.name} restored from {name}', s.events)
    results.unmark_applied(s.root, name)
    if live and config.have_ros():
        for line in applying.set_live(live, state_path, s.spec.defaults()):
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
    return 0


def cmd_status(s, args):
    while True:
        try:
            spec = s.spec
            nxt = results.next_step(s.rows())
        except config.Error as error:
            spec, nxt = None, str(error)
        text = status.page(s.vessel, spec, nxt)
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


def dispatch(argv):
    """`astro NAME [args]`: the help.yaml command whose name is the longest prefix of argv."""
    module = help_module()
    packages, _ = module.discover()
    best = None
    for package in packages:
        for command in package['commands']:
            words = command['name'].split()
            if argv[:len(words)] == words and (best is None or len(words) > len(best[0])):
                best = (words, command)
    if not best:
        print(f'astro: no verb or help.yaml command {argv[0]!r} (astro help)', file=sys.stderr)
        return 2
    words, command = best
    if any(a in ('-h', '--help') for a in argv[len(words):]):     # help asked: never run the command
        package = next(p['package'] for p in packages if command in p['commands'])
        sys.stdout.write(module.text(packages, exact=(package, command['name'])))
        return 0
    if command['run'].startswith('astro '):
        print(f'astro: {command["name"]} is an astro verb', file=sys.stderr)
        return 2
    line = command['run'] + ''.join(' ' + shlex.quote(a) for a in argv[len(words):])
    env = dict(os.environ)
    env.setdefault('ROS_WS', str(config.WS))
    try:                                  # the vessel's env, as for the built-in verbs
        env.update(config.Vessel().env())
    except config.Error:
        pass
    return subprocess.call(['bash', '-c', line], env=env)


def parser():
    p = argparse.ArgumentParser(prog='astro', add_help=False)
    sub = p.add_subparsers(dest='verb')
    on = sub.add_parser('on', add_help=False)
    on.add_argument('host')
    on.add_argument('cmd', nargs=argparse.REMAINDER)
    sub.add_parser('status', add_help=False).add_argument('--watch', action='store_true')
    r = sub.add_parser('rec', add_help=False)
    r.add_argument('action', choices=('start', 'stop', 'ls'))
    r.add_argument('words', nargs='*')
    sub.add_parser('mark', add_help=False).add_argument('text', nargs='+')
    sub.add_parser('log', add_help=False).add_argument('-n', type=int, default=0)
    t = sub.add_parser('test', add_help=False)
    t.add_argument('name')
    t.add_argument('--space', default='large')
    t.add_argument('--amp', type=float)
    sub.add_parser('apply', add_help=False).add_argument('result')
    sub.add_parser('undo', add_help=False)
    sub.add_parser('bundle', add_help=False).add_argument('--bags', action='store_true')
    sim = sub.add_parser('sim', add_help=False)
    sim.add_argument('action', choices=('up', 'down', 'status'))
    sim.add_argument('args', nargs=argparse.REMAINDER)
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help'):
        return help_module().main(['--list'])
    verb = argv[0]
    if verb not in VERBS:
        return dispatch(argv)
    if verb == 'help':
        return help_module().main(argv[1:])
    if any(a in ('-h', '--help') for a in argv[1:]) and (verb != 'on' or argv[1:2] in (['-h'], ['--help'])):
        sub = argv[1] if verb == 'rec' and argv[1] in ('start', 'stop', 'ls') else None
        return verb_help(f'rec {sub}' if sub else verb)
    args = parser().parse_args(argv)
    s = Session()
    try:
        if verb == 'on':
            return remote.on(args.host, args.cmd)
        if verb == 'sim':
            return remote.sim(args.action, args.args)
        if verb == 'mark':
            log.write(s.root, 'mark', ' '.join(args.text), s.events)
            return 0
        if verb == 'rec':
            if args.action == 'ls':
                return rec.ls(s.vessel)
            need_ros('rec')
            return rec.start(s.vessel, args.words) if args.action == 'start' else rec.stop(s.vessel)
        return {'test': cmd_test, 'apply': cmd_apply, 'undo': cmd_undo, 'bundle': cmd_bundle,
                'status': cmd_status, 'log': cmd_log}[verb](s, args)
    except config.Error as error:
        print(f'astro {verb}: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
