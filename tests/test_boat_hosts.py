import importlib.util
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


class BoatHosts(unittest.TestCase):
    def run_script(self, *args, **env):
        with tempfile.TemporaryDirectory() as tmp:
            ssh = Path(tmp, 'ssh')
            ssh.write_text(FAKE_SSH)
            ssh.chmod(0o755)
            log = Path(tmp, 'ssh.log')
            log.touch()
            result = subprocess.run(
                [str(ROOT / 'boat_hosts.sh'), *args], capture_output=True, text=True,
                env={**os.environ, 'BOAT_HOSTS_SSH': str(ssh), 'FAKE_SSH_LOG': str(log),
                     'XDG_STATE_HOME': tmp, **env})
            return result, log.read_text()

    def test_deploy_fast_forwards_configured_repos_and_builds(self):
        result, calls = self.run_script('deploy', 'host-a', BOAT_REPOS='repo_x')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('== host-a bash -s', calls)
        self.assertIn('for repo in astro_dock repo_x', calls)
        self.assertIn('git merge --ff-only -q origin/main', calls)
        self.assertIn('== host-a docker exec -i roboboat_dev bash -s', calls)
        self.assertIn('find build install -xtype l -delete', calls)
        self.assertIn('colcon build --symlink-install', calls)
        self.assertNotIn('docker compose', calls)

    def test_deploy_image_commits_a_rollback_before_compose(self):
        result, calls = self.run_script('deploy', '--image', 'repo/img@sha256:abc', 'host-a')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(calls.index('docker commit roboboat_dev astro-rollback:'),
                        calls.index('ASTRO_IMAGE=repo/img@sha256:abc docker compose'))
        self.assertIn('.devcontainer/prebuild.sh', calls)

    def test_deploy_without_hosts_takes_the_ssh_hosts_of_the_device_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            devices = Path(tmp, 'devices.yaml')
            devices.write_text('defaults: {ssh: true}\nhosts:\n  a: {}\n  r: {ssh: false}\n  b: {user: x}\n')
            result, calls = self.run_script('deploy', BOAT_DEVICES=str(devices))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('== a bash -s', calls)
        self.assertIn('== b bash -s', calls)
        self.assertNotIn('== r ', calls)

    def test_the_shipped_registry_names_hosts_to_deploy(self):
        result = subprocess.run(['bash', '-c', f'source <(sed -n "/^deploy_hosts()/,/^}}/p" {ROOT}/boat_hosts.sh); '
                                f'BOAT_DEVICES={ROOT}/config/devices.yaml deploy_hosts'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('tranquility', result.stdout.split())
        self.assertNotIn('router', result.stdout.split())

    def test_usage_without_a_verb(self):
        result, _ = self.run_script()
        self.assertEqual(result.returncode, 2)
        self.assertIn('deploy [--image REF]', result.stderr)


if __name__ == '__main__':
    unittest.main()
