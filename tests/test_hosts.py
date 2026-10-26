"""hw.sh hosts (and the boat_hosts.sh alias) with an ssh stand-in that records scripts; nothing is deployed."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

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
                     'XDG_STATE_HOME': tmp, 'ASTRO_ENV_FILE': '/dev/null', **env})
            return result, log.read_text()

    def test_deploy_fast_forwards_configured_repos_and_builds(self):
        result, calls = self.run_script('apply', 'host-a', HW_REPOS='repo_x')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('== host-a bash -s', calls)
        self.assertIn('for repo in astro_dock repo_x', calls)
        self.assertIn('dir=$HOME/astro_dock;', calls)                       # the registry's workspace
        self.assertIn('git merge --ff-only -q origin/main', calls)
        self.assertIn('== host-a docker exec -i roboboat_dev bash -s', calls)
        self.assertIn('find build install -xtype l -delete', calls)
        self.assertIn('colcon build --symlink-install', calls)
        self.assertNotIn('docker compose', calls)

    def test_old_deploy_command_and_overrides(self):
        result, calls = self.run_script('deploy', 'host-a', script='boat_hosts.sh', BOAT_REPOS='repo_y')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('for repo in astro_dock repo_y', calls)
        self.assertIn('colcon build --symlink-install', calls)

    def test_deploy_image_commits_a_rollback_before_compose(self):
        result, calls = self.run_script('apply', '--image', 'repo/img@sha256:abc', 'host-a')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(calls.index('docker commit roboboat_dev astro-rollback:'),
                        calls.index('ASTRO_IMAGE=repo/img@sha256:abc docker compose -p astro_dock_devcontainer'))
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

    def test_deploy_without_hosts_takes_the_ssh_hosts_of_the_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp, 'devices.yaml')
            registry.write_text(REGISTRY)
            result, calls = self.run_script('apply', ASTRO_REGISTRY=str(registry))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('== a bash -s', calls)
        self.assertIn('== b docker exec -i box bash -s', calls)
        self.assertNotIn('== r ', calls)

    def test_the_shipped_registry_names_hosts_to_deploy(self):
        result, calls = self.run_script('plan')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('== tranquility bash -s', calls)
        self.assertNotIn('== router ', calls)

    def test_keys_creates_the_key_once_and_installs_it_only_where_the_tui_is_refused(self):
        # This ssh refuses the TUI's strict check for host a (not installed yet) and accepts b.
        refusing = ('#!/usr/bin/env bash\necho "== $*" >> "$FAKE_SSH_LOG"\n'
                    'case "$*" in *StrictHostKeyChecking=yes*roboboat@192.168.0.152*) exit 255;; esac\n')
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in (('ssh', refusing), ('copy', FAKE_SSH)):
                Path(tmp, name).write_text(text)
                Path(tmp, name).chmod(0o755)
            Path(tmp, 'boat-network.env').write_text(
                'GATEWAY_IP=192.168.0.2\nSTATIC_LEASES="aa=blastoise-odroid=192.168.0.152 '
                'bb=blastoise-jetson=192.168.0.153"\n')
            devices = Path(tmp, 'devices.yaml')
            devices.write_text('defaults: {user: roboboat, ssh: true}\nhosts:\n'
                               '  blastoise-odroid: {}\n  blastoise-jetson: {}\n'
                               '  tranquility: {user: peace, address: $GATEWAY_IP}\n'
                               '  router: {address: 192.168.0.1, ssh: false}\n')
            Path(tmp, 'xdg/mhseals').mkdir(parents=True)
            Path(tmp, 'xdg/mhseals/devices.yaml').write_text('self: tranquility\n')
            log = Path(tmp, 'ssh.log')
            env = {**os.environ, 'HW_SSH': str(Path(tmp, 'ssh')), 'FAKE_SSH_LOG': str(log),
                   'HW_SSH_COPY_ID': str(Path(tmp, 'copy')), 'XDG_STATE_HOME': tmp, 'ASTRO_ENV_FILE': '/dev/null',
                   'XDG_CONFIG_HOME': str(Path(tmp, 'xdg')), 'BOAT_DEVICES': str(devices),
                   'BOAT_ENV': str(Path(tmp, 'boat-network.env'))}
            run = lambda: subprocess.run([str(ROOT / 'boat_hosts.sh'), 'keys'], capture_output=True,
                                         text=True, env=env)
            result = run()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            key = Path(tmp, 'xdg/mhseals/ssh/id_ed25519')
            first = key.read_text()
            calls = log.read_text()
            self.assertIn(f'== -i {key}.pub roboboat@192.168.0.152', calls)          # ssh-copy-id
            self.assertIn('StrictHostKeyChecking=accept-new roboboat@192.168.0.152 true', calls)
            self.assertNotIn(f'.pub roboboat@192.168.0.153', calls)                  # already reached
            self.assertNotIn('192.168.0.2', calls)                                   # self: tranquility
            self.assertNotIn('192.168.0.1 ', calls)                                  # no ssh
            self.assertEqual(run().returncode, 0)
            self.assertEqual(key.read_text(), first)                                 # kept, not replaced

    def test_usage_without_a_verb(self):
        result, _ = self.run_script(script='boat_hosts.sh')
        self.assertEqual(result.returncode, 2)
        self.assertIn('deploy [--image REF]', result.stderr)


if __name__ == '__main__':
    unittest.main()
