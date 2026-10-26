"""astro_dock is a generic ROS container workspace: no tracked file names a boat, fleet or LAN of one robot
team. Those belong in the profiles/ submodule; test data uses the fixture fleet (tests/data/profiles).

PENDING lists the files that still hit, each with the deferred groups that keep it. A group is scoped by a
pattern: a hit line of a listed file passes only when every boat literal on it lies inside a match of one
of the file's group patterns, so a listed file cannot gain new boat literals unnoticed. Fix a file and drop
its group: a group without a hit in its file fails, so the list only shrinks. A symlink is checked by its
target path.
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

# The deferred container-identity group and its neighbours: (what keeps it, the literal in its context).
IMAGE = ('image user and workspace roboboat (/home/roboboat/roboboat_ws)',
         r'/home/roboboat(?:/roboboat_ws)?|roboboat_ws|USERNAME\W+roboboat|(?:containerUser|remoteUser|user)\W+roboboat'
         r'|username="\$\{1:-roboboat\}"|id -[ug]n\)" = roboboat|runs as `roboboat`')
CONTAINER = ('container roboboat_dev', r'roboboat_dev')
HOSTNAME = ('hostname roboboat', r'hostname: roboboat|- roboboat:127\.0\.1\.1|roboboat:roboboat=127\.0\.1\.1'
                                 r'|base roboboat alias')
DATA = ('MHSEALS_DATA', r'MHSEALS_DATA')
CONFIG = ('~/.config/mhseals', r'\.config/mhseals')
KEEPER = ('the mhseals keeper pattern', r'mhseals_testing harness|mhseals\.\*harness')
GITMODULES = ('.gitmodules URLs', r'github\.com/MHSeals/[\w.-]+')
POINTER = ('the profiles/ pointer in README',
           r"MHSeals' fleet \(the roboboat fleet, the blastoise and squirtle vessels and the control_station station, "
           r"which the operator laptop tranquility resolves to\)|\[MHSeals/mhseals_profile\]")
HOOK_TABLE = ('mhseals_testing agent-hook regression table', r'ssh blastoise\b|mhseals_testing')
LEGACY_LINKS = ('legacy config/ links into the blastoise profile (astro-clock.py LEGACY_ENV, old callers)',
                r'^\.\./profiles/blastoise/(?:network\.env|devices\.yaml)$')

PENDING = {
    '.devcontainer/Dockerfile.dev': (IMAGE,),
    '.devcontainer/Dockerfile.jetson': (IMAGE,),
    '.devcontainer/boat-validate.sh': (IMAGE, CONTAINER),
    '.devcontainer/devcontainer.json': (IMAGE,),
    '.devcontainer/docker-compose.build.yml': (IMAGE,),
    '.devcontainer/docker-compose.override.immutable.yml': (IMAGE,),
    '.devcontainer/docker-compose.yml': (IMAGE, CONTAINER, HOSTNAME, DATA, CONFIG),
    '.devcontainer/image-smoke.sh': (IMAGE,),
    '.devcontainer/image-user.sh': (IMAGE,),
    '.devcontainer/prebuild.sh': (CONFIG, CONTAINER, HOSTNAME),
    '.devcontainer/run_env.py': (IMAGE, DATA),
    '.devcontainer/stop-children.sh': (KEEPER,),
    '.github/scripts/check-containers.py': (HOSTNAME,),
    '.github/scripts/test-jetson-validation.py': (CONTAINER,),
    '.github/workflows/images.yml': (IMAGE,),
    '.gitmodules': (GITMODULES,),
    'README.md': (IMAGE, CONTAINER, DATA, CONFIG, GITMODULES, POINTER),
    'config/boat-network.env': (LEGACY_LINKS,),
    'config/devices.yaml': (LEGACY_LINKS,),
    'config/run.yaml': (IMAGE,),
    'tests/test_agents_hook.py': (HOOK_TABLE, DATA, CONTAINER),
    'tests/test_run_env.py': (DATA,),
}


def tracked():
    """(name, text lines) of each tracked file outside the submodules; a symlink's one line is its target."""
    run = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'], capture_output=True, check=True)
    for name in run.stdout.decode().split('\0'):
        path = ROOT / name
        if not name or name.startswith(tuple(s + '/' for s in SUBMODULES)) or name in SUBMODULES:
            continue
        if os.path.islink(path):
            yield name, [os.readlink(path)]
        elif path.is_file():
            data = path.read_bytes()
            if b'\0' not in data:                       # not binary
                yield name, data.decode(errors='replace').splitlines()


def hits():
    """{path: [(number, line), ...]} for every hit line of a tracked text file outside ALLOWED and this file."""
    found = {}
    for name, lines in tracked():
        if name == SELF or name.startswith(ALLOWED):
            continue
        for number, line in enumerate(lines, 1):
            if SPECIFIC.search(line):
                found.setdefault(name, []).append((number, line))
    return found


def label(name, number, line):
    return '%s:%d: %s' % (name, number, line.strip()[:160])


class Agnostic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hits = hits()

    def test_no_boat_specific_literal_outside_the_profiles_and_test_data(self):
        unexpected = []
        for name, lines in sorted(self.hits.items()):
            scope = re.compile('|'.join('(?:%s)' % pattern for _, pattern in PENDING.get(name, ())) or r'(?!)')
            for number, line in lines:
                if SPECIFIC.search(scope.sub('', line)):
                    unexpected.append(label(name, number, line))
        self.assertEqual(unexpected, [], 'boat-specific literals (move them to the profiles submodule or the fixture '
                                         'fleet; a PENDING file allows only its groups\' patterns):\n' + '\n'.join(unexpected))

    def test_pending_only_shrinks(self):
        for name, groups in PENDING.items():
            for reason, pattern in groups:
                with self.subTest(path=name, group=reason):
                    self.assertTrue(reason.strip(), 'a PENDING group names what keeps it')
                    self.assertTrue(any(re.search(pattern, line) for _, line in self.hits.get(name, ())),
                                    'PENDING group without a hit in its file: drop it')

    def test_the_scope_catches_a_new_literal_in_a_pending_file(self):
        scope = re.compile('|'.join('(?:%s)' % p for _, p in PENDING['.devcontainer/docker-compose.yml']))
        self.assertFalse(SPECIFIC.search(scope.sub('', '      user: roboboat')))
        self.assertTrue(SPECIFIC.search(scope.sub('', '      ROS_DOMAIN_ID: 42  # blastoise')))


if __name__ == '__main__':
    unittest.main()
