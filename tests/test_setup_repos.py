"""setup.linux.sh --components repos: clone and build the selected profile's repositories (hosts.yaml repos, sources).

Runs the real setup.linux.sh, hw.sh and devices/ copied into a scratch workspace with its own profile;
git clone, colcon and docker are stand-ins that log, so nothing is fetched or built.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
# A competition profile with the sources, and two boats: boat-1 picks two repos, boat-2 names one with no source.
PROFILES = {
    'team/hosts.yaml': 'driver: hosts/fleet\nrepos: [alpha]\nsources:\n'
                       '  alpha: {url: https://example.invalid/alpha.git}\n'
                       '  beta: {url: https://example.invalid/beta.git, branch: ros2}\n',
    'boat-1/profile.yaml': 'extends: team\nnetwork_env: config/net.env\n',
    'boat-1/hosts.yaml': 'repos: [alpha, beta]\n',
    'boat-2/profile.yaml': 'extends: team\n',
    'boat-2/hosts.yaml': 'repos: [gamma]\n',
}
# clone: log and create the target; everything else goes to the real git.
GIT = '''#!/usr/bin/env bash
if [ "$1" = clone ]; then echo "git $*" >> "$LOG"; mkdir -p "${@: -1}"; exit 0; fi
exec %s "$@"
'''
# list prints one package per base path; build logs its directory and arguments.
COLCON = '''#!/usr/bin/env bash
if [ "$1" = list ]; then shift 3; for p in "$@"; do echo "${p##*/}_pkg"; done; exit 0; fi
echo "colcon $PWD $*" >> "$LOG"
'''
DOCKER = '''#!/usr/bin/env bash
echo "docker $*" >> "$LOG"
[ "$1" = inspect ] && echo "$RUNNING"
exit 0
'''


class SetupRepos(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.ws = self.tmp / 'ws'
        for name, text in PROFILES.items():
            (self.ws / 'config/profiles' / name).parent.mkdir(parents=True, exist_ok=True)
            (self.ws / 'config/profiles' / name).write_text(text)
        for name in ('setup.linux.sh', 'hw.sh'):
            shutil.copy2(ROOT / name, self.ws / name)
        shutil.copytree(ROOT / 'devices', self.ws / 'devices')
        (self.ws / 'config/net.env').write_text('ADDR=10.0.0.1\n')
        (self.ws / 'src/beta').mkdir(parents=True)           # already checked out: kept
        subprocess.run(['git', 'init', '-q', str(self.ws)], check=True)
        self.log = self.tmp / 'log'
        self.bin = self.tmp / 'bin'
        self.tool('git', GIT % shutil.which('git'))
        self.tool('docker', DOCKER)

    def tool(self, name, text, where=None):
        path = (where or self.bin) / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)

    def setup(self, *args, colcon=True, running='false', **extra):
        path = [str(self.bin)]
        if colcon:
            self.tool('colcon', COLCON, self.tmp / 'colcon')
            path.append(str(self.tmp / 'colcon'))
        env = {k: v for k, v in os.environ.items() if not k.startswith(('ASTRO_', 'HW_'))}
        env.update(PATH=':'.join(path + [os.environ['PATH']]), LOG=str(self.log), RUNNING=running,
                   ASTRO_ENV_FILE='/dev/null', **extra)
        run = subprocess.run(['bash', str(self.ws / 'setup.linux.sh'), '--components', 'repos', '--yes',
                              '--non-interactive', *args], capture_output=True, text=True, env=env, timeout=60)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        self.log.unlink(missing_ok=True)
        return run, calls

    def excluded(self):
        return (self.ws / '.git/info/exclude').read_text().splitlines()

    def test_clones_missing_repos_on_their_branch_and_builds_their_packages(self):
        run, calls = self.setup('--profile', 'boat-1')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('alpha https://example.invalid/alpha.git main', run.stdout)
        self.assertIn('beta https://example.invalid/beta.git ros2', run.stdout)
        self.assertIn('src/beta exists; keeping it.', run.stdout)
        self.assertEqual(calls, [
            'git clone --branch main https://example.invalid/alpha.git %s/src/alpha' % self.ws,
            'colcon %s build --symlink-install --packages-up-to alpha_pkg beta_pkg' % self.ws])
        self.assertTrue((self.ws / 'src/alpha').is_dir())
        self.assertEqual(self.excluded().count('/src/alpha/'), 1)
        self.assertEqual(self.excluded().count('/src/beta/'), 1)
        self.assertIn('Completed: repos', run.stdout)
        run, calls = self.setup('--profile', 'boat-1')              # rerun: nothing cloned, nothing re-excluded
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse([c for c in calls if c.startswith('git clone')])
        self.assertEqual(self.excluded().count('/src/alpha/'), 1)

    def test_without_colcon_it_builds_in_the_running_dev_container(self):
        run, calls = self.setup('--profile', 'boat-1', colcon=False, running='true')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('docker exec roboboat_dev bash -c cd "$ROS_WS" && colcon build --symlink-install '
                      '--packages-up-to $(colcon list --names-only --base-paths src/alpha src/beta)', calls)
        run, calls = self.setup('--profile', 'boat-1', colcon=False, running='false')
        self.assertIn('Start the dev container (roboboat_dev), then build inside it', run.stderr)
        self.assertFalse([c for c in calls if c.startswith('docker exec')])

    def test_dry_run_changes_nothing(self):
        run, calls = self.setup('--profile', 'boat-1', '--dry-run')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('git clone --branch main https://example.invalid/alpha.git', run.stdout)
        self.assertEqual(calls, [])
        self.assertFalse((self.ws / 'src/alpha').exists())
        self.assertNotIn('/src/alpha/', self.excluded())

    def test_profile_selection_default_environment_and_flag(self):
        run, _ = self.setup('--dry-run')                         # the one profile naming a network env
        self.assertIn('beta https://example.invalid/beta.git ros2', run.stdout)
        run, _ = self.setup('--dry-run', ASTRO_PROFILE='boat-2')
        self.assertIn('sources.gamma has no url', run.stderr)
        run, _ = self.setup('--dry-run', '--profile', 'boat-1', ASTRO_PROFILE='boat-2')
        self.assertIn('beta https://example.invalid/beta.git ros2', run.stdout)
        run, _ = self.setup('--dry-run', '--profile', 'team')    # the parent alone: its own repos
        self.assertIn('alpha https://example.invalid/alpha.git main', run.stdout)
        self.assertNotIn('beta', run.stdout)

    def test_a_repo_without_a_source_is_refused_before_any_clone(self):
        run, calls = self.setup('--profile', 'boat-2')
        self.assertIn('sources.gamma has no url', run.stderr)
        self.assertIn('Skipped:   repos', run.stdout)
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
