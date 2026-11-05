"""setup.linux.sh --components repos: clone and build this computer's repositories (hosts.yaml repos.all and
repos.ROLE of its roles, from sources) on the fixture fleet tests/data/profiles.

Runs the real setup.linux.sh, hw.sh and devices/ copied into a scratch workspace; git clone (but hw.sh's
clone of a configured profile source, the fixture fleet as a git repository), colcon and docker are
stand-ins that log, so nothing is fetched or built. The build is hw.sh build's (devices/build.sh), the one build.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/data/profiles'
# clone: log, and create the target with one package NAME_pkg (a clone into profiles/ is real); the rest is real git.
GIT = '''#!/usr/bin/env bash
if [ "$1" = clone ]; then
    echo "git $*" >> "$LOG"
    case "$*" in *unpublished*) exit 128 ;; esac
    case "${@: -1}" in */profiles) ;; *) d=${@: -1}; mkdir -p "$d/pkg"
        echo "<package><name>${d##*/}_pkg</name></package>" > "$d/pkg/package.xml"; exit 0 ;; esac
fi
exec %s "$@"
'''
# build logs its directory and arguments.
COLCON = '''#!/usr/bin/env bash
echo "colcon $PWD $*" >> "$LOG"
'''
# ps prints the container id when the filters name the Compose project's dev service.
DOCKER = '''#!/usr/bin/env bash
echo "docker $*" >> "$LOG"
[ "$1" != exec ] || cat > /dev/null
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
        shutil.copy2(ROOT / 'colcon_defaults.yaml', self.ws)
        (self.ws / '.devcontainer').mkdir()
        (self.ws / '.devcontainer/dev.bashrc').write_text('')
        (self.ws / 'src/beta/pkg').mkdir(parents=True)       # already checked out: kept
        (self.ws / 'src/beta/pkg/package.xml').write_text('<package>\n  <name>beta_pkg</name>\n</package>\n')
        (self.ws / 'src/beta/off/pkg').mkdir(parents=True)   # under COLCON_IGNORE: colcon never sees it
        (self.ws / 'src/beta/off/COLCON_IGNORE').write_text('')
        (self.ws / 'src/beta/off/pkg/package.xml').write_text('<package><name>off_pkg</name></package>\n')
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
                   PROJECT='fixture_project', XDG_CONFIG_HOME=str(self.tmp / 'config'),
                   ASTRO_SECRETS=str(self.tmp / 'none'), HW_HOSTNAME='innovation',
                   HW_SYSFS_NET=str(self.tmp / 'nosys'), HW_ADDRS='', HW_DT_MODEL=str(self.tmp / 'no-model'),
                   **extra)
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

    def test_a_repo_not_published_yet_is_skipped_and_the_rest_build(self):
        run, calls = self.setup('--host', 'a-pc', HW_REPOS_ALL='alpha later',
                                HW_SOURCES_LATER_URL='https://example.invalid/unpublished.git')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('Cannot clone https://example.invalid/unpublished.git yet; skipping src/later', run.stderr)
        self.assertEqual(calls[-1], 'colcon %s build --symlink-install --packages-up-to alpha_pkg' % self.ws)
        self.assertNotIn('/src/later/', self.excluded())
        self.assertIn('repos:later', run.stdout)

    def test_without_colcon_it_builds_in_the_compose_projects_dev_container(self):
        run, calls = self.setup('--host', 'a-pc', colcon=False, running='c0ffee')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('docker ps -q --filter label=com.docker.compose.project=fixture_project '
                      '--filter label=com.docker.compose.service=dev', calls)
        log = '\n'.join(calls)       # hw.sh build in the container: its ROS_WS, its image stamp
        self.assertIn('docker inspect --format {{.Image}} c0ffee', calls)
        self.assertIn('-e ASTRO_BUILD_IMAGE= c0ffee bash -s -- --packages-up-to alpha_pkg', log)
        self.assertNotIn('ASTRO_BUILD_WS', log)
        run, calls = self.setup('--host', 'a-pc', colcon=False, running='')
        self.assertIn('Start the dev container (the Compose dev service), then build: '
                      './hw.sh build --container NAME -- --packages-up-to alpha_pkg', run.stderr)
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
        run, _ = self.setup('--dry-run', '--host', 'op')                    # the station: operator
        self.assertEqual(listed(run), ['alpha', 'gamma'])
        run, _ = self.setup('--dry-run', '--host', 'somelaptop')            # a generic control station
        self.assertEqual(listed(run), ['alpha', 'gamma'])
        run, _ = self.setup('--dry-run', '--vessel', 'a', '--role', 'camera')
        self.assertEqual(listed(run), ['alpha', 'beta', 'delta'])

    def test_a_repo_without_a_source_is_refused_before_any_clone(self):
        run, calls = self.setup('--host', 'a-pc', HW_REPOS_ALL='nosuch')
        self.assertIn('sources.nosuch has no url', run.stderr)
        self.assertIn('Skipped:   repos', run.stdout)
        self.assertEqual(calls, [])

    def test_a_configured_profile_source_is_checked_out_first(self):
        source = self.tmp / 'fleet-repo'
        shutil.copytree(FIXTURE, source)
        env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 't',
               'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}
        for args in (('init', '-q', '-b', 'main'), ('add', '-A'), ('commit', '-q', '-m', 'fleet')):
            subprocess.run(['git', '-C', str(source), *args], check=True, capture_output=True, env=env)
        (self.tmp / 'config/astro').mkdir(parents=True)
        (self.tmp / 'config/astro/profiles.env').write_text('ASTRO_PROFILES_URL=%s\n' % source)
        run, calls = self.setup('--host', 'a-pc', profiles=None)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(calls[:2], ['git clone -q --no-checkout -- %s %s/profiles' % (source, self.ws),
                                     'git clone --branch main https://example.invalid/alpha.git %s/src/alpha' % self.ws])
        self.assertTrue((self.ws / 'profiles/a/profile.yaml').is_file())
        self.assertIn('alpha https://example.invalid/alpha.git main', run.stdout)

if __name__ == '__main__':
    unittest.main()
