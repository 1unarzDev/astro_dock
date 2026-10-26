"""hw.sh's device contract and profile layering, with throwaway profiles and drivers (no devices)."""
import os
from pathlib import Path
import re
import subprocess
import json
import shlex
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
# The shipped profile root: the profiles/ submodule when initialised, else the legacy config/profiles.
SHIPPED = next((p for p in (ROOT / 'profiles', ROOT / 'config/profiles') if any(p.glob('*/profile.yaml'))), None)
REQUIRED = ('status', 'plan', 'apply', 'verify', 'rollback')
VERBS = ''.join('driver_%s() { echo "%s $*"; touch "$MARK"; }\n' % (v, v) for v in REQUIRED)


class Contract(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.profile = self.tmp / 'profile'
        self.profile.mkdir()
        (self.tmp / 'net.env').write_text('ADDR=10.0.0.7\nLIST="a b"\n')
        (self.profile / 'profile.yaml').write_text('network_env: %s\n' % (self.tmp / 'net.env'))
        self.mark = self.tmp / 'ran'

    def driver(self, name, body):
        path = self.tmp / 'drivers' / (name + '.sh')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def hw(self, *args, **extra):
        env = {**os.environ, 'ASTRO_PROFILE_DIR': str(self.profile), 'HW_DRIVERS': str(self.tmp / 'drivers'),
               'ASTRO_ENV_FILE': str(self.tmp / 'no.env'), 'MARK': str(self.mark), **extra}
        return subprocess.run(['bash', str(ROOT / 'hw.sh'), *args], capture_output=True, text=True, env=env)

    def test_a_driver_missing_required_functions_fails_before_any_verb(self):
        (self.profile / 'thing.yaml').write_text('driver: fake/broken\n')
        self.driver('fake/broken', VERBS.replace('driver_verify()', 'not_verify()').replace('driver_rollback()', 'x()'))
        for args in (('thing', 'status'), ('thing',)):
            run = self.hw(*args)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn('Driver fake/broken breaks the device contract: missing driver_verify driver_rollback',
                          run.stderr)
            self.assertFalse(self.mark.exists())

    def test_a_complete_driver_runs_its_verbs_and_extras_with_profile_values(self):
        (self.profile / 'thing.yaml').write_text(
            'driver: fake/ok\naddress: $ADDR\nitems: [$LIST, c]\nnested: {a: 1, b: {c: x}}\n')
        self.driver('fake/ok', VERBS + 'driver_extra() { echo "$(cfg address)|$(cfg items)|$(cfg_keys nested)|'
                                       '$(cfg nested.b.c)"; }\n')
        run = self.hw('thing', 'plan', 'one', 'two')
        self.assertEqual((run.returncode, run.stdout), (0, 'plan one two\n'), run.stderr)
        run = self.hw('thing', 'extra')
        self.assertEqual(run.stdout, '10.0.0.7|a b c|a b|x\n', run.stderr)
        run = self.hw('thing', 'extra', HW_ADDRESS='override', ADDR='from-env')
        self.assertEqual(run.stdout.split('|')[0], 'override')
        run = self.hw('thing', 'extra', ADDR='from-env')
        self.assertEqual(run.stdout.split('|')[0], 'from-env')
        run = self.hw('thing', 'nope')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("thing has no verb 'nope'", run.stderr)

    def test_an_unknown_key_in_a_profile_fails(self):
        (self.profile / 'thing.yaml').write_text('driver: fake/ok\naddress: $MISSING\n')
        self.driver('fake/ok', VERBS)
        run = self.hw('thing', 'status')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('$MISSING is not set', run.stderr)
        self.assertFalse(self.mark.exists())

    def test_secrets_come_from_the_env_file_the_profile_names_and_the_environment_wins(self):
        (self.profile / 'thing.yaml').write_text('driver: fake/ok\ncredentials: {password: THING_PASSWORD}\n')
        self.driver('fake/ok', VERBS + 'driver_extra() { echo "[$(secret credentials.password)]"; }\n')
        (self.tmp / 'no.env').write_text('THING_PASSWORD="from file"\n')
        self.assertEqual(self.hw('thing', 'extra').stdout, '[from file]\n')
        self.assertEqual(self.hw('thing', 'extra', THING_PASSWORD='').stdout, '[]\n')

    def test_env_prints_only_network_env_keys(self):
        (self.tmp / 'no.env').write_text('THING_PASSWORD=hunter22\n')
        run = self.hw('env', 'ADDR')
        self.assertEqual((run.returncode, run.stdout), (0, '10.0.0.7\n'), run.stderr)
        for key in ('THING_PASSWORD', 'HOME'):
            with self.subTest(key=key):
                run = self.hw('env', key)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn('is not a key of', run.stderr)
                self.assertNotIn('hunter22', run.stdout + run.stderr)

    def test_profile_selection(self):
        env = {**os.environ, 'ASTRO_ENV_FILE': '/dev/null', 'ASTRO_SECRETS': '/nonexistent',
               'HW_HOSTNAME': 'innovation', 'HW_SYSFS_NET': '/nonexistent'}
        run = subprocess.run(['bash', str(ROOT / 'hw.sh'), 'env'], capture_output=True, text=True,
                             env={**env, 'ASTRO_VESSEL': 'nonexistent'})
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('not a vessel', run.stderr)
        run = subprocess.run(['bash', str(ROOT / 'hw.sh'), 'env', 'ROUTER_IP'], capture_output=True, text=True, env=env)
        self.assertNotEqual(run.returncode, 0)                          # no match and no --vessel
        self.assertIn('pass --vessel', run.stderr)
        run = subprocess.run(['bash', str(ROOT / 'hw.sh'), '--vessel', 'blastoise', 'env', 'ROUTER_IP'],
                             capture_output=True, text=True, env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertRegex(run.stdout, r'^[0-9.]+\n$')

    def test_every_shipped_driver_and_profile_satisfies_the_contract(self):
        drivers = sorted((ROOT / 'devices').glob('*/*.sh'))
        for path in drivers:
            with self.subTest(driver=path.relative_to(ROOT)):
                check = ('source %s/devices/lib.sh; out=$(source %s); [[ -z $out ]] || exit 9; source %s; '
                         'for v in %s; do declare -F driver_$v >/dev/null || { echo missing $v; exit 1; }; done'
                         % (ROOT, path, path, ' '.join(REQUIRED)))
                run = subprocess.run(['bash', '-c', check], capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        for profile in sorted(p.parent for p in (SHIPPED.glob('*/profile.yaml') if SHIPPED else ())):
            env = {**os.environ, 'ASTRO_ENV_FILE': '/dev/null', 'ASTRO_SECRETS': '/nonexistent'}
            show = subprocess.run(['bash', str(ROOT / 'hw.sh'), 'profile', 'show', profile.name],
                                  capture_output=True, text=True, env=env)
            with self.subTest(profile=profile.name):
                self.assertEqual(show.returncode, 0, show.stderr)
            shown = yaml.safe_load(show.stdout)
            if not shown['network_env']:
                continue                                # a parent or a stub: its kinds need a boat's addresses
            for kind in set(shown) - {'profile', 'network_env', 'registry'}:
                with self.subTest(profile=profile.name, kind=kind):
                    run = subprocess.run(['bash', str(ROOT / 'hw.sh'), '--vessel', profile.name, kind, '--help'],
                                         capture_output=True, text=True, env=env)
                    self.assertEqual(run.returncode, 0, run.stderr)


def profile_py(*args, env=None):
    return subprocess.run(['python3', '-I', str(ROOT / 'devices/profile.py'), *args], capture_output=True, text=True,
                          env=env or {'PATH': os.environ['PATH']})


def assignments(text):
    """HW_* values from profile.py kind output ('[[ -v X ]] || X=value' lines)."""
    out = {}
    for line in text.splitlines():
        name, _, value = line.split(' || ', 1)[1].partition('=')
        out[name] = (shlex.split(value) or [''])[0]
    return out


class Layering(unittest.TestCase):
    """Profiles extend a parent: maps merge, lists and scalars replace, unknown keys and cycles fail."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.write('base/thing.yaml', 'driver: fake/ok\nlist: [a, b]\nnested: {x: 1, y: {z: 2}}\nopen: {}\n'
                                      'boat_sets:\nscalar: s\n')
        self.write('base/other.yaml', 'driver: fake/ok\nkeep: 1\n')
        self.write('mid/profile.yaml', 'extends: base\nnetwork_env: net.env\n')
        self.write('mid/thing.yaml', 'list: [c]\nnested: {y: {z: 3}}\nopen: {r1: {mac: m1}}\nboat_sets: {k: v}\n')
        self.write('leaf/profile.yaml', 'extends: mid\nregistry: reg.yaml\n')
        self.write('leaf/thing.yaml', 'scalar: t\nopen: {r1: {mac: m2}}\n')
        self.write('leaf/extra.yaml', 'driver: fake/new\n')

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_merge_maps_deep_lists_and_scalars_replace_children_fill_empty_and_null(self):
        run = profile_py('kind', str(self.root / 'leaf'), 'thing', '')
        self.assertEqual(run.returncode, 0, run.stderr)
        values = assignments(run.stdout)
        self.assertEqual(values['HW_LIST'], 'c')                       # list replaced, not appended
        self.assertEqual((values['HW_NESTED_X'], values['HW_NESTED_Y_Z']), ('1', '3'))
        self.assertEqual(values['HW_OPEN__KEYS'], 'r1')
        self.assertEqual(values['HW_OPEN_R1_MAC'], 'm2')               # the nearest layer wins
        self.assertEqual(values['HW_BOAT_SETS_K'], 'v')
        self.assertEqual(values['HW_SCALAR'], 't')
        self.assertEqual(values['HW__KEYS'], 'driver list nested open boat_sets scalar')
        meta = profile_py('meta', str(self.root / 'leaf'))
        self.assertIn("HW_PROFILE_CHAIN='base mid leaf'", meta.stdout)
        self.assertIn("HW_PROFILE_KINDS='extra other thing'", meta.stdout)
        self.assertIn('ASTRO_NETWORK_ENV=%s' % (self.root / 'mid/net.env'), meta.stdout)   # inherited, relative to mid
        self.assertIn('ASTRO_REGISTRY=%s' % (self.root / 'leaf/reg.yaml'), meta.stdout)

    def refused(self, *args):
        run = profile_py(*args)
        self.assertNotEqual(run.returncode, 0)
        return run.stderr.strip()

    def test_unknown_keys_and_type_changes_fail_with_file_and_line(self):
        path = self.write('leaf/thing.yaml', 'scalar: t\nnested: {y: {typo: 1}}\n')
        self.assertEqual(self.refused('kind', str(self.root / 'leaf'), 'thing', ''),
                         "%s:2: unknown key 'nested.y.typo' (not in a parent profile)" % path)
        path = self.write('leaf/thing.yaml', 'scalar: t\nnot_in_parent: 1\n')
        self.assertIn("%s:2: unknown key 'not_in_parent'" % path, self.refused('kind', str(self.root / 'leaf'), 'thing', ''))
        path = self.write('leaf/thing.yaml', '\nnested: 5\n')
        self.assertIn('%s:2: nested must be a mapping' % path, self.refused('kind', str(self.root / 'leaf'), 'thing', ''))
        path = self.write('leaf/profile.yaml', 'extends: mid\nextend: x\n')
        self.assertIn("%s:2: unknown key 'extend'" % path, self.refused('meta', str(self.root / 'leaf')))

    def test_strict_yaml_duplicate_keys_and_anchors_fail_with_line(self):
        path = self.write('leaf/thing.yaml', 'scalar: t\nscalar: u\n')
        self.assertIn("%s:2: duplicate key 'scalar'" % path, self.refused('kind', str(self.root / 'leaf'), 'thing', ''))
        path = self.write('leaf/thing.yaml', 'list: &l [x]\n')
        self.assertIn('%s:1: anchors and aliases are not allowed' % path,
                      self.refused('kind', str(self.root / 'leaf'), 'thing', ''))

    def test_cycles_and_missing_parents_fail(self):
        self.write('base/profile.yaml', 'extends: leaf\n')
        self.assertIn('profile cycle: leaf -> mid -> base -> leaf', self.refused('meta', str(self.root / 'leaf')))
        path = self.write('base/profile.yaml', 'extends: nowhere\n')
        self.assertIn("%s:1: no profile 'nowhere'" % path, self.refused('meta', str(self.root / 'leaf')))

    def test_show_never_prints_a_secret(self):
        env = self.root / 'net.env'
        env.write_text('ADDR=10.1.1.1\n')
        self.write('base/thing.yaml', 'driver: fake/ok\naddress: $ADDR\nleak: $SECRET_TOKEN\n'
                                      'credentials: {password: SECRET_TOKEN}\n')
        self.write('leaf/thing.yaml', '')
        self.write('mid/thing.yaml', '')
        run = profile_py('show', str(self.root / 'leaf'), str(env),
                         env={'PATH': os.environ['PATH'], 'SECRET_TOKEN': 'hunter22'})
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn('hunter22', run.stdout)
        self.assertIn('address: 10.1.1.1', run.stdout)
        self.assertIn('leak: $SECRET_TOKEN', run.stdout)               # the environment is never read
        self.assertIn('password: SECRET_TOKEN', run.stdout)            # the .env key name, not its value
        self.assertTrue(run.stdout.startswith('profile: base -> mid -> leaf\n'))


class ShippedProfiles(unittest.TestCase):
    def setUp(self):
        if SHIPPED is None:
            self.skipTest('no profile root (generic checkout)')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sys = Path(tmp.name)

    def nic(self, mac):
        (self.sys / 'eth0').mkdir(exist_ok=True)
        (self.sys / 'eth0/address').write_text(mac + '\n')
        (self.sys / 'eth0/addr_assign_type').write_text('0\n')

    def show(self, *args, hostname='innovation', **env):
        environ = {k: v for k, v in os.environ.items() if not k.startswith(('ASTRO_', 'HW_'))}
        run = subprocess.run(['bash', str(ROOT / 'hw.sh'), *args, 'profile', 'show'], capture_output=True,
                             text=True, env={**environ, 'ASTRO_ENV_FILE': '/dev/null', 'ASTRO_SECRETS': '/nonexistent',
                                             'HW_HOSTNAME': hostname, 'HW_SYSFS_NET': str(self.sys), **env})
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout.splitlines()[0]

    def test_the_boat_hosts_and_the_station_find_blastoise_and_flags_override(self):
        self.assertEqual(self.show(), 'profile: roboboat')                     # no match: the fleet
        self.assertEqual(self.show(hostname='tranquility'), 'profile: roboboat -> blastoise')
        self.assertEqual(self.show(hostname='blastoise-odroid'), 'profile: roboboat -> blastoise')
        self.nic('56:e2:03:5c:60:22')                                          # the ODROID's leased MAC
        self.assertEqual(self.show(hostname='roboboat'), 'profile: roboboat -> blastoise')
        self.assertEqual(self.show(ASTRO_VESSEL='squirtle'), 'profile: roboboat -> squirtle')
        self.assertEqual(self.show('--vessel', 'blastoise', ASTRO_VESSEL='squirtle'), 'profile: roboboat -> blastoise')
        self.assertEqual(self.show(ASTRO_PROFILE='squirtle', hostname='x'), 'profile: roboboat -> squirtle')

    def test_blastoise_resolves_to_the_values_before_layering(self):
        """No behaviour change: blastoise's kinds equal the pre-layering values, except the repos list,
        which became repos.all plus per-role lists (the owner's decision)."""
        baseline = json.loads((ROOT / 'tests/data/blastoise-baseline.json').read_text())
        meta = profile_py('meta', str(SHIPPED / 'blastoise')).stdout
        env = assignments('\n'.join(line for line in meta.splitlines() if ' || ' in line))
        self.assertEqual(Path(env['ASTRO_NETWORK_ENV']).read_text(), (ROOT / 'config/boat-network.env').read_text())
        self.assertEqual(Path(env['ASTRO_REGISTRY']).read_text(), (ROOT / 'config/devices.yaml').read_text())
        for kind, expected in baseline.items():
            with self.subTest(kind=kind):
                run = profile_py('kind', str(SHIPPED / 'blastoise'), kind, env['ASTRO_NETWORK_ENV'])
                self.assertEqual(run.returncode, 0, run.stderr)
                values = assignments(run.stdout)
                if kind == 'hosts':                 # hosts.yaml gained sources: and per-role repos
                    added = {k for k in set(values) - set(expected)}
                    self.assertTrue(all(k.startswith(('HW_SOURCES', 'HW_REPOS_')) for k in added), added)
                    self.assertEqual(values['HW_REPOS_ALL'], expected.pop('HW_REPOS'))
                    values = {k: v for k, v in values.items() if k not in added}
                    expected = {**expected, 'HW__KEYS': expected['HW__KEYS'].replace('repos', 'repos sources')}
                self.assertEqual(values, expected)


# Commands hw.sh and the setup repos stage replaced: nothing tracked here, nor a checked-out src/mhseals_* repo, may name them.
REMOVED = re.compile(r'\b(?:(?:router_setup|ubiquiti_setup|link_measure|lidar_setup|promote_jetson|boat_hosts'
                     r'|clone_optional)\.sh|optional_repos\.yaml)\b')


class RemovedCommands(unittest.TestCase):
    def test_no_file_names_a_removed_command(self):
        def tracked(repo):
            run = subprocess.run(['git', '-C', str(repo), 'ls-files', '-z'], capture_output=True, check=True)
            return [repo / name for name in run.stdout.decode().split('\0') if name]
        files = tracked(ROOT) + [f for repo in sorted(ROOT.glob('src/mhseals_*')) if (repo / '.git').exists()
                                 for f in tracked(repo)]
        hits = []
        for path in files:
            if path == Path(__file__) or not path.is_file():
                continue
            for number, line in enumerate(path.read_text(errors='ignore').splitlines(), 1):
                if REMOVED.search(line):
                    hits.append('%s:%d: %s' % (path.relative_to(ROOT), number, line.strip()))
        self.assertEqual(hits, [])


if __name__ == '__main__':
    unittest.main()
