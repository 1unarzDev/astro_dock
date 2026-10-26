import os
from pathlib import Path
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.devcontainer'))
import astro_banner  # noqa: E402
import astro_banner_stats as stats  # noqa: E402

ANSI = re.compile(r'\x1b\[[0-9;]*m')
CHEAP = ['ros', 'dds', 'workspace', 'time', 'disk', 'hw', 'host', 'system']


def plain(text):
    return ANSI.sub('', text)


class BannerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {'XDG_RUNTIME_DIR': self.tmp.name, 'ROS_WS': str(ROOT), 'ROS_DISTRO': 'jazzy',
                    'ROS_DOMAIN_ID': '7', 'ASTRO_DDS_ADDRESS': '10.0.1.10', 'TERM': 'xterm-256color'}
        self.config = dict(astro_banner.load_config([ROOT / 'config' / 'banner.yaml']), stats=CHEAP)

    def tearDown(self):
        self.tmp.cleanup()

    def render(self, cols, rows, **config):
        return astro_banner.render(self.env, cols, rows, dict(self.config, **config))

    def test_art_follows_terminal_size(self):
        cases = {(140, 40): ('╚█████╗', '⠸⠟⠅⠁'), (90, 30): ('█▀█ ▄▄█', '⢀⢀⢄⠔⢔'), (60, 20): ('✦ █▀█ ▄▄█', None),
                 (60, 8): ('✦ ASTRO DOCK', None)}
        for (cols, rows), (wordmark, art) in cases.items():
            text = plain(self.render(cols, rows))
            self.assertIn(wordmark, text, (cols, rows))
            if art:
                self.assertIn(art, text)
            else:
                self.assertFalse(re.search('[⠁-⣿]', text))
            lines = text.splitlines()
            self.assertLessEqual(max(len(line) for line in lines), cols)
            self.assertLessEqual(len(lines), rows - 2)
            self.assertIn('│ ros       jazzy · domain 7', text)
        self.assertEqual(self.render(45, 40), '')
        self.assertEqual(self.render(140, 4), '')

    def test_stats_sit_beside_wide_art_and_below_narrow(self):
        wide = plain(self.render(140, 40)).splitlines()
        self.assertTrue(any(re.search('[⠁-⣿]', line) and '│ dds' in line for line in wide))
        narrow = plain(self.render(60, 20)).splitlines()
        first_stat = next(i for i, line in enumerate(narrow) if '│ ros' in line)
        self.assertGreater(first_stat, next(i for i, line in enumerate(narrow) if '█▀█' in line))

    def test_colour_and_charset_fallbacks(self):
        self.assertIn('38;2;', self.render(90, 30, color='truecolor'))
        self.assertIn('38;5;', self.render(90, 30, color='256'))
        self.assertNotIn('38;', self.render(90, 30, color='16'))
        self.assertNotIn('\x1b', self.render(90, 30, color='none'))
        self.env['NO_COLOR'] = '1'
        self.assertNotIn('\x1b', self.render(90, 30))
        text = self.render(90, 30, charset='ascii')
        text.encode('ascii')
        self.assertEqual([len(line) for line in text.splitlines()],
                         [len(line) for line in self.render(90, 30, charset='unicode').splitlines()])

    def test_ascii_fallback_triggers(self):
        self.assertTrue(astro_banner.wants_ascii('auto', {'LANG': 'C'}))
        self.assertTrue(astro_banner.wants_ascii('unicode', {'ASTRO_BANNER_CHARSET': 'ascii'}))
        self.assertFalse(astro_banner.wants_ascii('ascii', {'ASTRO_BANNER_CHARSET': 'unicode'}))
        self.assertFalse(astro_banner.wants_ascii('unicode', {'LANG': 'C'}))
        self.env['LANG'] = 'C'
        self.render(90, 30, charset='auto').encode('ascii')

    def test_cache_ttl(self):
        cache = {'nodes': {'t': 100.0, 'key': '42//', 'value': 3}}
        self.assertFalse(stats.stale(cache, 'nodes', '42//', 30, 120))
        self.assertTrue(stats.stale(cache, 'nodes', '42//', 30, 131))
        self.assertTrue(stats.stale(cache, 'nodes', '7//', 30, 120))
        self.assertTrue(stats.stale(cache, 'repos', str(ROOT), 30, 120))

    def test_cache_dir_falls_back_when_runtime_dir_is_unusable(self):
        # A missing $XDG_RUNTIME_DIR left the refresher unable to write: rows stayed pending for good.
        private = stats.cache_path({'XDG_RUNTIME_DIR': '/nonexistent/run'}).parent
        self.assertEqual(private, Path('/tmp/astro-%d' % os.getuid()))
        self.assertEqual(os.lstat(str(private)).st_mode & 0o777, 0o700)
        with tempfile.TemporaryDirectory() as run:
            self.assertEqual(stats.cache_path({'XDG_RUNTIME_DIR': run}).parent, Path(run))

    def test_only_stale_probes_refresh_and_cached_values_show(self):
        calls = []
        real = stats.refresh
        stats.refresh = lambda path, jobs: calls.append([job[0] for job in jobs])
        try:
            path = stats.cache_path(self.env)
            path.write_text('{"repos": {"t": %f, "key": "%s", "value": [{"name": "dock", "dirty": 2, "ahead": 0}]}}'
                            % (time.time(), ROOT))
            rows = dict(stats.collect(self.env, dict(self.config, stats=['repos', 'nodes']), ROOT, time.time()))
            self.assertEqual(calls, [['nodes']])
            self.assertIn(('✗ dock', 'warn'), rows['repos'])
            self.assertEqual(rows['nodes'], [('pending', 'dim')])
        finally:
            stats.refresh = real

    def test_timing_budget(self):
        script = ('import sys, os; sys.path.insert(0, %r); import astro_banner as b; '
                  'b.render(dict(os.environ), 140, 40)' % str(ROOT / '.devcontainer'))
        best = 9.0
        for _ in range(5):
            start = time.perf_counter()
            subprocess.run([sys.executable, '-I', '-c', script], env=dict(os.environ, **self.env), check=True)
            best = min(best, time.perf_counter() - start)
        self.assertLess(best, 0.15)


class GateTest(unittest.TestCase):
    """astro-banner.sh runs the banner only for a person at an interactive terminal, once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gate = Path(self.tmp.name) / 'astro-banner.sh'
        shutil.copy(ROOT / '.devcontainer' / 'astro-banner.sh', self.gate)
        (Path(self.tmp.name) / 'astro_banner.py').write_text('print("BANNER" + "-RAN")\n')
        self.env = {'PATH': os.environ['PATH'], 'HOME': self.tmp.name, 'TERM': 'xterm-256color'}

    def tearDown(self):
        self.tmp.cleanup()

    def tty(self, commands, **env):
        master, slave = pty.openpty()
        proc = subprocess.Popen(['bash', '--norc', '--noprofile', '-i'], stdin=slave, stdout=slave, stderr=slave,
                                env=dict(self.env, **env), start_new_session=True)
        os.close(slave)
        os.write(master, (commands + '\nexit\n').encode())
        out = b''
        while select.select([master], [], [], 5)[0]:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        os.close(master)
        proc.wait(5)
        return out.decode(errors='replace').count('BANNER-RAN')

    def test_interactive_terminal_shows_once(self):
        self.assertEqual(self.tty('source %s; source %s' % (self.gate, self.gate)), 1)

    def test_opt_outs_agents_ssh_and_dumb_terminals_skip(self):
        for env in ({'ASTRO_BANNER': '0'}, {'ASTRO_BANNER_SHOWN': '1'}, {'CLAUDECODE': '1'},
                    {'SSH_ORIGINAL_COMMAND': 'ros2 topic list'}, {'TERM': 'dumb'},
                    {'VSCODE_RESOLVING_ENVIRONMENT': '1'}):
            self.assertEqual(self.tty('source %s' % self.gate, **env), 0, env)

    def test_non_interactive_shells_skip(self):
        runs = [['bash', '-c', 'source %s' % self.gate],
                ['bash', '-c', 'true'],  # BASH_ENV path
                ['bash', '-i', '-c', 'source %s' % self.gate]]
        for argv in runs:
            out = subprocess.run(argv, env=dict(self.env, BASH_ENV=str(self.gate)), capture_output=True,
                                 text=True, stdin=subprocess.DEVNULL)
            self.assertNotIn('BANNER-RAN', out.stdout + out.stderr, argv)

    def test_gate_parses(self):
        subprocess.run(['bash', '-n', str(self.gate)], check=True)


if __name__ == '__main__':
    unittest.main()
