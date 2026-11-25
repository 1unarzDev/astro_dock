"""`astro on NAME [cmd...]` and `astro sim VERB`: where a command runs.

NAME is a registry host of the vessel (exact name, else a unique substring; any vessel's registry when the
current one has none) reached over ssh, then `docker exec` into its dev container; or a vessel profile
whose registry has no ssh host (the sim), run on this machine with that vessel's environment: inside the
registry host's `container:` when it names one, else this checkout's dev container, else (already in a
container with ROS) right here."""
import contextlib
import datetime
import fcntl
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

from . import config
from .engine import log


def _tty():
    return sys.stdin.isatty() and sys.stdout.isatty()


def _members(root):
    return sorted(d.name for d in Path(root).iterdir() if (d / 'profile.yaml').exists()) if root else []


def find_host(name):
    """(vessel, host name, spec) for NAME among the registry hosts."""
    try:
        here = config.Vessel()
        vessels = [here]
    except config.Error:
        here, vessels = None, []
    root = config.profiles_root()
    for member in _members(root):
        if not here or member != here.name:
            try:
                vessels.append(config.Vessel(member))
            except config.Error:
                continue
    for exact in (True, False):
        for vessel in vessels:
            hosts = vessel.hosts()
            found = [h for h in hosts if (h == name if exact else name in h) and hosts[h]['ssh']]
            if len(found) == 1:
                return vessel, found[0], hosts[found[0]]
            if len(found) > 1:
                raise config.Error(f'{name!r} matches several hosts of {vessel.name}: {", ".join(found)}')
    listed = sorted({h for v in vessels for h, s in v.hosts().items() if s['ssh']})
    raise config.Error(f'no ssh host matches {name!r} (hosts: {", ".join(listed) or "none"}; '
                       f'local vessels: {", ".join(local_vessels()) or "none"})')


def local_vessels():
    out = []
    for member in _members(config.profiles_root()):
        try:
            vessel = config.Vessel(member)
        except config.Error:
            continue
        hosts = vessel.hosts()
        if vessel.registry and hosts and not any(spec['ssh'] for spec in hosts.values()):
            out.append(member)
    return out


def ssh_base(vessel, host, spec):
    """(ssh words, target) reaching HOST's own shell: the operator key and the via jump host when set."""
    if not spec['address']:
        raise config.Error(f'{host} has no address in {vessel.registry}')
    target = f'{spec["user"]}@{spec["address"]}' if spec['user'] else spec['address']
    words = ['ssh', '-o', 'ConnectTimeout=8']   # an unreachable host fails in seconds, never hangs
    key_dir = vessel.hosts_kind.get('operator_key')
    if key_dir:
        base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / str(key_dir)
        if (base / 'id_ed25519').exists():
            words += ['-i', str(base / 'id_ed25519'), '-o', f'UserKnownHostsFile={base / "known_hosts"}']
    if spec['via']:
        via = vessel.hosts().get(spec['via'])
        if via and via['address']:
            words += ['-J', f'{via["user"]}@{via["address"]}' if via['user'] else via['address']]
    return words, target


def host_shell(vessel, host, spec, script):
    """argv running SCRIPT (bash) in HOST's own shell over ssh, outside its container (clocks, chrony)."""
    words, target = ssh_base(vessel, host, spec)
    return words + ['-o', 'BatchMode=yes', target, 'bash -c ' + shlex.quote(script)]


def container_find(vessel, spec):
    """(shell that sets c, the container word) naming a registry host's dev container in its own shell."""
    name = spec['container'] or (vessel.hosts_kind.get('container') or {}).get('name') \
        or config.compose_default_container()
    project = vessel.hosts_kind.get('compose_project')
    find = ''
    if not spec['container']:   # the dev service of the host's compose project, else any compose dev service, else the name
        dev = '--filter label=com.docker.compose.service=dev'
        if project:
            find = f'c=$(docker ps -q --filter label=com.docker.compose.project={shlex.quote(project)} {dev} | head -n1); '
        find += f'[ -n "$c" ] || c=$(docker ps -q {dev} | head -n1); '
    return find, (f'"${{c:-{name}}}"' if find else shlex.quote(name))


def ssh_command(vessel, host, spec, cmd, tty):
    words, target = ssh_base(vessel, host, spec)
    find, container = container_find(vessel, spec)
    words += ['-t'] if tty else []
    tz = '-e TZ="$(tail -n1 /etc/localtime 2>/dev/null)" '   # one clock: the container keeps its host's
    remote = f'{find}docker exec {"-it" if tty else "-i"} {tz}{container} bash'
    if cmd:
        remote += ' -c ' + shlex.quote(' '.join(cmd))
    return words + [target, remote]


def local_env(vessel, inside):
    """The vessel's variables; paths stay as written (~, workspace-relative) when they cross into a container."""
    env = vessel.env()
    if not inside:
        env.pop('ASTRO_DATA', None)               # the container resolves its own data root from the profile
        raw = vessel.state_raw() if vessel.astro.get('state') else ''
        for key in ('ASTRO_STATE', str(vessel.astro.get('state_env') or '')):
            if key in env and raw:
                env[key] = raw
    return env


def in_container():
    return Path('/.dockerenv').exists() or bool(os.environ.get('ROS_DISTRO'))


def local_words(vessel, container, cmd, tty):
    """(argv, env) running `bash -c cmd` (no cmd: a shell) with the vessel's environment on this machine:
    in CONTAINER, else this checkout's dev container, else right here when already in a container."""
    if not container and in_container():
        args = ['bash', '-c', cmd] if cmd else ['bash', '-i']
        return args, dict(os.environ, **local_env(vessel, True))
    if not container:
        container = config.dev_container()
        if not container:
            raise config.Error('no running dev container for this checkout (start it, or name one with '
                               f'container: on the {vessel.name} registry host)')
    words = ['docker', 'exec', '-it' if tty else '-i']
    if log.posix_tz():
        words += ['-e', f'TZ={log.posix_tz()}']      # one clock: the container keeps the host's
    for key, value in local_env(vessel, False).items():
        words += ['-e', f'{key}={value}']
    return words + [container, 'bash'] + (['-c', cmd] if cmd else []), None


def place(vessel, host):
    """(name, spec) of HOST in the vessel's registry: a host name, else the one host with that role."""
    hosts = vessel.hosts()
    if host in hosts:
        return host, hosts[host]
    found = [h for h, spec in hosts.items() if host in spec['roles']]
    if len(found) != 1:
        raise config.Error(f'{host!r} is {"several hosts" if found else "no host or role"} of {vessel.name} '
                           f'({", ".join(found or hosts) or "no registry"})')
    return found[0], hosts[found[0]]


def words(vessel, host, spec, cmd, tty=False):
    """(argv, env) running `bash -c cmd` in HOST's dev container: ssh + docker exec, or here (ssh: false)."""
    if spec['ssh']:
        return ssh_command(vessel, host, spec, [cmd] if cmd else [], tty), None
    return local_words(vessel, spec['container'], cmd, tty)


def home_words(vessel, home, args, tty, env=None):
    """(host name, argv, env) running `astro ARGS` on HOME (a registry host or role) in its dev container,
    with ENV's variables set. ASTRO_HERE=1 keeps it there."""
    host, spec = place(vessel, home)
    sets = ''.join(f'{k}={shlex.quote(str(v))} ' for k, v in (env or {}).items())
    cmd = f'ASTRO_HERE=1 {sets}astro' + ''.join(' ' + shlex.quote(a) for a in args)
    argv, env = words(vessel, host, spec, cmd, tty)
    return host, argv, env


# ---- forwarded marks: never lost ----------------------------------------------------------------------
# A forwarded mark is sent in the background (deliver_mark). When its ssh fails it is appended to a local
# pending file (ISO time, a tab, the text) and sent again, with its original time (`astro mark --at ISO`),
# after the next forwarded command that succeeds or by astro sync.

MARK_TIMEOUT = 20      # seconds a forwarded mark's ssh may take


def pending_path(vessel):
    base = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state')
    return base / 'astro' / f'{vessel.name}-marks.pending'


@contextlib.contextmanager
def _locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        yield handle


def queue_mark(vessel, at, text):
    path = pending_path(vessel)
    with _locked(path) as handle:
        handle.write(f'{at}\t{" ".join(str(text).split())}\n')
    return path


def send_mark(vessel, home, at, words):
    """True when `astro mark --at AT WORDS` ran on HOME (exit 0) within MARK_TIMEOUT s."""
    try:
        _, argv, env = home_words(vessel, home, ['mark', '--at', at, *words], False)
        done = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=MARK_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired, config.Error):
        return False
    return done.returncode == 0


def replay_marks(vessel, home):
    """Send the pending marks to HOME in order, keeping those from the first failure on. (sent, left)."""
    path = pending_path(vessel)
    if not path.exists():
        return 0, 0
    with _locked(path) as handle:
        rows = [line.rstrip('\n').split('\t', 1) for line in handle if '\t' in line]
        sent = 0
        for at, text in rows:
            if not send_mark(vessel, home, at, text.split()):
                break
            sent += 1
        handle.seek(0)
        handle.truncate()
        handle.writelines(f'{at}\t{text}\n' for at, text in rows[sent:])
    if not rows[sent:]:
        path.unlink(missing_ok=True)
    if sent:
        print(f'astro: sent {sent} pending mark{"s" if sent > 1 else ""} to {home}'
              + (f'; {len(rows) - sent} still pending in {path}' if rows[sent:] else ''), file=sys.stderr)
    return sent, len(rows) - sent


def deliver_mark(vessel, home, at, words):
    """The background half of a forwarded mark: send it, else keep it pending and say so in one line."""
    if send_mark(vessel, home, at, words):
        replay_marks(vessel, home)
        return 0
    path = queue_mark(vessel, at, ' '.join(words))
    print(f'astro mark: {home} not reachable; kept in {path} (sent on the next forwarded command or astro sync)',
          file=sys.stderr)
    return 1


def mark_time(at):
    """A mark's --at (ISO, with or without an offset) as a local naive datetime."""
    try:
        when = datetime.datetime.fromisoformat(at)
    except ValueError:
        raise config.Error(f'--at {at}: expected an ISO time (YYYY-MM-DDTHH:MM:SS[+HH:MM])') from None
    return when.astimezone().replace(tzinfo=None) if when.tzinfo else when


def on(name, cmd):
    tty = _tty()
    root = config.profiles_root()
    if name in _members(root) and name in local_vessels():
        vessel = config.Vessel(name)
        container = next((s['container'] for s in vessel.hosts().values() if s['container']), '')
        argv, env = local_words(vessel, container, ' '.join(cmd), tty)
        if env is not None:
            os.execvpe(argv[0], argv, env)
        return subprocess.call(argv)
    vessel, host, spec = find_host(name)
    return subprocess.call(ssh_command(vessel, host, spec, cmd, tty))


VAR = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)')


def expand(text, env=None):
    """~ and $VAR / ${VAR} / ${VAR:-default}, from env (default os.environ)."""
    env = os.environ if env is None else env

    def one(match):
        name = match.group(1) or match.group(3)
        value = env.get(name, '')
        return value if value else (match.group(2) or '')
    return os.path.expanduser(VAR.sub(one, str(text)))


def sim_vessel():
    wanted = os.environ.get('ASTRO_SIM')
    if wanted:
        return config.Vessel(wanted)
    found = []
    for member in _members(config.profiles_root()):
        try:
            vessel = config.Vessel(member)
        except config.Error:
            continue
        if vessel.astro.get('sim'):
            found.append(vessel)
    if len(found) != 1:
        raise config.Error('no vessel profile has astro.sim (the sim backend command: CMD up|down|status)'
                           if not found else f'several profiles have astro.sim ({", ".join(v.name for v in found)}); '
                                             'set ASTRO_SIM=NAME')
    return found[0]


def sim(verb, args):
    vessel = sim_vessel()
    command = vessel.astro.get('sim')
    if not command:
        raise config.Error(f'vessel profile {vessel.name} has no astro.sim (the sim backend command)')
    env = dict(os.environ, **vessel.env())
    words = shlex.split(expand(command, env)) + [verb] + list(args)
    return subprocess.call(words, env=env)
