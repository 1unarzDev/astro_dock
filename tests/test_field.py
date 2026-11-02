"""hw.sh hot and station take (devices/field.sh, field.py) against the fixture fleet: pure logic,
a dummy station in user network and UTS namespaces (random hostname and interface names; this machine's
network is never touched), and hot end to end with ssh standing in for scratch "hosts" on this disk."""
import os
from pathlib import Path
import random
import string
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / 'tests/data/profiles'                 # vessel a: LAN 10.0.1.0/24, boat hosts a-pc a-cam
sys.path.insert(0, str(ROOT / 'devices'))
import field  # noqa: E402

GIT_ENV = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 't',
           'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}
ADDR = ('2: wlx0    inet 172.20.0.5/16 brd 172.20.255.255 scope global wlx0\\ valid_lft forever\n'
        '3: enx9    inet 10.0.1.104/24 brd 10.0.1.255 scope global enx9\\ valid_lft forever\n'
        '3: enx9    inet 10.0.1.2/24 scope global secondary enx9\\ valid_lft forever')
ROUTE = 'default via 172.20.0.1 dev wlx0 proto dhcp metric 600'

# Runs each ssh call on a scratch "host": HOME is FAKE_ROOT/<target>; docker exec is only logged.
EXEC_SSH = r'''#!/usr/bin/env bash
while [[ $1 == -* ]]; do shift 2; done
target=$1; shift
echo "== $target $*" >> "$FAKE_SSH_LOG"
export HOME=$FAKE_ROOT/$target; mkdir -p "$HOME"; cd "$HOME" || exit 1
unset XDG_STATE_HOME XDG_CONFIG_HOME
if [[ $1 == docker ]]; then { echo "-- $*"; cat; } >> "$FAKE_SSH_LOG"; exit 0; fi
exec bash -c "$*"
'''
FAKE_DOCKER = '''#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_SSH_LOG"
case $1 in ps) echo c0ffee ;; exec) cat >/dev/null ;; esac
'''
FAKE_SETUP = '''#!/usr/bin/env bash
echo "setup $* ASTRO_NTP_ALLOW=$ASTRO_NTP_ALLOW" >> "$FAKE_SSH_LOG"
'''


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


def executable(path, text):
    path.write_text(text)
    path.chmod(0o755)
    return str(path)


def namespaces_work():
    try:
        return subprocess.run(['unshare', '-rnu', 'ip', 'link', 'add', 'probe0', 'type', 'dummy'],
                              capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class Logic(unittest.TestCase):
    def test_boat_link_is_the_interface_on_the_vessel_subnet(self):
        found = field.boat_link(ADDR.splitlines(), [ROUTE], '10.0.1.0/24')
        self.assertEqual(found, {'BOAT_IFACE': 'enx9', 'BOAT_ADDR': '10.0.1.104', 'UPLINK_IFACE': 'wlx0'})
        alone = field.boat_link(ADDR.splitlines(), ['default via 10.0.1.1 dev enx9'], '10.0.1.0/24')
        self.assertEqual(alone['UPLINK_IFACE'], '')

    def test_boat_link_refuses_none_or_several(self):
        with self.assertRaisesRegex(field.Error, 'no interface has an address in 10.9.0.0/16'):
            field.boat_link(ADDR.splitlines(), [ROUTE], '10.9.0.0/16')
        two = ADDR + '\n4: eth1    inet 10.0.1.77/24 scope global eth1'
        with self.assertRaisesRegex(field.Error, 'several interfaces are on 10.0.1.0/24 .enx9 eth1.'):
            field.boat_link(two.splitlines(), [ROUTE], '10.0.1.0/24')

    def test_boat_hosts_skip_operators_and_ssh_off(self):
        self.assertEqual(field.boat_hosts(PROFILES / 'a/devices.yaml'), ['a-pc', 'a-cam'])
        self.assertIn('operator', field.roles(PROFILES / 'a/devices.yaml'))

    def test_plan_builds_only_what_symlink_install_cannot_carry(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp, 'pkg_dir')
            (pkg / 'launch').mkdir(parents=True)
            (pkg / 'package.xml').write_text('<package><name>pkg_a</name></package>')
            live = ['M\tpkg_dir/pkg_a/node.py', 'M\tpkg_dir/config/x.yaml', 'M\tpkg_dir/launch/a.launch.xml',
                    'M\tREADME.md', 'A\tnotes.txt', 'A\tpkg_dir/README.md', 'M\tpkg_dir/doc/x.rst',
                    'A\tpkg_dir/test/test_new.py', 'D\tpkg_dir/tests/old.cpp']
            self.assertEqual(field.plan(live, tmp), ([], []))
            for change in ('A\tpkg_dir/pkg_a/new.py', 'D\tpkg_dir/config/x.yaml', 'M\tpkg_dir/setup.py',
                           'M\tpkg_dir/CMakeLists.txt', 'M\tpkg_dir/src/drive.cpp', 'M\tpkg_dir/package.xml'):
                packages, why = field.plan([change], tmp)
                self.assertEqual(packages, ['pkg_a'], change)
                self.assertIn(change.replace('\t', ' '), why[0])


class Station(unittest.TestCase):
    def take(self, *args, prefix=(), **env):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, 'calls.log')
            log.touch()
            Path(tmp, 'modules', 'k1').mkdir(parents=True)
            run = subprocess.run(
                [*prefix, str(ROOT / 'hw.sh'), 'station', 'take', *args], capture_output=True, text=True,
                env={**os.environ, 'XDG_STATE_HOME': tmp, 'XDG_CONFIG_HOME': tmp, 'ASTRO_PROFILES': str(PROFILES),
                     'ASTRO_SECRETS': '/nonexistent', 'ASTRO_ENV_FILE': '/dev/null', 'HW_KERNEL_RELEASE': 'k1',
                     'HW_MODULES_ROOT': str(Path(tmp, 'modules')), 'FAKE_SSH_LOG': str(log),
                     'HW_SSH': executable(Path(tmp, 'ssh'), '#!/usr/bin/env bash\n{ echo "== $*"; cat; } >> "$FAKE_SSH_LOG"\n'),
                     'HW_SSH_COPY_ID': 'true', 'HW_DOCKER': executable(Path(tmp, 'docker'), FAKE_DOCKER),
                     'HW_SETUP': executable(Path(tmp, 'setup'), FAKE_SETUP), **env})
            station = Path(tmp, 'fixture/station.yaml')
            return run, log.read_text(), station.read_text() if station.exists() else ''

    def test_reboot_needed_comes_first(self):
        run, calls, _ = self.take(HW_KERNEL_RELEASE='k2', ASTRO_PROFILES='/nonexistent')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('Reboot needed: the running kernel k2 has no modules', run.stderr)
        self.assertIn('installed: k1', run.stderr)
        self.assertEqual(calls, '')

    def test_take_applies_roles_keys_and_writes_the_station_file_everywhere(self):
        run, calls, station = self.take(ASTRO_VESSEL='a', HW_IP_ADDR=ADDR, HW_IP_ROUTE=ROUTE)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        me = os.uname().nodename.split('.')[0]
        self.assertIn('boat link enx9 (10.0.1.104 in 10.0.1.0/24), internet wlx0', run.stdout)
        self.assertIn('setup --components clock,gateway --yes ASTRO_NTP_ALLOW=operator', calls)
        self.assertIn(f'operator: {me}\n', station)
        self.assertRegex(station, r'since: \d{4}-\d\d-\d\dT')
        for target in ('crew@10.0.1.10', 'crew@10.0.1.11'):          # a-pc, a-cam by their leases
            self.assertRegex(calls, rf'== .*{target} .*station\.yaml')
        self.assertNotIn('10.0.1.2 ', calls)                          # the operator host op is not a boat host
        self.assertIn('docker exec -i c0ffee sh -c', calls)           # the key into the dev container

    def test_dry_run_changes_nothing(self):
        run, calls, station = self.take('--dry-run', ASTRO_VESSEL='a', HW_IP_ADDR=ADDR, HW_IP_ROUTE=ROUTE)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('dry run: nothing changed', run.stdout)
        self.assertEqual((calls, station), ('', ''))

    @unittest.skipUnless(namespaces_work(), 'no unprivileged user/network namespaces')
    def test_dummy_station_in_namespaces(self):
        """A random hostname and random interface names: one with internet, one on vessel a's LAN. The
        machine resolves as a generic control station of a by that address alone."""
        word = lambda n: ''.join(random.choices(string.ascii_lowercase, k=n))   # noqa: E731
        host, up, boat, octet = f'st-{word(6)}', f'u{word(5)}', f'b{word(5)}', random.randint(100, 149)
        script = (f'python3 -c "import socket; socket.sethostname(\'{host}\')" && '
                  f'ip link add {up} type dummy && ip link add {boat} type dummy && '
                  f'ip link set {up} up && ip link set {boat} up && ip addr add 198.51.100.7/24 dev {up} && '
                  f'ip addr add 10.0.1.{octet}/24 dev {boat} && ip route add default via 198.51.100.1 dev {up} && '
                  f'exec "$@"')
        run, calls, _ = self.take('--dry-run', prefix=('unshare', '-rnu', 'sh', '-c', script, 'sh'),
                                  HW_HOSTNAME=host)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn(f'station {host} for a: boat link {boat} (10.0.1.{octet} in 10.0.1.0/24), internet {up}',
                      run.stdout)
        self.assertIn(f'operator: {host}', run.stdout)
        self.assertIn('-> a-pc a-cam and the dev container', run.stdout)


class Hot(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ['rm', '-rf', str(self.tmp)])
        self.src = self.tmp / 'src'
        repo = self.repo = self.src / 'alpha'                          # repos.all of the fixture fleet
        (repo / 'pkg_dir/pkg_a').mkdir(parents=True)
        (repo / 'pkg_dir/package.xml').write_text('<package><name>pkg_a</name></package>\n')
        (repo / 'pkg_dir/pkg_a/node.py').write_text('SPEED = 1\n')
        git(repo, 'init', '-q', '-b', 'main')
        git(repo, 'add', '-A')
        git(repo, 'commit', '-q', '-m', 'one')
        self.hosts = self.tmp / 'hosts'
        self.pc = self.hosts / 'crew@10.0.1.10/astro_dock/src/alpha'      # a-pc has it; a-cam does not
        git(self.tmp, 'clone', '-q', str(repo), str(self.pc))

    def hot(self, *args):
        log = self.tmp / 'calls.log'
        log.write_text('')
        run = subprocess.run(
            [str(ROOT / 'hw.sh'), 'hot', 'alpha', *args], capture_output=True, text=True,
            env={**os.environ, **GIT_ENV, 'XDG_STATE_HOME': str(self.tmp), 'XDG_CONFIG_HOME': str(self.tmp),
                 'ASTRO_PROFILES': str(PROFILES), 'ASTRO_VESSEL': 'a', 'ASTRO_SECRETS': '/nonexistent',
                 'ASTRO_ENV_FILE': '/dev/null', 'HW_SRC': str(self.src), 'FAKE_SSH_LOG': str(log),
                 'FAKE_ROOT': str(self.hosts), 'HW_SSH': executable(self.tmp / 'ssh', EXEC_SSH)})
        return run, log.read_text()

    def test_working_tree_reaches_every_host_on_the_field_branch(self):
        (self.repo / 'pkg_dir/pkg_a/node.py').write_text('SPEED = 2\n')     # uncommitted
        head = git(self.repo, 'rev-parse', 'HEAD')
        run, calls = self.hot()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        branch = git(self.pc, 'rev-parse', '--abbrev-ref', 'HEAD')
        self.assertRegex(branch, r'^field/\d{8}$')
        self.assertEqual((self.pc / 'pkg_dir/pkg_a/node.py').read_text(), 'SPEED = 2\n')
        self.assertIn('a-pc: changed M\tpkg_dir/pkg_a/node.py', run.stdout)
        self.assertIn('OK a-pc: no build', run.stdout)
        cam = self.hosts / 'crew@10.0.1.11/astro_dock/src/alpha'          # not there before: created
        self.assertEqual((cam / 'pkg_dir/pkg_a/node.py').read_text(), 'SPEED = 2\n')
        self.assertIn('colcon build --symlink-install --packages-select pkg_a', calls)
        self.assertEqual(calls.count('-- docker exec -i dev bash -s'), 1)   # a-cam only
        # The station's checkout is as it was: same HEAD, the edit still uncommitted, no hot ref left.
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), head)
        self.assertEqual(git(self.repo, 'diff', '--name-only'), 'pkg_dir/pkg_a/node.py')
        self.assertEqual(git(self.repo, 'diff', '--cached', '--name-only'), '')
        self.assertEqual(git(self.repo, 'for-each-ref', 'refs/hot'), '')

    def test_new_file_builds_and_host_dirty_state_is_backed_up(self):
        (self.pc / 'pkg_dir/pkg_a/node.py').write_text('SPEED = 9\n')       # a field edit on the host
        (self.repo / 'pkg_dir/pkg_a/extra.py').write_text('X = 1\n')
        run, calls = self.hot('--host', 'a-pc')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('a-pc: BACKUP alpha', run.stdout)
        self.assertIn('pkg_a: A pkg_dir/pkg_a/extra.py', run.stdout)
        self.assertIn('--packages-select pkg_a', calls)
        self.assertNotIn('10.0.1.11', calls)                               # --host: a-pc only
        self.assertEqual((self.pc / 'pkg_dir/pkg_a/node.py').read_text(), 'SPEED = 1\n')
        backups = list(self.tmp.glob('hosts/crew@10.0.1.10/.local/state/astro_dock/hosts/hot-*/alpha.diff'))
        self.assertTrue(backups and 'SPEED = 9' in backups[0].read_text())

    def test_dry_run_prints_hosts_files_and_plan_and_sends_nothing(self):
        (self.repo / 'pkg_dir/pkg_a/extra.py').write_text('X = 1\n')
        (self.repo / 'pkg_dir/README.md').write_text('docs\n')
        before = git(self.pc, 'rev-parse', 'HEAD')
        run, calls = self.hot('--dry-run')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(calls, '')                                        # no ssh at all
        self.assertIn('dry run: a-pc (crew@10.0.1.10)', run.stdout)
        self.assertIn('dry run: changed A\tpkg_dir/pkg_a/extra.py', run.stdout)
        self.assertIn('dry run: builds for pkg_a: A pkg_dir/pkg_a/extra.py', run.stdout)
        self.assertNotIn('builds for pkg_a: A pkg_dir/README.md', run.stdout)
        self.assertEqual(git(self.pc, 'rev-parse', 'HEAD'), before)
        self.assertEqual(git(self.repo, 'for-each-ref', 'refs/hot'), '')

    def test_unknown_repo_or_host_is_refused(self):
        run, _ = self.hot('--host', 'op')                                  # the operator host: no boat host
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('No boat host runs alpha among op', run.stderr)


if __name__ == '__main__':
    unittest.main()
