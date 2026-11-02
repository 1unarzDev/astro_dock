"""hw.sh overlay --dry-run and hw.sh pyenv on a throwaway git repository: no colcon, nothing built."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, ASTRO_SECRETS='/nonexistent', ASTRO_ENV_FILE='/dev/null', PYTHONPATH='/base',
           GIT_CONFIG_GLOBAL='/dev/null', GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
           GIT_COMMITTER_EMAIL='t@t')


def package(root, path, build_type, cmake=False):
    (root / path).mkdir(parents=True)
    name = Path(path).name
    (root / path / 'package.xml').write_text(
        f'<package><name>{name}</name><export><build_type>{build_type}</build_type></export></package>\n')
    if cmake:
        (root / path / 'CMakeLists.txt').write_text('project(x)\n')


def hw(*args):
    return subprocess.run([str(ROOT / 'hw.sh'), *args], env=ENV, capture_output=True, text=True, timeout=60)


class OverlayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name).resolve()
        package(self.repo, 'nav/planner', 'ament_cmake', cmake=True)
        package(self.repo, 'tools/py_tool', 'ament_python')
        package(self.repo, 'msgs', 'ament_cmake', cmake=True)
        git = ['git', '-C', str(self.repo)]
        subprocess.run([*git, 'init', '-q', '-b', 'main'], check=True, env=ENV)
        subprocess.run([*git, 'add', '.'], check=True, env=ENV)
        subprocess.run([*git, 'commit', '-qm', 'base'], check=True, env=ENV)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_selects_touched_packages(self):
        (self.repo / 'tools/py_tool/new.py').write_text('x = 1\n')         # untracked
        (self.repo / 'msgs/CMakeLists.txt').write_text('project(y)\n')     # modified
        run = hw('overlay', str(self.repo), '--base', 'main', '--dry-run')
        self.assertEqual(run.returncode, 0, run.stderr)
        line = next(l for l in run.stdout.splitlines() if l.startswith('overlay: '))
        selected = line.split('--packages-select ')[1].split()
        self.assertEqual(sorted(selected), ['msgs', 'py_tool'])
        self.assertIn('--symlink-install', line)
        self.assertIn(f'--install-base {self.repo}/install', line)
        self.assertIn(f'source {self.repo}/install/setup.bash', run.stdout)

    def test_nothing_touched_and_unknown_base(self):
        run = hw('overlay', str(self.repo), '--base', 'main', '--dry-run')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('No package', run.stdout)
        run = hw('overlay', str(self.repo), '--base', 'no-such-ref', '--dry-run')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('building all', run.stderr)
        selected = run.stdout.split('--packages-select ')[1].splitlines()[0].split()
        self.assertEqual(sorted(selected), ['msgs', 'planner', 'py_tool'])

    def test_pyenv_lists_pure_python_packages(self):
        run = hw('pyenv', str(self.repo))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), f'{self.repo}/tools/py_tool:/base')
        self.assertIn('Usage', hw('overlay', '--help').stdout)


if __name__ == '__main__':
    unittest.main()
