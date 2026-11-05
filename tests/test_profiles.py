"""Profile roots: every member loads, this machine resolves its host and vessel, repos per role, secrets.

Runs the real hw.sh on the fixture fleet tests/data/profiles (vessels a and b, station s) with a fake
hostname, a fake /sys/class/net and a fake driver; as a configured source, the fixture fleet is a git
repository in a temp dir. Nothing is contacted.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/data/profiles'
DRIVER = ''.join('driver_%s() { echo "%s $(cfg address) $*"; }\n' % (v, v)
                 for v in ('status', 'plan', 'apply', 'verify', 'rollback'))


class Profiles(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / 'profiles'
        shutil.copytree(FIXTURE, self.root)
        (self.tmp / 'drivers/fake').mkdir(parents=True)
        (self.tmp / 'drivers/fake/thing.sh').write_text(DRIVER)
        (self.tmp / 'drivers/hosts').mkdir()
        (self.tmp / 'drivers/hosts/fleet.sh').symlink_to(ROOT / 'devices/hosts/fleet.sh')
        self.sys = self.tmp / 'sys'
        self.nic('eth0', '02:00:00:00:00:99')               # matches nothing

    def nic(self, name, mac, assign='0'):
        (self.sys / name).mkdir(parents=True, exist_ok=True)
        (self.sys / name / 'address').write_text(mac + '\n')
        (self.sys / name / 'addr_assign_type').write_text(assign + '\n')

    def hw(self, *args, hostname='innovation', hw_root=ROOT, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(('ASTRO_', 'HW_'))}
        env.update(ASTRO_PROFILES=str(self.root), ASTRO_SECRETS=str(self.tmp / 'no-secrets'),
                   HW_HOSTNAME=hostname, HW_SYSFS_NET=str(self.sys),
                   HW_ADDRS='', HW_DT_MODEL=str(self.tmp / 'no-model'),
                   HW_DRIVERS=str(self.tmp / 'drivers'), XDG_STATE_HOME=str(self.tmp / 'state'),
                   XDG_CONFIG_HOME=str(self.tmp / 'config'))
        env.update(extra)
        env = {k: v for k, v in env.items() if v is not None}
        return subprocess.run(['bash', str(hw_root / 'hw.sh'), *args], capture_output=True, text=True,
                              env=env, timeout=60)

    def whoami(self, *args, **kw):
        run = self.hw(*args, 'whoami', **kw)
        self.assertEqual(run.returncode, 0, run.stderr)
        return dict(line.split(None, 1) for line in run.stdout.splitlines())

    # Resolution
    def test_an_operator_host_is_the_station_by_its_role(self):
        me = self.whoami(hostname='op')
        self.assertEqual(me['host'], 'op (hostname op)')
        self.assertEqual(me['station'], 's')
        self.assertEqual(me['vessel'], 'a (registry of a lists op)')
        self.assertEqual(me['network'], str(self.root / 'a/network.env'))
        self.assertNotIn('station', self.whoami(hostname='a-pc'))           # no operator role
        self.assertNotIn('station', self.whoami(hostname='op', HW_DT_MODEL='/proc/self/status'))  # a board

    def test_a_named_unlisted_laptop_is_a_generic_station_without_a_vessel(self):
        me = self.whoami('--host', 'somelaptop')
        self.assertEqual(me['host'], 'somelaptop (--host/ASTRO_HOST; no registry lists it: a generic control station)')
        self.assertEqual(me['station'], 's')
        self.assertTrue(me['vessel'].startswith('none'))
        self.assertEqual(self.repos('--host', 'somelaptop', 'hosts', 'repos'), ['alpha', 'gamma'])
        self.assertTrue(self.whoami('--host', 'somelaptop', '--vessel', 'b')['vessel'].startswith('b (--vessel'))
        me = self.whoami('--host', 'somelaptop', HW_ADDRS='10.0.1.57/24')       # on a's LAN: a
        self.assertEqual(me['vessel'], 'a (boat LAN 10.0.1.57 in 10.0.1.0/24)')
        run = self.hw('--host', 'somelaptop', 'whoami', HW_DT_MODEL='/proc/self/status')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('is a boat computer (device-tree model', run.stderr)

    def test_an_unlisted_laptop_on_one_boat_lan_is_its_generic_station(self):
        me = self.whoami(HW_ADDRS='172.17.0.1/16 10.0.1.57/24')
        self.assertEqual(me['host'], 'innovation (boat LAN 10.0.1.57 in 10.0.1.0/24: a generic control station)')
        self.assertEqual(me['station'], 's')
        self.assertEqual(me['vessel'], 'a (boat LAN 10.0.1.57 in 10.0.1.0/24)')
        self.assertEqual(self.repos('hosts', 'repos', HW_ADDRS='10.0.1.57/24'), ['alpha', 'gamma'])
        me = self.whoami(HW_ADDRS='10.0.9.57/24')                               # on no boat LAN
        self.assertTrue(me['host'].startswith('none (no match'))
        self.assertTrue(me['vessel'].startswith('none'))

    def test_an_unlisted_laptop_gets_the_vessel_domain_by_lan_subnet(self):
        me = self.whoami(HW_ADDRS='10.0.1.57/24')
        self.assertEqual((me['station'], me['vessel']), ('s', 'a (boat LAN 10.0.1.57 in 10.0.1.0/24)'))
        run = self.hw('env', 'ROS_DOMAIN_ID', HW_ADDRS='10.0.1.57/24')
        self.assertEqual((run.returncode, run.stdout.strip()), (0, '7'), run.stderr)

    def test_a_boat_computer_is_never_a_station(self):
        for extra, why in (({'HW_ADDRS': '10.0.1.10/24'}, 'address 10.0.1.10 belongs to a-pc'),     # a reservation
                           ({'HW_ADDRS': '10.0.1.1/24'}, 'address 10.0.1.1 belongs to a-router'),   # a registry address
                           ({'HW_ADDRS': '10.0.1.57/24', 'HW_DT_MODEL': '/proc/self/status'}, 'device-tree model')):
            with self.subTest(why=why):
                me = self.whoami(**extra)
                self.assertNotIn('station', me)
                self.assertIn('no match: hostname innovation', me['host'])
                self.assertIn(why, me['host'])
                self.assertTrue(me['vessel'].startswith('none'))
                run = self.hw('env', **extra)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn('pass --vessel', run.stderr)
        self.nic('enp1s0', '02:aa:00:00:00:02')                                 # a-cam's MAC: the host, no station
        me = self.whoami(hostname='devcontainer', HW_ADDRS='10.0.1.57/24')
        self.assertTrue(me['host'].startswith('a-cam (MAC'))
        self.assertNotIn('station', me)
        me = self.whoami('--host', 'a-pc', HW_ADDRS='10.0.1.57/24')             # a named boat host stays one
        self.assertEqual(me['host'], 'a-pc (--host/ASTRO_HOST)')
        self.assertNotIn('station', me)

    def test_two_vessels_on_one_subnet_need_vessel(self):
        env = self.root / 'b/network.env'
        env.write_text(env.read_text().replace('10.0.2.0/24', '10.0.1.0/24'))
        me = self.whoami(HW_ADDRS='10.0.1.57/24')
        self.assertIn('on the boat LAN of several vessels (a b): pass --vessel', me['host'])
        self.assertNotIn('station', me)
        self.assertTrue(me['vessel'].startswith('none'))
        me = self.whoami('--vessel', 'b', HW_ADDRS='10.0.1.57/24')
        self.assertEqual(me['station'], 's')
        self.assertTrue(me['vessel'].startswith('b (--vessel'))

    def test_several_stations_need_a_link_to_pick_one(self):
        shutil.copytree(self.root / 's', self.root / 't')
        (self.root / 'fleet/topology.yaml').write_text(
            'nodes: {a: vessel, b: vessel, s: station, t: station}\nlinks:\n  - {between: [s, a]}\n')
        self.assertEqual(self.whoami(hostname='op')['station'], 's')            # s is linked to a
        run = self.hw('--host', 'somelaptop', 'whoami')                         # no vessel: ambiguous
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('several station members (s t)', run.stderr)
        self.assertTrue(self.whoami()['host'].startswith('none (no match'))       # not a station: fine

    def test_a_permanent_mac_finds_the_host_when_the_hostname_is_unregistered(self):
        self.nic('enp1s0', '02:AA:00:00:00:02')
        self.nic('docker0', '02:aa:00:00:00:01')            # virtual: skipped
        self.nic('wlan0', '02:bb:00:00:00:01', assign='3')  # random MAC: skipped
        me = self.whoami(hostname='devcontainer')
        self.assertEqual(me['host'], 'a-cam (MAC 02:aa:00:00:00:02 (enp1s0))')
        self.assertTrue(me['vessel'].startswith('a '))

    def test_flags_and_environment_override(self):
        self.assertTrue(self.whoami('--host', 'b-pc', hostname='op')['vessel'].startswith('b '))
        self.assertTrue(self.whoami(ASTRO_HOST='b-pc')['vessel'].startswith('b '))
        self.assertTrue(self.whoami('--vessel', 'b', hostname='op')['vessel'].startswith('b (--vessel'))
        self.assertTrue(self.whoami(ASTRO_VESSEL='b')['vessel'].startswith('b '))
        for args, error in ((('--vessel', 's'), 'not a vessel'), (('--vessel', 'fleet'), 'not a vessel')):
            run = self.hw(*args, 'whoami')
            self.assertNotEqual(run.returncode, 0)
            self.assertIn(error, run.stderr)

    def test_no_match_leaves_scope_free_commands_working_and_device_verbs_ask_for_a_vessel(self):
        me = self.whoami()
        self.assertIn('no match: hostname innovation; MACs checked: 02:00:00:00:00:99', me['host'])
        self.assertTrue(me['vessel'].startswith('none'))
        self.assertEqual(self.hw('profile', 'show', 'a').returncode, 0)
        for args in (('env',), ('thing', 'status')):
            run = self.hw(*args)
            self.assertNotEqual(run.returncode, 0)
            for text in ('hostname innovation', '02:00:00:00:00:99', 'a:vessel b:vessel s:station', 'pass --vessel'):
                self.assertIn(text, run.stderr)
        run = self.hw('--vessel', 'a', 'thing', 'status', 'x')
        self.assertEqual(run.stdout, 'status 10.0.1.1 x\n', run.stderr)

    def test_two_matching_hosts_fail(self):
        self.nic('enp1s0', '02:aa:00:00:00:02')
        run = self.hw('whoami', hostname='a-pc')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('more than one host: a-cam by MAC 02:aa:00:00:00:02 (enp1s0), a-pc by hostname a-pc', run.stderr)

    def test_a_station_on_two_vessels_needs_vessel_or_one_boat_lan(self):
        with open(self.root / 'b/devices.yaml', 'a') as f:
            f.write('  op: {roles: [operator]}\n')
        run = self.hw('whoami', hostname='op')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('op is on vessels a b; pass --vessel', run.stderr)
        me = self.whoami('--vessel', 'b', hostname='op')
        self.assertTrue(me['vessel'].startswith('b '))
        self.assertEqual(me['station'], 's')
        me = self.whoami(hostname='op', HW_ADDRS='10.0.2.2/24')
        self.assertEqual(me['vessel'], 'b (registry of b lists op; on its boat LAN)')

    # Every member loads on every run
    def test_a_broken_member_fails_every_command_with_file_and_line(self):
        (self.root / 'b/profile.yaml').write_text('extends: fleet\nkind: vessel\nnetwork_env: network.env\ncolour: x\n')
        for args in (('whoami',), ('--vessel', 'a', 'env'), ('--vessel', 'a', 'thing', 'status')):
            run = self.hw(*args)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn('%s:4: unknown key' % (self.root / 'b/profile.yaml'), run.stderr)
        (self.root / 'b/profile.yaml').write_text('extends: fleet\nkind: vessel\n')
        (self.root / 'b/thing.yaml').write_text('address: x\nport: 1\n')
        run = self.hw('--vessel', 'a', 'env')
        self.assertIn('%s:2: unknown key' % (self.root / 'b/thing.yaml'), run.stderr)
        (self.root / 'b/thing.yaml').unlink()
        (self.root / 'b/profile.yaml').write_text('extends: fleet\n')
        self.assertIn('a member needs kind', self.hw('whoami').stderr)

    def test_a_vessel_file_holds_device_facts_and_roles_read_as_addresses(self):
        shown = self.hw('--vessel', 'a', 'profile', 'show')
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn('mac: 02:aa:00:00:02:01', shown.stdout)                # a/profile.yaml lidar: over fleet's
        self.assertIn('target: 10.0.1.10', shown.stdout)                     # radio.yaml $COMPUTER_IP: a-pc's lease
        for key, value in (('TIME_SERVER_IP', '10.0.1.10'), ('OPERATOR_IP', '10.0.1.2'), ('GATEWAY_IP', '10.0.1.2'),
                           ('ROUTER_IP', '10.0.1.1')):
            with self.subTest(key=key):
                run = self.hw('--vessel', 'a', 'env', key)
                self.assertEqual((run.returncode, run.stdout), (0, value + '\n'), run.stderr)
        run = self.hw('--vessel', 'a', 'env', 'CAMERA_HOST_IP')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('is not a key of', run.stderr)
        with open(self.root / 'a/profile.yaml', 'a') as stream:
            stream.write('jetson: {colour: x}\n')
        run = self.hw('--vessel', 'a', 'env')
        self.assertIn("%s:6: unknown key 'colour'" % (self.root / 'a/profile.yaml'), run.stderr)

    def test_topology_must_match_the_members(self):
        (self.root / 'fleet/topology.yaml').write_text('nodes: {a: vessel, s: station}\n')
        self.assertIn('nodes must list every member', self.hw('whoami').stderr)
        (self.root / 'fleet/topology.yaml').write_text(
            'nodes: {a: vessel, b: vessel, s: station}\nlinks:\n  - {between: [s, a], via: b.radio}\n')
        self.assertIn("via 'b.radio' must be MEMBER.KIND", self.hw('whoami').stderr)
        (self.root / 'fleet/topology.yaml').write_text(
            'nodes: {a: vessel, b: vessel, s: station}\nlinks:\n  - {between: [s, z]}\n')
        self.assertIn('between must name two members', self.hw('whoami').stderr)

    # Repositories per role
    def repos(self, *args, **kw):
        run = self.hw(*args, **kw)
        self.assertEqual(run.returncode, 0, run.stderr)
        return [line.split()[0] for line in run.stdout.splitlines()]

    def test_a_host_gets_all_and_its_roles_repos_and_a_vessel_replaces_one_role(self):
        self.assertEqual(self.repos('--host', 'a-cam', 'hosts', 'repos'), ['alpha', 'beta', 'delta'])
        self.assertEqual(self.repos('--host', 'a-pc', 'hosts', 'repos'), ['alpha'])
        self.assertEqual(self.repos('--host', 'op', 'hosts', 'repos'), ['alpha', 'gamma'])
        self.assertEqual(self.repos('--vessel', 'b', 'hosts', 'repos', '--role', 'camera'), ['alpha', 'beta'])
        self.assertEqual(self.repos('hosts', 'repos'), ['alpha'])               # no self, no role: all
        self.assertEqual(self.repos('hosts', 'repos', '--role', 'operator', '--role', 'camera'),
                         ['alpha', 'gamma', 'beta'])

    # Secrets
    def test_secrets_file_must_be_private_and_values_never_print(self):
        secrets = self.tmp / 'secrets.env'
        secrets.write_text('THING_PASSWORD=hunter22\n')
        secrets.chmod(0o644)
        run = self.hw('whoami', ASTRO_SECRETS=str(secrets))
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('is mode 644; secrets must be private: chmod 600', run.stderr)
        secrets.chmod(0o600)
        run = self.hw('secrets', 'check', ASTRO_SECRETS=str(secrets))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('mode 600', run.stdout)
        self.assertIn('missing   none', run.stdout)
        self.assertIn('optional  THING_JUMP (unset)', run.stdout)
        run = self.hw('secrets', 'check')
        self.assertEqual(run.returncode, 1)
        self.assertIn('missing   THING_PASSWORD', run.stdout)
        for args in (('secrets', 'check'), ('--vessel', 'a', 'profile', 'show'), ('--vessel', 'a', 'env'),
                     ('whoami',)):
            run = self.hw(*args, ASTRO_SECRETS=str(secrets))
            self.assertNotIn('hunter22', run.stdout + run.stderr)
        self.assertIn('password: THING_PASSWORD', self.hw('--vessel', 'a', 'profile', 'show',
                                                          ASTRO_SECRETS=str(secrets)).stdout)

    def test_secrets_inside_a_git_work_tree_are_refused_even_through_a_symlink(self):
        repo = self.tmp / 'repo'
        (repo / '.handoff').mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        inside = repo / '.handoff/secrets.env'
        inside.write_text('THING_PASSWORD=x\n')
        inside.chmod(0o600)
        link = self.tmp / 'config/secrets.env'
        link.parent.mkdir()
        link.symlink_to(inside)
        for path in (inside, link):
            run = self.hw('secrets', 'check', ASTRO_SECRETS=str(path))
            self.assertNotEqual(run.returncode, 0)
            self.assertIn('inside a git work tree; move it to ~/.config/astro/secrets.env', run.stderr)

    # Generic mode: no profiles
    def checkout(self):
        """A workspace with astro_dock's tools and no profiles/, as a fresh clone has it."""
        ws = self.tmp / 'ws'
        ws.mkdir()
        for name in ('hw.sh', 'setup.linux.sh', '.gitmodules', '.gitignore'):
            shutil.copy2(ROOT / name, ws / name)
        shutil.copytree(ROOT / 'devices', ws / 'devices')
        return ws

    def test_a_generic_checkout_works_without_profiles(self):
        ws = self.checkout()
        run = self.hw('whoami', hw_root=ws, ASTRO_PROFILES=None)
        self.assertEqual((run.returncode, run.stderr), (0, ''))                 # quiet
        self.assertEqual(run.stdout, 'profiles  none: generic mode (hw.sh profile sync URL [REF], or ASTRO_PROFILES=DIR)\n')
        self.assertEqual(self.hw('whoami', '--root', hw_root=ws, ASTRO_PROFILES=None).stdout, '\n')
        run = self.hw(hw_root=ws, ASTRO_PROFILES=None)
        self.assertEqual((run.returncode, run.stderr), (0, ''))
        self.assertIn('Generic mode: no profiles (hw.sh profile sync URL [REF], or ASTRO_PROFILES=DIR)', run.stdout)
        for args in (('env',), ('radio', 'status'), ('hosts', 'repos'), ('profile', 'show')):
            run = self.hw(*args, hw_root=ws, ASTRO_PROFILES=None)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn('No profiles (generic mode): hw.sh profile sync URL [REF], or ASTRO_PROFILES=DIR', run.stderr)
        run = self.hw('profile', 'sync', hw_root=ws, ASTRO_PROFILES=None)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('No profile source: hw.sh profile sync URL [REF] records one in %s'
                      % (self.tmp / 'config/astro/profiles.env'), run.stderr)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('ASTRO_', 'HW_'))}
        run = subprocess.run(['bash', str(ws / 'setup.linux.sh'), '--components', 'repos,gateway,boat,jetson',
                              '--dry-run', '--non-interactive'], capture_output=True, text=True, timeout=60,
                             env={**env, 'ASTRO_SECRETS': str(self.tmp / 'none'),
                                  'XDG_CONFIG_HOME': str(self.tmp / 'config')})
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(run.stdout.count('Generic mode (no profiles: ./hw.sh profile sync URL [REF], or ASTRO_PROFILES=DIR)'), 1)
        self.assertIn('Skipped:   repos gateway boat jetson', run.stdout)
        self.assertNotIn('submodule', run.stdout + run.stderr)
        self.assertFalse((ws / 'profiles').exists())

    # A configured source: ASTRO_PROFILES_URL at ASTRO_PROFILES_REF, checked out at profiles/
    def git(self, repo, *args):
        env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True,
                              env=env).stdout.strip()

    def commit(self, repo, name, text='x\n'):
        (repo / name).write_text(text)
        self.git(repo, 'add', '-A')
        self.git(repo, 'commit', '-q', '-m', name)
        return self.git(repo, 'rev-parse', 'HEAD')

    def source(self):
        """The fixture fleet as a git repository on main: (its path, its commit)."""
        repo = self.tmp / 'fleet-repo'
        shutil.copytree(FIXTURE, repo)
        self.git(repo, 'init', '-q', '-b', 'main')
        return repo, self.commit(repo, 'README')

    def config(self, text=None):
        path = self.tmp / 'config/astro/profiles.env'
        if text is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return path

    def test_profile_sync_records_the_source_and_checks_out_its_ref(self):
        repo, first = self.source()
        second = self.commit(repo, 'NOTES')
        ws = self.checkout()
        sync = lambda *args, **kw: self.hw('profile', 'sync', *args, hw_root=ws, ASTRO_PROFILES=None, **kw)
        self.config('ASTRO_PROFILES_URL=https://example.invalid/old.git\n# mine\nASTRO_PROFILES_REF=old\n')
        run = sync(str(repo), first)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('profiles %s/profiles: %s at %s (%s)' % (ws, repo, first, first[:7]), run.stderr)
        self.assertEqual(self.config().read_text(),
                         '# mine\nASTRO_PROFILES_URL=%s\nASTRO_PROFILES_REF=%s\n' % (repo, first))
        self.assertEqual(self.git(ws / 'profiles', 'rev-parse', 'HEAD'), first)
        run = self.hw('whoami', hostname='op', hw_root=ws, ASTRO_PROFILES=None)
        self.assertEqual((run.returncode, run.stderr), (0, ''))
        me = dict(line.split(None, 1) for line in run.stdout.splitlines())
        self.assertEqual(me['profiles'], '%s/profiles (%s at %s, %s)' % (ws, repo, first, first[:7]))
        self.assertEqual(me['station'], 's')
        self.assertEqual(self.hw('whoami', '--root', hw_root=ws, ASTRO_PROFILES=None).stdout, '%s/profiles\n' % ws)
        # A ref the checkout is not at warns (here: the environment's, which wins over the file).
        run = self.hw('whoami', hw_root=ws, ASTRO_PROFILES=None, ASTRO_PROFILES_REF='main')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('profiles/ is not at ASTRO_PROFILES_REF=main (%s): run hw.sh profile sync' % self.config(),
                      run.stderr)
        run = sync(str(repo))                                                    # no REF: main
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.config().read_text(), '# mine\nASTRO_PROFILES_URL=%s\n' % repo)
        self.assertEqual(self.git(ws / 'profiles', 'rev-parse', 'HEAD'), second)
        # A branch moves only on sync: whoami does not fetch.
        third = self.commit(repo, 'MORE')
        run = self.hw('whoami', hw_root=ws, ASTRO_PROFILES=None)
        self.assertEqual((run.returncode, run.stderr), (0, ''))
        self.assertEqual(self.git(ws / 'profiles', 'rev-parse', 'HEAD'), second)
        self.assertEqual(sync().returncode, 0)
        self.assertEqual(self.git(ws / 'profiles', 'rev-parse', 'HEAD'), third)
        run = sync(str(repo), 'nosuch')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('%s has no nosuch' % repo, run.stderr)

    def test_a_configured_source_is_checked_out_on_first_use(self):
        repo, first = self.source()
        ws = self.checkout()
        (ws / 'profiles').mkdir()                                                # empty, as an old pull leaves it
        self.config('ASTRO_PROFILES_URL=%s\n' % repo)
        me = dict(line.split(None, 1) for line in
                  self.hw('whoami', hostname='a-pc', hw_root=ws, ASTRO_PROFILES=None).stdout.splitlines())
        self.assertEqual(me['profiles'], '%s/profiles (%s at main, %s)' % (ws, repo, first[:7]))
        self.assertTrue(me['vessel'].startswith('a '))
        self.assertEqual(self.git(ws / 'profiles', 'rev-parse', 'HEAD'), first)
        ws2 = self.tmp / 'ws2'
        shutil.copytree(ws, ws2, ignore=shutil.ignore_patterns('profiles'))
        run = self.hw('whoami', hw_root=ws2, ASTRO_PROFILES=None, ASTRO_PROFILES_URL=str(self.tmp / 'nosuch'))
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('Cannot clone %s' % (self.tmp / 'nosuch'), run.stderr)

    def test_profiles_env_loads_only_its_keys_and_messages_hide_credentials(self):
        repo, first = self.source()
        ws = self.checkout()
        # Not private like secrets.env: its other keys (a vessel, a secret) are ignored.
        self.config('ASTRO_PROFILES_URL=%s\nASTRO_VESSEL=b\nTHING_PASSWORD=x\n' % repo)
        run = self.hw('whoami', hostname='a-pc', hw_root=ws, ASTRO_PROFILES=None)
        self.assertEqual(run.returncode, 0, run.stderr)
        me = dict(line.split(None, 1) for line in run.stdout.splitlines())
        self.assertTrue(me['vessel'].startswith('a '), me['vessel'])
        run = self.hw('secrets', 'check', hw_root=ws, ASTRO_PROFILES=None)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('THING_PASSWORD', run.stdout + run.stderr)                 # still missing
        # A URL with user:token@ is printed without them.
        secret = 'https://user:tok3n@example.invalid/fleet.git'
        run = self.hw('whoami', hostname='a-pc', hw_root=ws, ASTRO_PROFILES=None, ASTRO_PROFILES_URL=secret,
                      ASTRO_PROFILES_REF=first)
        me = dict(line.split(None, 1) for line in run.stdout.splitlines())
        self.assertEqual(me['profiles'], '%s/profiles (https://example.invalid/fleet.git at %s, %s)'
                         % (ws, first, first[:7]))
        run = self.hw('profile', 'sync', secret, 'main', hw_root=ws, ASTRO_PROFILES=None, GIT_TERMINAL_PROMPT='0')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('Cannot fetch https://example.invalid/fleet.git into', run.stderr)
        self.assertNotIn('tok3n', run.stdout + run.stderr)

    def test_astro_profiles_wins_over_a_configured_source(self):
        repo, _ = self.source()
        ws = self.checkout()
        self.config('ASTRO_PROFILES_URL=%s\n' % repo)
        me = self.whoami(hw_root=ws)
        self.assertEqual(me['profiles'], str(self.root))
        self.assertFalse((ws / 'profiles').exists())
        run = self.hw('profile', 'sync', hw_root=ws)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('ASTRO_PROFILES=%s is your own directory: update it there' % self.root, run.stderr)

    def test_sync_refuses_local_changes_and_a_directory_that_is_not_a_checkout(self):
        repo, first = self.source()
        self.commit(repo, 'NOTES')
        ws = self.checkout()
        self.assertEqual(self.hw('profile', 'sync', str(repo), first, hw_root=ws, ASTRO_PROFILES=None).returncode, 0)
        with open(ws / 'profiles/a/profile.yaml', 'a') as f:
            f.write('# local\n')
        run = self.hw('profile', 'sync', str(repo), 'main', hw_root=ws, ASTRO_PROFILES=None)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('%s/profiles has local changes' % ws, run.stderr)
        self.assertEqual(self.git(ws / 'profiles', 'rev-parse', 'HEAD'), first)
        shutil.rmtree(ws / 'profiles')
        shutil.copytree(FIXTURE, ws / 'profiles')                               # a copy, not a checkout
        run = self.hw('profile', 'sync', hw_root=ws, ASTRO_PROFILES=None)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('%s/profiles is not a git checkout; move it away to sync' % ws, run.stderr)

if __name__ == '__main__':
    unittest.main()
