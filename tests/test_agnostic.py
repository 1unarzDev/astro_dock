"""astro_dock is a generic ROS container workspace: no tracked file names a boat, fleet or LAN of one robot
team. Those belong in the profiles/ submodule; test data uses the fixture fleet (tests/data/profiles).

PENDING lists the files that still hit, each with the deferred group that keeps it. Fix a file and drop its
entry: a PENDING entry without a hit fails, so the list only shrinks.
"""
import os
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPECIFIC = re.compile(r'blastoise|squirtle|tranquility|roboboat|mhseals|mhsboat|odroid|192\.168\.0\.'
                      r'|\bdomain\W{0,3}42\b', re.IGNORECASE)
SUBMODULES = ('profiles', 'src/ros_tcp_endpoint')
ALLOWED = ('tests/data/',)
SELF = 'tests/test_agnostic.py'

# The deferred container-identity group: values the code itself still hard-codes.
IMAGE = 'image user and workspace roboboat (/home/roboboat/roboboat_ws)'
CONTAINER = 'container roboboat_dev'
HOSTNAME = 'hostname roboboat'
DATA = 'MHSEALS_DATA'
CONFIG = '~/.config/mhseals'
KEEPER = 'the mhseals keeper pattern'
GITMODULES = '.gitmodules URLs'
POINTER = 'the profiles/ pointer in README'
HOOK_TABLE = 'mhseals_testing agent-hook regression table'

PENDING = {
    '.devcontainer/Dockerfile.dev': IMAGE,
    '.devcontainer/Dockerfile.jetson': IMAGE,
    '.devcontainer/boat-validate.sh': ' + '.join((IMAGE, CONTAINER)),
    '.devcontainer/devcontainer.json': IMAGE,
    '.devcontainer/docker-compose.build.yml': IMAGE,
    '.devcontainer/docker-compose.override.immutable.yml': IMAGE,
    '.devcontainer/docker-compose.yml': ' + '.join((IMAGE, CONTAINER, HOSTNAME, DATA, CONFIG)),
    '.devcontainer/image-smoke.sh': IMAGE,
    '.devcontainer/image-user.sh': IMAGE,
    '.devcontainer/prebuild.sh': ' + '.join((CONFIG, CONTAINER, HOSTNAME)),
    '.devcontainer/run_env.py': ' + '.join((IMAGE, DATA)),
    '.devcontainer/stop-children.sh': KEEPER,
    '.github/scripts/check-containers.py': HOSTNAME,
    '.github/scripts/test-jetson-validation.py': CONTAINER,
    '.github/workflows/images.yml': IMAGE,
    '.gitmodules': GITMODULES,
    'README.md': ' + '.join((IMAGE, CONTAINER, DATA, CONFIG, GITMODULES, POINTER)),
    'config/run.yaml': IMAGE,
    'tests/test_agents_hook.py': ' + '.join((HOOK_TABLE, DATA)),
    'tests/test_run_env.py': DATA,
}


def tracked():
    run = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'], capture_output=True, check=True)
    for name in run.stdout.decode().split('\0'):
        path = ROOT / name
        if (name and not name.startswith(tuple(s + '/' for s in SUBMODULES)) and name not in SUBMODULES
                and not os.path.islink(path) and path.is_file()):
            yield name, path


def hits():
    """{path: ['path:line: text', ...]} for every tracked text file outside ALLOWED and this file."""
    found = {}
    for name, path in tracked():
        if name == SELF or name.startswith(ALLOWED):
            continue
        data = path.read_bytes()
        if b'\0' in data:
            continue                                    # binary
        for number, line in enumerate(data.decode(errors='replace').splitlines(), 1):
            if SPECIFIC.search(line):
                found.setdefault(name, []).append('%s:%d: %s' % (name, number, line.strip()[:160]))
    return found


class Agnostic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hits = hits()

    def test_no_boat_specific_literal_outside_the_profiles_and_test_data(self):
        unexpected = [line for name, lines in sorted(self.hits.items()) if name not in PENDING for line in lines]
        self.assertEqual(unexpected, [], 'boat-specific literals (move them to the profiles submodule or '
                                         'the fixture fleet):\n' + '\n'.join(unexpected))

    def test_pending_only_shrinks(self):
        stale = sorted(name for name in PENDING if name not in self.hits)
        self.assertEqual(stale, [], 'PENDING entries without a hit: drop them')
        for name, reason in PENDING.items():
            with self.subTest(path=name):
                self.assertTrue(reason.strip(), 'a PENDING entry names its deferred group')


if __name__ == '__main__':
    unittest.main()
