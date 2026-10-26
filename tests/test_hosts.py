"""hw.sh hosts with an ssh stand-in that records scripts; nothing is deployed."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / 'tests/data/profiles'                 # the fixture fleet, vessel a

# An ssh stand-in that records each call's host, command and script on stdin.
FAKE_SSH = '''#!/usr/bin/env bash
{ echo "== $*"; cat; } >> "$FAKE_SSH_LOG"
'''
REGISTRY = 'defaults: {ssh: true, container: box, workspace: ~/ws}\nhosts:\n  a: {}\n  r: {ssh: false}\n  b: {user: x}\n'


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
                     'XDG_STATE_HOME': tmp, 'ASTRO_PROFILES': str(PROFILES), 'ASTRO_VESSEL': 'a', 'ASTRO_SECRETS': '/nonexistent', 'ASTRO_ENV_FILE': '/dev/null', **env})
            return result, log.read_text()

    def test_deploy_fast_forwards_configured_repos_and_builds(self):
        result, calls = self.run_script('apply', 'host-a', HW_REPOS_ALL='repo_x')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('== host-a bash -s', calls)
        self.assertIn('for repo in astro_dock repo_x', calls)
        self.assertIn('git submodule update --init -q profiles', calls)    # the pinned profiles follow astro_dock
        self.assertIn('dir=$HOME/astro_dock;', calls)                       # the registry's workspace
        self.assertIn('git merge --ff-only -q origin/main', calls)
        self.assertIn('== host-a docker exec -i dev bash -s', calls)
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
            pinned = report % 'clean at-origin"\necho "profiles OK'
            good, _ = self.run_script('verify', 'a', ssh=pinned, ASTRO_REGISTRY=str(registry))
            self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
            for state in ('uninitialised', 'not at the pinned commit'):
                bad, _ = self.run_script('verify', 'a', ssh=report % ('clean at-origin"\necho "profiles FAIL %s' % state),
                                         ASTRO_REGISTRY=str(registry))
                self.assertNotEqual(bad.returncode, 0, state)

    def test_heads_report_the_profiles_submodule_state(self):
        # The status script itself, run against a scratch astro_dock with a declared submodule.
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp, 'astro_dock')
            sub = Path(tmp, 'sub')
            git = lambda *a, cwd=ws: subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t',
                                                     '-c', 'protocol.file.allow=always', *a],
                                                    cwd=cwd, check=True, capture_output=True)
            for repo in (sub, ws):
                repo.mkdir()
                git('init', '-q', '-b', 'main', cwd=repo)
                git('commit', '-q', '--allow-empty', '-m', 'x', cwd=repo)
            git('submodule', 'add', '-q', str(sub), 'profiles')
            git('commit', '-q', '-m', 'pin')
            script = lambda: subprocess.run(
                ['bash', '-c', '. "$1"; REPOS_ALL=; repos() { echo astro_dock; }; heads_script box "$2" | bash',
                 '_', str(ROOT / 'devices/hosts/fleet.sh'), str(ws)],
                capture_output=True, text=True).stdout
            self.assertIn('profiles OK', script())
            git('commit', '-q', '--allow-empty', '-m', 'y', cwd=ws / 'profiles')
            self.assertIn('profiles FAIL not at the pinned commit', script())
            git('submodule', 'deinit', '-q', '-f', 'profiles')
            self.assertIn('profiles FAIL uninitialised', script())

    def test_deploy_without_hosts_takes_the_ssh_hosts_of_the_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp, 'devices.yaml')
            registry.write_text(REGISTRY)
            result, calls = self.run_script('apply', ASTRO_REGISTRY=str(registry))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('== a bash -s', calls)
        self.assertIn('== b docker exec -i box bash -s', calls)
        self.assertNotIn('== r ', calls)

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
