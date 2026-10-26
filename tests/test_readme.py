"""README.md names only hw.sh and setup.linux.sh verbs and flags that their --help prints (no network)."""
import os
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, ASTRO_SECRETS='/nonexistent', ASTRO_ENV_FILE='/dev/null',
           ASTRO_PROFILES=str(ROOT / 'tests/data/profiles'), ASTRO_VESSEL='a')   # the fixture fleet
TOP_VERBS = ('whoami', 'env', 'profile', 'secrets')


def run_help(*args):
    out = subprocess.run(args, cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=60)
    return out.stdout + out.stderr


def snippets(text):
    """Inline code spans and fenced code lines: the places a command is quoted."""
    fenced, out = False, []
    for line in text.splitlines():
        if line.lstrip().startswith('```'):
            fenced = not fenced
        elif fenced:
            out.append(line)
        else:
            out += re.findall(r'`([^`]+)`', line)
    return out


class ReadmeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hw_top = run_help('./hw.sh', '--help')
        cls.kinds = re.findall(r'^  (\w+)\s+\w+/\w+$', cls.hw_top, re.M)
        cls.kind_help = {k: run_help('./hw.sh', k, '--help') for k in cls.kinds}
        cls.hw_all = cls.hw_top + ''.join(cls.kind_help.values())
        cls.setup = run_help('./setup.linux.sh', '--help')
        text = (ROOT / 'README.md').read_text()
        cls.lines, cls.snippets = text.splitlines(), snippets(text)

    def test_help_ran(self):
        self.assertIn('hw.sh whoami', self.hw_top)
        self.assertIn('hosts', self.kinds)
        self.assertIn('--components', self.setup)

    def test_flags(self):
        """Every --flag on a README line that names hw.sh or setup.linux.sh is in that tool's --help."""
        for line in self.lines:
            helps = (self.hw_all if 'hw.sh' in line else '') + (self.setup if 'setup.linux.sh' in line else '')
            for flag in re.findall(r'(?<![\w-])--[a-z][a-z-]*', line if helps else ''):
                with self.subTest(line=line[:60], flag=flag):
                    self.assertTrue(re.search(rf'(?<![\w-]){re.escape(flag)}(?![\w-])', helps), 'not in --help')

    def test_components(self):
        listed = set(re.search(r'--components LIST\s+(\S+)', self.setup).group(1).split(','))
        for snippet in self.snippets:
            if 'setup.linux.sh' in snippet:
                for value in re.findall(r'--components[ =]([\w,-]+)', snippet):
                    with self.subTest(snippet=snippet):
                        self.assertLessEqual(set(value.split(',')), listed)

    def test_verbs(self):
        for snippet in self.snippets:
            for words in re.findall(r'hw\.sh((?:\s+[^\s|;&]+)*)', snippet):
                words = words.split()
                while words and words[0] in ('--vessel', '--host'):
                    words = words[2:]
                if not words or not re.fullmatch(r'[a-z]+', words[0]):
                    continue  # a placeholder (KIND), an option or nothing
                with self.subTest(snippet=snippet):
                    self.assertIn(words[0], TOP_VERBS + tuple(self.kinds))
                    if words[0] in self.kind_help and len(words) > 1 and re.fullmatch(r'[a-z]+', words[1]):
                        self.assertRegex(self.kind_help[words[0]], rf'\b{words[1]}\b')


if __name__ == '__main__':
    unittest.main()
