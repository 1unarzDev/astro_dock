"""`astro on NAME [cmd...]` and `astro sim VERB`: where a command runs.

NAME is a registry host of the vessel (exact name, else a unique substring; any vessel's registry when the
current one has none) reached over ssh, then `docker exec` into its dev container; or a vessel profile
whose registry has no ssh host (the sim), run on this machine with that vessel's environment: inside the
registry host's `container:` when it names one, else this checkout's dev container, else (already in a
container with ROS) right here."""
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

from . import config


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


def ssh_command(vessel, host, spec, cmd, tty):
    if not spec['address']:
        raise config.Error(f'{host} has no address in {vessel.registry}')
    target = f'{spec["user"]}@{spec["address"]}' if spec['user'] else spec['address']
    words = ['ssh']
    key_dir = vessel.hosts_kind.get('operator_key')
    if key_dir:
        base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / str(key_dir)
        if (base / 'id_ed25519').exists():
            words += ['-i', str(base / 'id_ed25519'), '-o', f'UserKnownHostsFile={base / "known_hosts"}']
    if spec['via']:
        via = vessel.hosts().get(spec['via'])
        if via and via['address']:
            words += ['-J', f'{via["user"]}@{via["address"]}' if via['user'] else via['address']]
    name = spec['container'] or (vessel.hosts_kind.get('container') or {}).get('name') \
        or config.compose_default_container()
    project = vessel.hosts_kind.get('compose_project')
    find = ''
    if project and not spec['container']:   # the dev service of the host's compose project, else the name
        find = (f'c=$(docker ps -q --filter label=com.docker.compose.project={shlex.quote(project)} '
                f'--filter label=com.docker.compose.service=dev | head -n1); ')
    container = f'"${{c:-{name}}}"' if find else shlex.quote(name)
    words += ['-t'] if tty else []
    remote = f'{find}docker exec {"-it" if tty else "-i"} {container} bash'
    if cmd:
        remote += ' -c ' + shlex.quote(' '.join(cmd))
    return words + [target, remote]


def local_env(vessel, inside):
    """The vessel's variables; paths stay as written (~, workspace-relative) when they cross into a container."""
    env = vessel.env()
    if not inside:
        env.pop('ASTRO_DATA', None)               # the container resolves its own data root from the profile
        raw = vessel.astro.get('state')
        for key in ('ASTRO_STATE', str(vessel.astro.get('state_env') or '')):
            if key in env and raw:
                env[key] = str(raw)
    return env


def in_container():
    return Path('/.dockerenv').exists() or bool(os.environ.get('ROS_DISTRO'))


def on(name, cmd):
    tty = _tty()
    root = config.profiles_root()
    if name in _members(root) and name in local_vessels():
        vessel = config.Vessel(name)
        hosts = vessel.hosts()
        container = next((s['container'] for s in hosts.values() if s['container']), '')
        if not container and in_container():
            env = dict(os.environ, **local_env(vessel, True))
            args = ['bash', '-c', ' '.join(cmd)] if cmd else ['bash', '-i']
            os.execvpe('bash', args, env)
        if not container:
            container = config.dev_container()
            if not container:
                raise config.Error('no running dev container for this checkout (start it, or name one with '
                                   f'container: on the {name} registry host)')
        words = ['docker', 'exec', '-it' if tty else '-i']
        for key, value in local_env(vessel, False).items():
            words += ['-e', f'{key}={value}']
        words += [container, 'bash'] + (['-c', ' '.join(cmd)] if cmd else [])
        return subprocess.call(words)
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
