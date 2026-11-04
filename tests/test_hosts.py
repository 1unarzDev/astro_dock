"""hw.sh hosts with an ssh stand-in that records scripts; nothing is deployed. The host-side profiles
scripts also run against scratch repositories (the fixture fleet as a git repository)."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / 'tests/data/profiles'                 # the fixture fleet, vessel a

# An ssh stand-in that records each call's host, command and script on stdin.
FAKE_SSH = '''#!/usr/bin/env bash
{ echo "== $*"; cat; } >> "$FAKE_SSH_LOG"
'''
GIT_ENV = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 't',
           'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}
REGISTRY = 'defaults: {ssh: true, container: box, workspace: ~/ws}\nhosts:\n  a: {}\n  r: {ssh: false}\n  b: {user: x}\n'



def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


def commit(repo, name):
    Path(repo, name).write_text(name + '\n')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', name)
    return git(repo, 'rev-parse', 'HEAD')


def fleet_repo(path, origin='https://example.invalid/fleet.git'):
    """The fixture fleet as a git checkout with an origin: its commit."""
    shutil.copytree(PROFILES, path)
    git(path, 'init', '-q', '-b', 'main')
    if origin:
        git(path, 'remote', 'add', 'origin', origin)
    return commit(path, 'README')


class Hosts(unittest.TestCase):
    def run_script(self, *args, script='hw.sh', ssh=FAKE_SSH, **env):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'ssh').write_text(ssh)
            Path(tmp, 'ssh').chmod(0o755)
            log = Path(tmp, 'ssh.log')
            log.touch()
            command = ['hosts', *args] if script == 'hw.sh' else list(args)
            result = subprocess.run(
                [str(ROOT / script), *command], capture_output=True, text=True,
                env={**os.environ, 'HW_SSH': str(Path(tmp, 'ssh')), 'FAKE_SSH_LOG': str(log),
                     'XDG_STATE_HOME': tmp, 'XDG_CONFIG_HOME': tmp, 'ASTRO_PROFILES': str(PROFILES), 'ASTRO_VESSEL': 'a', 'ASTRO_SECRETS': '/nonexistent', 'ASTRO_ENV_FILE': '/dev/null', **env})
            return result, log.read_text()

    def test_deploy_fast_forwards_configured_repos_and_builds(self):
        result, calls = self.run_script('apply', 'host-a', HW_REPOS_ALL='repo_x')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('== host-a bash -s', calls)
        self.assertIn('for repo in astro_dock repo_x', calls)
        self.assertIn('is not a git checkout: the hosts keep their profiles/', result.stderr)
        self.assertNotIn('profiles', calls)                                 # the fixture root: no profiles step
        self.assertIn('dir=$HOME/astro_dock;', calls)                       # the registry's workspace
        self.assertIn('git merge --ff-only -q origin/main', calls)
        self.assertIn("== host-a docker exec -i -e ASTRO_BUILD_IMAGE=$(docker inspect --format '{{.Image}}' dev) dev bash -s", calls)
        self.assertIn('find build install -xtype l -delete', calls)
        self.assertIn('colcon build --symlink-install', calls)
        self.assertNotIn('docker compose', calls)

    def test_deploy_image_commits_a_rollback_before_compose(self):
        result, calls = self.run_script('apply', '--image', 'repo/img@sha256:abc', 'host-a')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(calls.index('docker commit dev astro-rollback:'),
                        calls.index('ASTRO_IMAGE=repo/img@sha256:abc docker compose -p fixture_project'))
        self.assertIn('.devcontainer/prebuild.sh', calls)
        self.assertIn('hosts rollback astro-rollback:', calls)

    def test_rollback_recreates_and_builds_without_fast_forward(self):
        result, calls = self.run_script('rollback', 'astro-rollback:1', 'host-a')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ASTRO_IMAGE=astro-rollback:1 docker compose', calls)
        self.assertIn('colcon build', calls)
        self.assertNotIn('git merge', calls)

    def test_status_plan_and_verify_only_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp, 'devices.yaml')
            registry.write_text(REGISTRY)
            for verb in ('status', 'plan', 'verify'):
                with self.subTest(verb=verb):
                    result, calls = self.run_script(verb, ASTRO_REGISTRY=str(registry))
                    self.assertIn('git ls-remote origin main', calls)
                    for word in ('git fetch', 'git merge', 'checkout', 'docker commit', 'compose', 'colcon', 'tar '):
                        self.assertNotIn(word, calls)
                    self.assertIn('== a bash -s', calls)
                    self.assertNotIn('== r ', calls)

    def test_verify_fails_unless_every_repo_is_at_origin(self):
        report = ('#!/usr/bin/env bash\ncat >/dev/null\necho "astro_dock main abc clean at-origin"\n'
                  'echo "repo_x main def %s"\necho "container box img running=true"\n')
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp, 'devices.yaml')
            registry.write_text(REGISTRY)
            good, _ = self.run_script('verify', 'a', ssh=report % 'clean at-origin', ASTRO_REGISTRY=str(registry))
            self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
            bad, _ = self.run_script('verify', 'a', ssh=report % 'clean differs', ASTRO_REGISTRY=str(registry))
            self.assertNotEqual(bad.returncode, 0)
            pinned = report % 'clean at-origin"\necho "profiles OK abc1234'
            good, _ = self.run_script('verify', 'a', ssh=pinned, ASTRO_REGISTRY=str(registry))
            self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
            for state in ('at none, this computer\'s is abc1234', 'at def5678, this computer\'s is abc1234'):
                bad, _ = self.run_script('verify', 'a', ssh=report % ('clean at-origin"\necho "profiles FAIL %s' % state),
                                         ASTRO_REGISTRY=str(registry))
                self.assertNotEqual(bad.returncode, 0, state)

    def test_apply_and_plan_carry_this_computers_profile_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, 'fleet')
            pin = fleet_repo(root)
            result, calls = self.run_script('apply', 'host-a', ASTRO_PROFILES=str(root))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('WARN', result.stderr)
            self.assertIn("git clone -q --no-checkout 'https://example.invalid/fleet.git' profiles", calls)
            self.assertIn('checkout -q --detach %s' % pin, calls)
            result, calls = self.run_script('plan', 'host-a', ASTRO_PROFILES=str(root))
            self.assertIn('check out profiles %s' % pin[:7], result.stdout)
            self.assertIn('if [ "$p" = %s ]' % pin, calls)
            Path(root, 'a/profile.yaml').write_text(Path(root, 'a/profile.yaml').read_text() + '# local\n')
            result, calls = self.run_script('apply', 'host-a', ASTRO_PROFILES=str(root))
            self.assertIn('has local changes; the hosts get its commit %s without them' % pin[:7], result.stderr)
            self.assertIn('checkout -q --detach %s' % pin, calls)
            git(root, 'remote', 'remove', 'origin')
            result, calls = self.run_script('apply', 'host-a', ASTRO_PROFILES=str(root))
            self.assertIn('has no origin: the hosts keep their profiles/', result.stderr)
            self.assertNotIn('profiles', calls)

    def test_messages_never_show_the_origins_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, 'fleet')
            fleet_repo(root, origin='https://user:tok3n@example.invalid/fleet.git')
            result, calls = self.run_script('apply', 'host-a', ASTRO_PROFILES=str(root))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("git clone -q --no-checkout 'https://user:tok3n@example.invalid/fleet.git' profiles", calls)
            self.assertIn('echo "FAIL profiles: clone https://example.invalid/fleet.git"', calls)
            self.assertIn('(pushed to https://example.invalid/fleet.git? local changes?)', calls)
            self.assertEqual(calls.count('tok3n'), 2)                            # the clone and set-url only
            self.assertNotIn('tok3n', result.stdout + result.stderr)

    def test_the_host_scripts_check_out_and_verify_the_pin(self):
        # heads_script and fast_forward themselves, run against a scratch host workspace.
        with tempfile.TemporaryDirectory() as tmp:
            source, origin, ws = Path(tmp, 'fleet'), Path(tmp, 'astro-origin'), Path(tmp, 'astro_dock')
            first = fleet_repo(source, origin=None)
            origin.mkdir()
            git(origin, 'init', '-q', '-b', 'main')
            Path(origin, '.gitignore').write_text('/profiles/\n')
            commit(origin, 'hw.sh')
            git(tmp, 'clone', '-q', str(origin), str(ws))
            script = lambda fn, pin: subprocess.run(
                ['bash', '-c', '. "$1"; STAMP=1; REPOS_ALL=; repos() { echo astro_dock; }; PROFILES_URL=$3 '
                 'PROFILES_PIN=$4; %s "$2" | bash' % fn, '_', str(ROOT / 'devices/hosts/fleet.sh'), str(ws),
                 str(source), pin], capture_output=True, text=True, env={**os.environ, 'XDG_STATE_HOME': str(Path(tmp, 'state'))}).stdout
            heads = lambda pin: script('heads_script box', pin)          # container box, workspace ws
            apply = lambda pin: script('fast_forward', pin)
            self.assertNotIn('profiles', heads(''))                              # no pin: no profiles line
            self.assertIn('profiles FAIL at none, this computer\'s is %s' % first[:7], heads(first))
            out = apply(first)
            self.assertIn('OK profiles %s' % first[:7], out)
            self.assertNotIn('BACKUP', out)                                      # a fresh clone is not dirty
            self.assertEqual(git(ws / 'profiles', 'rev-parse', 'HEAD'), first)
            self.assertIn('profiles OK %s' % first[:7], heads(first))
            second = commit(source, 'NOTES')
            self.assertIn('profiles FAIL at %s' % first, heads(second))
            Path(ws, 'profiles/a/profile.yaml').write_text(Path(ws, 'profiles/a/profile.yaml').read_text() + '# local\n')
            out = apply(second)
            self.assertIn('BACKUP profiles', out)
            self.assertIn('OK profiles %s' % second[:7], out)
            self.assertEqual(git(ws / 'profiles', 'rev-parse', 'HEAD'), second)
            self.assertIn('# local', Path(tmp, 'state/astro_dock/hosts/backup-1/profiles.diff').read_text())
            self.assertIn('FAIL profiles: %s not checked out' % ('0' * 7), apply('0' * 40))

    def test_deploy_without_hosts_takes_the_ssh_hosts_of_the_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp, 'devices.yaml')
            registry.write_text(REGISTRY)
            result, calls = self.run_script('apply', ASTRO_REGISTRY=str(registry))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('== a bash -s', calls)
        self.assertIn("== b docker exec -i -e ASTRO_BUILD_IMAGE=$(docker inspect --format '{{.Image}}' box) box bash -s", calls)
        self.assertNotIn('== r ', calls)

    def test_a_registry_without_defaults_takes_this_checkouts_container_and_home_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp, 'devices.yaml')
            registry.write_text('hosts:\n  a: {user: x}\n')
            result, calls = self.run_script('apply', ASTRO_REGISTRY=str(registry), HOME=str(ROOT.parent))
        self.assertEqual(result.returncode, 0, result.stderr)
        container = re.search(r'(?m)^    container_name: (\S+)', (ROOT / '.devcontainer/docker-compose.yml').read_text())
        self.assertIn("== a docker exec -i -e ASTRO_BUILD_IMAGE=$(docker inspect --format '{{{{.Image}}}}' {0}) {0} bash -s".format(container.group(1)), calls)
        self.assertIn('dir=$HOME/%s;' % ROOT.name, calls)

    def test_the_vessel_registry_names_hosts_to_deploy(self):
        result, calls = self.run_script('plan')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('== op bash -s', calls)                       # a's operator registry host
        self.assertNotIn('== a-router ', calls)

    def test_keys_creates_the_key_once_and_installs_it_only_where_the_tui_is_refused(self):
        # This ssh refuses the TUI's strict check for host a (not installed yet) and accepts b.
        refusing = ('#!/usr/bin/env bash\necho "== $*" >> "$FAKE_SSH_LOG"\n'
                    'case "$*" in *StrictHostKeyChecking=yes*crew@10.0.1.10*) exit 255;; esac\n')
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in (('ssh', refusing), ('copy', FAKE_SSH)):
                Path(tmp, name).write_text(text)
                Path(tmp, name).chmod(0o755)
            Path(tmp, 'network.env').write_text(
                'GATEWAY_IP=10.0.1.2\nSTATIC_LEASES="aa=a-pc=10.0.1.10 bb=a-cam=10.0.1.11"\n')
            devices = Path(tmp, 'devices.yaml')
            devices.write_text('defaults: {user: crew, ssh: true}\nhosts:\n'
                               '  a-pc: {}\n  a-cam: {}\n'
                               '  s: {user: op, address: $GATEWAY_IP}\n'
                               '  a-router: {address: 10.0.1.1, ssh: false}\n')
            Path(tmp, 'xdg/fixture').mkdir(parents=True)            # the fleet's operator_registry
            Path(tmp, 'xdg/fixture/devices.yaml').write_text('self: s\n')
            log = Path(tmp, 'ssh.log')
            env = {**os.environ, 'HW_SSH': str(Path(tmp, 'ssh')), 'FAKE_SSH_LOG': str(log),
                   'HW_SSH_COPY_ID': str(Path(tmp, 'copy')), 'XDG_STATE_HOME': tmp, 'ASTRO_PROFILES': str(PROFILES), 'ASTRO_VESSEL': 'a', 'ASTRO_SECRETS': '/nonexistent', 'ASTRO_ENV_FILE': '/dev/null',
                   'XDG_CONFIG_HOME': str(Path(tmp, 'xdg')), 'ASTRO_REGISTRY': str(devices),
                   'ASTRO_NETWORK_ENV': str(Path(tmp, 'network.env'))}
            run = lambda: subprocess.run([str(ROOT / 'hw.sh'), 'hosts', 'keys'], capture_output=True,
                                         text=True, env=env)
            result = run()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            key = Path(tmp, 'xdg/fixture/ssh/id_ed25519')         # the fleet's operator_key
            first = key.read_text()
            calls = log.read_text()
            self.assertIn(f'== -i {key}.pub crew@10.0.1.10', calls)          # ssh-copy-id
            self.assertIn('StrictHostKeyChecking=accept-new crew@10.0.1.10 true', calls)
            self.assertNotIn(f'.pub crew@10.0.1.11', calls)                  # already reached
            self.assertNotIn('10.0.1.2', calls)                                      # self: s
            self.assertNotIn('10.0.1.1 ', calls)                                     # no ssh
            self.assertEqual(run().returncode, 0)
            self.assertEqual(key.read_text(), first)                                 # kept, not replaced

    def test_usage_without_a_verb(self):
        result, calls = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('plan|apply [--image REF] [HOST...]', result.stdout)
        self.assertEqual(calls, '')


if __name__ == '__main__':
    unittest.main()
