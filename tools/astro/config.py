"""Where astro reads its facts: the workspace, the vessel profile's `astro:` section and the files it names.

The vessel comes from the profile set exactly as hw.sh resolves it (devices/profile.py): ASTRO_VESSEL wins,
else this machine's registry host. Its `astro:` section points at team files and holds no content:

    astro:
      tests: PKG/config/tests.yaml      # procedures (package-relative, or absolute / ~)
      bags:  PKG/config/bags.yaml       # rec presets: NAME: [topics] | all
      state: ~/.config/TEAM/state.yaml  # what `astro apply` writes
      check: ros2 run PKG config_check  # validates the state (the state path is appended); exit 0 = ok
      events: /events                   # every log line is published here
      home: ROLE|HOST                   # optional: where ROS-side verbs (test, rec, mark, ...) run; outside ROS
                                        # astro forwards them there (`astro on HOME astro ...`); --here stops it
      data: bags/sim                    # optional data root (relative: to the workspace); else $ASTRO_DATA
      state_env: VAR                    # optional: `on`/`sim` export the state path as VAR (and ASTRO_STATE)
      env: {VAR: value}                 # optional: more variables for `on`/`sim`
      sim: CMD                          # optional (sim profiles): the world backend, `CMD up|down|status`
      instance_env: VAR                 # optional (sim profiles): the backend's instance variable (see below)
      sim_time: false                   # optional: test runs follow /clock (use_sim_time)
      stacks: {NAME: {host, run, ready, needs, default, with}}   # optional: astro up/down/status (stacks.py)
      field: {repo: OWNER/NAME, url: GIT_URL, dir: PATH, auto: true}  # optional: check/issue/sync/show (field.py)
      help: [PATH]                      # optional: more help.yaml files (~, ${VAR:-default}), each its own group
      bundle_next: 'TEXT {bundle}'      # optional: printed after `astro bundle`, {bundle} = the file
      test_needs: [NAME]                # optional: stacks `astro test` starts first when down; a test's own
                                        # `needs` in the tests file replaces it for that test

Parallel sims: ASTRO_SIM_INSTANCE=N (default 0) on a sim profile (one with astro.sim) gives every verb its own
world. Instance N exports ASTRO_SIM_INSTANCE=N (and instance_env's VAR=N) to the backend and the containers,
uses ROS_DOMAIN_ID = the profile's domain + N, and for N >= 1 the registry containers NAME_N, the data dir
<data>/iN and the state file NAME.iN.EXT (copied from the base state the first time). Instance 0 is the
profile as written.
"""
import datetime
import importlib.util
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess

import yaml

WS = Path(__file__).resolve().parents[2]          # tools/astro/config.py -> the workspace root


class Error(Exception):
    pass


def _profile_module():
    spec = importlib.util.spec_from_file_location('astro_profile', WS / 'devices' / 'profile.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


profile = _profile_module()


def profiles_root():
    """ASTRO_PROFILES, else the one profiles.env names, else <ws>/profiles once it holds a profile."""
    root = os.environ.get('ASTRO_PROFILES')
    if not root:
        conf = Path(os.environ.get('ASTRO_PROFILES_CONFIG') or
                    Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'astro/profiles.env')
        try:
            for line in conf.read_text().splitlines():
                key, _, value = line.partition('=')
                if key.strip() == 'ASTRO_PROFILES' and value.strip():
                    root = value.strip().strip('"\'')
        except OSError:
            pass
    if root:
        return Path(root).expanduser()
    local = WS / 'profiles'
    return local if any(local.glob('*/profile.yaml')) else None


def sim_instance():
    """ASTRO_SIM_INSTANCE as an int (default 0)."""
    raw = os.environ.get('ASTRO_SIM_INSTANCE', '').strip() or '0'
    if not raw.isdigit():
        raise Error(f'ASTRO_SIM_INSTANCE={raw!r}: expected a whole number (0 = the default instance)')
    return int(raw)


def instance_path(path, n):
    """PATH with .iN before its suffix (N >= 1): state.yaml -> state.i3.yaml; PATH itself for 0."""
    if not n:
        return path
    text = str(path)
    stem, dot, ext = text.rpartition('.')
    if not dot or '/' in ext:
        return type(path)(f'{text}.i{n}')
    return type(path)(f'{stem}.i{n}.{ext}')


def _assignments(lines):
    out = {}
    for line in lines:
        match = re.search(r'([A-Z_][A-Z0-9_]*)=(.*)$', line)
        if match:
            words = shlex.split(match.group(2))
            out.setdefault(match.group(1), words[0] if words else '')
    return out


class Vessel:
    """One vessel profile: its name, directory chain, network env, registry and astro section."""

    def __init__(self, name=None):
        if os.environ.get('ASTRO_PROFILE_DIR') and not name:
            directory = Path(os.environ['ASTRO_PROFILE_DIR'])
            self.root = directory.parent
        else:
            self.root = profiles_root()
            if self.root is None:
                raise Error('no profile set (ASTRO_PROFILES, or hw.sh profile sync URL): astro needs a vessel profile')
            if name:
                directory = self.root / name
                if not (directory / 'profile.yaml').exists():
                    raise Error(f'no profile {name!r} in {self.root}')
            else:
                try:
                    found = _assignments(profile.resolve(self.root))
                except profile.Error as error:
                    raise Error(str(error)) from None
                if not found.get('ASTRO_VESSEL'):
                    raise Error(found.get('HW_SCOPE_ERROR') or 'no vessel for this machine; set ASTRO_VESSEL')
                directory = Path(found['HW_PROFILE'])
        try:
            self.dirs, meta = profile.chain(directory)
            kinds = profile.kinds(self.dirs)
            self.astro = dict(profile.resolve_kind(self.dirs, 'astro')) if 'astro' in kinds else {}
            self.hosts_kind = dict(profile.resolve_kind(self.dirs, 'hosts')) if 'hosts' in kinds else {}
        except profile.Error as error:
            raise Error(str(error)) from None
        self.name = self.dirs[-1].name
        self.network_env = meta['network_env'] or ''
        self.registry = meta['registry'] or ''
        self.network = profile.network(self.network_env) if self.network_env else {}
        self.is_sim = bool(self.astro.get('sim'))
        self.instance = sim_instance() if self.is_sim else 0

    def need(self, key):
        value = self.astro.get(key)
        if value in (None, ''):
            raise Error(f'vessel profile {self.name} has no astro.{key} (add it to the astro: section of '
                        f'{self.dirs[0].name}/profile.yaml; see `astro help astro`)')
        return value

    def hosts(self):
        if not self.registry:
            return {}
        hosts = profile.hosts(self.registry, self.network)
        if self.instance:                 # a parallel sim instance runs in its own containers: NAME_N
            for spec in hosts.values():
                if spec.get('container'):
                    spec['container'] = f'{spec["container"]}_{self.instance}'
        return hosts

    def path(self, key):
        return find(self.need(key))

    def state_raw(self):
        """The state path as written (~ kept), with this sim instance's .iN."""
        return instance_path(str(self.need('state')), self.instance)

    def state(self):
        """The state file; a sim instance's own copy starts from the base state the first time."""
        path = Path(os.path.expanduser(self.state_raw()))
        if self.instance and not path.exists():
            base = Path(os.path.expanduser(str(self.need('state'))))
            if base.is_file():
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(base, path)
                except OSError:
                    pass
        return path

    def data_root(self):
        data = self.astro.get('data')
        if data:
            data = Path(os.path.expanduser(str(data)))
            root = data if data.is_absolute() else WS / data
        elif self.instance and os.environ.get('ASTRO_DATA'):
            # an exported ASTRO_DATA may already be this instance's own dir
            root = Path(os.environ['ASTRO_DATA'])
            return root if root.name == f'i{self.instance}' else root / f'i{self.instance}'
        else:
            root = Path(os.environ.get('ASTRO_DATA') or WS / 'bags')
        return root / f'i{self.instance}' if self.instance else root

    def env(self):
        """The variables `astro on NAME` / `astro sim` export for this vessel."""
        out = {'ASTRO_VESSEL': self.name}
        if self.network.get('ROS_DOMAIN_ID'):
            out['ROS_DOMAIN_ID'] = str(int(self.network['ROS_DOMAIN_ID']) + self.instance)
        if self.is_sim:
            out['ASTRO_SIM_INSTANCE'] = str(self.instance)
            if self.astro.get('instance_env'):
                out[str(self.astro['instance_env'])] = str(self.instance)
        subnet = self.network.get('LAN_SUBNET', '')
        if subnet.startswith('127.'):
            out['ROS_AUTOMATIC_DISCOVERY_RANGE'] = 'LOCALHOST'
        out['ASTRO_DATA'] = str(self.data_root())
        if self.astro.get('state'):
            out['ASTRO_STATE'] = str(self.state())
            if self.astro.get('state_env'):
                out[str(self.astro['state_env'])] = str(self.state())
        if self.astro.get('sim_time'):
            out['ASTRO_SIM_TIME'] = '1'
        for key, value in (self.astro.get('env') or {}).items():
            out[str(key)] = os.path.expanduser(str(value))
        return out

    def sim_time(self):
        return bool(self.astro.get('sim_time')) or os.environ.get('ASTRO_SIM_TIME') == '1'


def find(path):
    """A package-relative file (PKG/rest): the workspace source first, then share/ of each ament prefix."""
    path = os.path.expanduser(str(path))
    if os.path.isabs(path):
        return Path(path)
    src = WS / 'src'
    for candidate in [src / path, *sorted(src.glob('*/' + path))]:
        if candidate.is_file():
            return candidate
    for prefix in filter(None, os.environ.get('AMENT_PREFIX_PATH', '').split(os.pathsep)):
        candidate = Path(prefix, 'share', path)
        if candidate.is_file():
            return candidate
    raise Error(f'{path}: not found under {src} or any share/ of AMENT_PREFIX_PATH')


def load_yaml(path):
    try:
        data = yaml.safe_load(Path(path).read_text())
    except OSError:
        return None
    except yaml.YAMLError as error:
        raise Error(f'{path}: {error}') from None
    return data


def today(root):
    day = Path(root) / datetime.date.today().isoformat()
    return day


def dev_container(local_root=WS, stopped=False):
    """This checkout's running dev container (devices/lib.sh hw_dev_container), or ''; with STOPPED, one that
    exists but is stopped counts too."""
    run = subprocess.run(['bash', '-c', 'source "$1/devices/lib.sh"; hw_dev_container "$1" "$2"', '_', str(local_root),
                          '--all' if stopped else ''], capture_output=True, text=True)
    return run.stdout.strip()


def compose_default_container(local_root=WS):
    run = subprocess.run(['bash', '-c', 'source "$1/devices/lib.sh"; hw_compose_default "$1" ASTRO_CONTAINER',
                          '_', str(local_root)], capture_output=True, text=True)
    return run.stdout.strip()


_HOSTS = {}     # profile root -> this machine's host: found once per process (it reads interfaces and leases)


def this_host():
    """This machine's registry host name as hw.sh whoami finds it (ASTRO_HOST wins), or ''."""
    if os.environ.get('ASTRO_HOST'):
        return os.environ['ASTRO_HOST']
    root = profiles_root()
    if root is None:
        return ''
    if str(root) not in _HOSTS:
        try:
            _HOSTS[str(root)] = _assignments(profile.resolve(root)).get('ASTRO_HOST', '')
        except profile.Error:
            _HOSTS[str(root)] = ''
    return _HOSTS[str(root)]


def have_ros():
    return importlib.util.find_spec('rclpy') is not None
