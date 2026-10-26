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
        run = subprocess.run(['bash', str(ROOT / 'hw.sh'), 'env'], capture_output=True, text=True,
                             env={**os.environ, 'ASTRO_PROFILE': 'nonexistent', 'ASTRO_ENV_FILE': '/dev/null'})
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('No profile', run.stderr)
        run = subprocess.run(['bash', str(ROOT / 'hw.sh'), 'env', 'ROUTER_IP'], capture_output=True, text=True,
                             env={**os.environ, 'ASTRO_ENV_FILE': '/dev/null'})
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
        for profile in sorted(p for p in (ROOT / 'config/profiles').iterdir() if p.is_dir()):
            env = {**os.environ, 'ASTRO_ENV_FILE': '/dev/null'}
            show = subprocess.run(['bash', str(ROOT / 'hw.sh'), 'profile', 'show', profile.name],
                                  capture_output=True, text=True, env=env)
            with self.subTest(profile=profile.name):
                self.assertEqual(show.returncode, 0, show.stderr)
            shown = yaml.safe_load(show.stdout)
            if not shown['network_env']:
                continue                                # a parent or a stub: its kinds need a boat's addresses
            for kind in set(shown) - {'profile', 'network_env', 'registry'}:
                with self.subTest(profile=profile.name, kind=kind):
                    run = subprocess.run(['bash', str(ROOT / 'hw.sh'), '--profile', profile.name, kind, '--help'],
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
        self.assertIn('ASTRO_NETWORK_ENV=net.env', meta.stdout)        # inherited from mid
        self.assertIn('ASTRO_REGISTRY=reg.yaml', meta.stdout)

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
    def show(self, *args, dotenv='', **env):
        with tempfile.NamedTemporaryFile('w', suffix='.env') as file:
            file.write(dotenv)
            file.flush()
            environ = {k: v for k, v in os.environ.items() if not k.startswith(('ASTRO_', 'HW_'))}
            run = subprocess.run(['bash', str(ROOT / 'hw.sh'), *args, 'profile', 'show'], capture_output=True,
                                 text=True, env={**environ, 'ASTRO_ENV_FILE': file.name, **env})
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout.splitlines()[0]

    def test_selection_precedence_flag_then_environment_then_dotenv_then_the_one_with_a_network_env(self):
        self.assertEqual(self.show(), 'profile: roboboat -> blastoise')
        self.assertEqual(self.show(dotenv='ASTRO_PROFILE=squirtle\n'), 'profile: roboboat -> squirtle')
        self.assertEqual(self.show(dotenv='ASTRO_PROFILE=squirtle\n', ASTRO_PROFILE='roboboat'), 'profile: roboboat')
        self.assertEqual(self.show('--profile', 'blastoise', dotenv='ASTRO_PROFILE=squirtle\n',
                                   ASTRO_PROFILE='roboboat'), 'profile: roboboat -> blastoise')

    def test_blastoise_resolves_to_the_values_before_layering(self):
        baseline = json.loads((ROOT / 'tests/data/blastoise-baseline.json').read_text())
        meta = profile_py('meta', str(ROOT / 'config/profiles/blastoise')).stdout
        self.assertIn('ASTRO_NETWORK_ENV=config/boat-network.env', meta)
        self.assertIn('ASTRO_REGISTRY=config/devices.yaml', meta)
        for kind, expected in baseline.items():
            with self.subTest(kind=kind):
                run = profile_py('kind', str(ROOT / 'config/profiles/blastoise'), kind,
                                 str(ROOT / 'config/boat-network.env'))
                self.assertEqual(run.returncode, 0, run.stderr)
                values = assignments(run.stdout)
                if kind == 'hosts':                 # hosts.yaml gained sources: (the clone URLs) only
                    added = {k for k in set(values) - set(expected)}
                    self.assertTrue(all(k.startswith('HW_SOURCES') for k in added), added)
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
