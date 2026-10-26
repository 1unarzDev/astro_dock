"""setup.linux.sh --components repos: clone and build this computer's repositories (hosts.yaml repos.all and
repos.ROLE of its roles, from sources) on the fixture fleet tests/data/profiles.

Runs the real setup.linux.sh, hw.sh and devices/ copied into a scratch workspace; git clone, git submodule,
colcon and docker are stand-ins that log, so nothing is fetched or built.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/data/profiles'
# clone: log and create the target; submodule update: log and check out the fixture; the rest is real git.
GIT = '''#!/usr/bin/env bash
if [ "$1" = clone ]; then echo "git $*" >> "$LOG"; mkdir -p "${@: -1}"; exit 0; fi
if [ "$1" = -C ] && [ "$3" = submodule ]; then echo "git $*" >> "$LOG"; cp -r "$FIXTURE/." "$2/profiles/"; exit 0; fi
exec %s "$@"
'''
# list prints one package per base path; build logs its directory and arguments.
COLCON = '''#!/usr/bin/env bash
if [ "$1" = list ]; then shift 3; for p in "$@"; do echo "${p##*/}_pkg"; done; exit 0; fi
echo "colcon $PWD $*" >> "$LOG"
'''
# ps prints the container id when the filters name the Compose project's dev service.
DOCKER = '''#!/usr/bin/env bash
echo "docker $*" >> "$LOG"
if [ "$1" = ps ]; then
    case "$*" in *"label=com.docker.compose.project=$PROJECT "*"label=com.docker.compose.service=dev"*) echo "$RUNNING" ;; esac
fi
exit 0
'''


class SetupRepos(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.ws = self.tmp / 'ws'
        self.ws.mkdir()
        for name in ('setup.linux.sh', 'hw.sh'):
            shutil.copy2(ROOT / name, self.ws / name)
        shutil.copytree(ROOT / 'devices', self.ws / 'devices')
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

    def setup(self, *args, colcon=True, running='', profiles=FIXTURE, **extra):
        path = [str(self.bin)]
        if colcon:
            self.tool('colcon', COLCON, self.tmp / 'colcon')
            path.append(str(self.tmp / 'colcon'))
        env = {k: v for k, v in os.environ.items() if not k.startswith(('ASTRO_', 'HW_'))}
        env.update(PATH=':'.join(path + [os.environ['PATH']]), LOG=str(self.log), RUNNING=running,
                   PROJECT='fixture_project', FIXTURE=str(FIXTURE), ASTRO_ENV_FILE='/dev/null',
                   ASTRO_SECRETS=str(self.tmp / 'none'), HW_HOSTNAME='innovation',
                   HW_SYSFS_NET=str(self.tmp / 'nosys'), **extra)
        if profiles:
            env['ASTRO_PROFILES'] = str(profiles)
        run = subprocess.run(['bash', str(self.ws / 'setup.linux.sh'), '--components', 'repos', '--yes',
                              '--non-interactive', *args], capture_output=True, text=True, env=env, timeout=60)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        self.log.unlink(missing_ok=True)
        return run, calls

    def excluded(self):
        return (self.ws / '.git/info/exclude').read_text().splitlines()

    def test_clones_the_hosts_missing_repos_on_their_branch_and_builds_their_packages(self):
        run, calls = self.setup('--host', 'a-cam')                  # all + camera (vessel a replaces camera)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('alpha https://example.invalid/alpha.git main', run.stdout)
        self.assertIn('beta https://example.invalid/beta.git ros2', run.stdout)
        self.assertIn('src/beta exists; keeping it.', run.stdout)
        self.assertEqual(calls, [
            'git clone --branch main https://example.invalid/alpha.git %s/src/alpha' % self.ws,
            'git clone --branch main https://example.invalid/delta.git %s/src/delta' % self.ws,
            'colcon %s build --symlink-install --packages-up-to alpha_pkg beta_pkg delta_pkg' % self.ws])
        self.assertTrue((self.ws / 'src/alpha').is_dir())
        self.assertEqual(self.excluded().count('/src/alpha/'), 1)
        self.assertEqual(self.excluded().count('/src/beta/'), 1)
        self.assertIn('Completed: repos', run.stdout)
        run, calls = self.setup('--host', 'a-cam')              # rerun: nothing cloned, nothing re-excluded
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse([c for c in calls if c.startswith('git clone')])
        self.assertEqual(self.excluded().count('/src/alpha/'), 1)

    def test_without_colcon_it_builds_in_the_compose_projects_dev_container(self):
        run, calls = self.setup('--host', 'a-pc', colcon=False, running='c0ffee')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('docker ps -q --filter label=com.docker.compose.project=fixture_project '
                      '--filter label=com.docker.compose.service=dev', calls)
        self.assertIn('docker exec c0ffee bash -c cd "$ROS_WS" && colcon build --symlink-install '
                      '--packages-up-to $(colcon list --names-only --base-paths src/alpha)', calls)
        run, calls = self.setup('--host', 'a-pc', colcon=False, running='')
        self.assertIn('Start the dev container (the Compose dev service), then build inside it', run.stderr)
        self.assertFalse([c for c in calls if c.startswith('docker exec')])
        run, calls = self.setup('--host', 'a-pc', colcon=False, running='c0ffee', ASTRO_COMPOSE_PROJECT='other')
        self.assertFalse([c for c in calls if c.startswith('docker exec')])

    def test_dry_run_changes_nothing(self):
        run, calls = self.setup('--host', 'a-pc', '--dry-run')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('git clone --branch main https://example.invalid/alpha.git', run.stdout)
        self.assertEqual(calls, [])
        self.assertFalse((self.ws / 'src/alpha').exists())
        self.assertNotIn('/src/alpha/', self.excluded())

    def test_selection_host_role_and_vessel(self):
        listed = lambda run: [l.split()[0] for l in run.stdout.splitlines() if l.startswith('  ') and not l.lstrip().startswith('$') and ' https:' in l]
        run, _ = self.setup('--dry-run')                                    # no self, no role: all
        self.assertEqual(listed(run), ['alpha'])
        run, _ = self.setup('--dry-run', '--role', 'operator')
        self.assertEqual(listed(run), ['alpha', 'gamma'])
        run, _ = self.setup('--dry-run', '--vessel', 'b', '--role', 'camera')  # the fleet's camera list
        self.assertEqual(listed(run), ['alpha', 'beta'])
        run, _ = self.setup('--dry-run', '--host', 's')                     # the station: operator
        self.assertEqual(listed(run), ['alpha', 'gamma'])
        run, _ = self.setup('--dry-run', '--profile', 'a', '--role', 'camera')   # the old flag, one release
        self.assertIn('--profile is now --vessel', run.stderr)
        self.assertEqual(listed(run), ['alpha', 'beta', 'delta'])

    def test_a_repo_without_a_source_is_refused_before_any_clone(self):
        run, calls = self.setup('--host', 'a-pc', HW_REPOS_ALL='nosuch')
        self.assertIn('sources.nosuch has no url', run.stderr)
        self.assertIn('Skipped:   repos', run.stdout)
        self.assertEqual(calls, [])

    def test_the_profiles_submodule_is_initialised_first(self):
        (self.ws / '.gitmodules').write_text('[submodule "profiles"]\n\tpath = profiles\n\turl = https://example.invalid/p.git\n')
        (self.ws / 'profiles').mkdir()
        run, calls = self.setup('--host', 'a-pc', profiles=None)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(calls[0], 'git -C %s submodule update --init profiles' % self.ws)
        self.assertIn('alpha https://example.invalid/alpha.git main', run.stdout)


if __name__ == '__main__':
    unittest.main()
