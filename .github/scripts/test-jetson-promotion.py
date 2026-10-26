#!/usr/bin/env python3
"""Exercise local promotion ordering with isolated GitHub and SSH commands."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
DIGEST = 'sha256:' + 'a' * 64


class JetsonPromotionTest(unittest.TestCase):
    """Keep failed validation or provenance checks from publishing releases."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.log = root / 'commands'
        self.env = dict(os.environ, GH_REPO='example/astro',
                        MOCK_LOG=str(self.log), MOCK_DIGEST=DIGEST)
        self.env['PATH'] = str(root) + os.pathsep + self.env['PATH']
        self.executable(root / 'gh', '''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
args = sys.argv[1:]
with open(os.environ['MOCK_LOG'], 'a') as stream:
    stream.write('gh ' + json.dumps(args) + '\\n')
family = 'jetson-jp6-jazzy'
if args[:2] == ['run', 'list']:
    if 'promote-jetson.yml' in args:
        print(json.dumps([dict(databaseId=456, createdAt='9999-01-01',
            displayTitle=family + ' promotion ' + os.environ['MOCK_DIGEST'])]))
    else:
        print(123)
elif args[0] == 'api':
    print(json.dumps(dict(path='.github/workflows/images.yml',
        head_branch=os.environ.get('MOCK_BRANCH', 'main'),
        conclusion='success', head_sha='abc123')))
elif args[:2] == ['run', 'download']:
    destination = Path(args[args.index('--dir') + 1]) / 'candidate.json'
    destination.write_text(json.dumps(dict(family=family,
        revision=os.environ.get('MOCK_REVISION', 'abc123'),
        digest=os.environ['MOCK_DIGEST'])))
elif args[:2] == ['run', 'watch']:
    sys.exit(int(os.environ.get('MOCK_CI_FAIL', '0')))
''')
        self.executable(root / 'ssh', '''#!/usr/bin/env python3
import json
import os
import shlex
import sys
args = sys.argv[1:]
with open(os.environ['MOCK_LOG'], 'a') as stream:
    stream.write('ssh ' + json.dumps(args) + '\\n')
# Unwrap the outer command exactly as Tranquility's shell does.
command = args[-1]
if command.startswith('ssh '):
    command = shlex.split(command)[-1]
with open(os.environ['MOCK_LOG'], 'a') as stream:
    stream.write('remote ' + command + '\\n')
if command.startswith('mktemp '):
    print('/tmp/astro-jetson.ABC123')
elif command.startswith('cat > '):
    sys.stdin.read()
elif command.startswith('bash '):
    if os.environ.get('MOCK_CAMERA_FAIL') == '1':
        sys.exit(1)
    print('PASS: CUDA initialized as normal user; devices: 1')
    print('PASS: received real images: three frames')
elif command.startswith('docker pull '):
    digest = os.environ.get('MOCK_PULLED_DIGEST', os.environ['MOCK_DIGEST'])
    print(json.dumps(['lunarzdev/astro@' + digest]))
''')

    @staticmethod
    def executable(path, content):
        """Create an isolated executable substitute."""
        path.write_text(content)
        path.chmod(0o755)

    def promote(self, *arguments):
        """Run the real helper without contacting GitHub or the boat."""
        return subprocess.run(
            ['bash', str(ROOT / 'hw.sh'), 'jetson', 'apply', *arguments],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=10)

    def commands(self):
        """Return the recorded external calls."""
        return self.log.read_text() if self.log.exists() else ''

    def test_success_validates_then_promotes_then_pulls_and_cleans(self):
        result = self.promote('roboboat@jetson.local')
        self.assertEqual(result.returncode, 0, result.stdout)
        commands = self.commands()
        self.assertLess(commands.index('boat-validate.sh lunarzdev/astro@'),
                        commands.index('"workflow", "run"'))
        self.assertLess(commands.index('"run", "watch"'),
                        commands.index('docker pull'))
        self.assertIn('tranquility', commands)
        self.assertIn('roboboat@jetson.local', commands)
        self.assertIn('rm -rf -- /tmp/astro-jetson.ABC123', commands)

    def test_failed_camera_never_dispatches_and_cleans(self):
        self.env['MOCK_CAMERA_FAIL'] = '1'
        result = self.promote('user@jetson')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('"workflow", "run"', self.commands())
        self.assertIn('rm -rf -- /tmp/astro-jetson.ABC123', self.commands())

    def test_failed_hosted_promotion_never_pulls_release(self):
        self.env['MOCK_CI_FAIL'] = '1'
        result = self.promote('user@jetson')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('docker pull', self.commands())

    def test_mismatched_pull_is_reported_as_failure(self):
        self.env['MOCK_PULLED_DIGEST'] = 'sha256:' + 'b' * 64
        self.assertNotEqual(self.promote('user@jetson').returncode, 0)

    def test_invalid_provenance_never_contacts_hardware(self):
        for variable, value in [('MOCK_BRANCH', 'feature'),
                                ('MOCK_REVISION', 'different'),
                                ('MOCK_DIGEST', 'mutable-tag')]:
            with self.subTest(variable=variable):
                self.env[variable] = value
                self.log.write_text('')
                self.assertNotEqual(self.promote('user@jetson').returncode, 0)
                self.assertNotIn('ssh ', self.commands())
                del self.env[variable]
        self.env['MOCK_DIGEST'] = DIGEST

    def test_requires_host_and_rejects_shell_metacharacters(self):
        for arguments in [(), ('user@host;false',), ('a@b', '1', 'extra')]:
            self.assertNotEqual(self.promote(*arguments).returncode, 0)
            self.assertEqual(self.commands(), '')

    def test_plan_touches_no_host_and_rollback_needs_a_build(self):
        hw = lambda *a: subprocess.run(['bash', str(ROOT / 'hw.sh'), 'jetson', *a], env=self.env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10)
        result = hw('plan', 'user@jetson')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('Candidate lunarzdev/astro@' + DIGEST, result.stdout)
        self.assertNotIn('ssh ', self.commands())
        self.assertNotIn('"workflow", "run"', self.commands())
        self.log.write_text('')
        result = hw('rollback', 'user@jetson')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.commands(), '')

    def test_direct_ssh_and_explicit_build(self):
        self.env['HW_GATEWAY'] = ''
        result = self.promote('user@jetson', '123')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn('tranquility', self.commands())
        self.assertIn('user@jetson', self.commands())


if __name__ == '__main__':
    unittest.main()
