"""hw.sh's device contract and profile layering, with throwaway profiles and drivers (no devices)."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

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
        (self.profile / 'profile.env').write_text('ASTRO_NETWORK_ENV=%s\n' % (self.tmp / 'net.env'))
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
        for yaml in sorted((ROOT / 'config/profiles').glob('*/*.yaml')):
            with self.subTest(profile=yaml.relative_to(ROOT)):
                run = subprocess.run(['bash', str(ROOT / 'hw.sh'), yaml.stem, '--help'], capture_output=True,
                                     text=True, env={**os.environ, 'ASTRO_PROFILE': yaml.parent.name,
                                                     'ASTRO_ENV_FILE': '/dev/null'})
                self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == '__main__':
    unittest.main()
