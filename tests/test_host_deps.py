"""Every external command the station-side tools call is a host dependency in config/host-deps.yaml
(installed by setup.linux.sh and setup.mac.sh) or on its allowlist (shell builtins, coreutils, other stages)."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONF = yaml.safe_load((ROOT / 'config/host-deps.yaml').read_text())
KEYWORDS = set('if then else elif fi for while until do done case esac in function select time coproc { } ! [[ ]] [ ]'.split())
BUILTINS = set('cd echo printf read exit return export local declare readonly unset set shift source . : eval exec trap '
               'wait break continue getopts let type command builtin alias umask ulimit pushd popd test true false '
               'mapfile readarray hash jobs fg bg disown'.split())
SEP = re.compile(r'(?:^|[|;&(`]|\$\(|\bthen\b|\bdo\b|\belse\b|\bexec\b|\bsetsid\b|\bsudo\b|\bxargs\b|\bnice -n \d+\b|\btimeout \S+)\s*([A-Za-z][A-Za-z0-9_.+-]*)(?![\w.+=-])')   # NAME= assigns
WHICH = re.compile(r'(?:command -v|\bwhich|shutil\.which\()\s*[\'"]?([A-Za-z][A-Za-z0-9_.+-]*)')


def shell_files():
    out = [ROOT / 'hw.sh', ROOT / 'fetch_bags.sh']   # the setup scripts are the installers, not callers
    out += sorted((ROOT / 'net').glob('*.sh')) + sorted((ROOT / 'devices').glob('**/*.sh'))
    for d in sorted((ROOT / 'src').glob('*/tools')):   # every repo's tools/, whatever the team calls its repos
        out += [p for p in sorted(d.iterdir()) if p.is_file()]
    for name in ('start', 'ev'):   # an agent-loop repo's entry points, in whichever repo carries them
        out += sorted(p for p in (ROOT / 'src').glob('*/' + name) if p.is_file())
    return out


def is_shell(path):
    head = path.read_text(errors='replace').split('\n', 1)[0]
    return path.suffix == '.sh' or ('sh' in head and head.startswith('#!') and 'python' not in head)


def commands(path):
    """Commands a shell script runs: command-position words, and `command -v X` probes. Skips comments,
    quoted text, heredoc bodies and the script's own functions."""
    text = path.read_text(errors='replace')
    funcs = set(re.findall(r'^\s*(?:function\s+)?([A-Za-z_][\w.-]*)\s*\(\)', text, re.M))
    found, heredoc = set(), None
    for line in text.splitlines():
        if heredoc:
            if line.strip() == heredoc:
                heredoc = None
            continue
        m = re.search(r'<<-?\s*[\'"]?(\w+)[\'"]?', line)
        if m:
            heredoc = m.group(1)
        line = re.sub(r'#.*$', '', line) if not re.search(r'[\'"].*#', line) else line
        found.update(WHICH.findall(line))
        bare = re.sub(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'', '""', line)
        if bare.strip().startswith('#'):
            continue
        # a command-position word counts when it is an installed program (a function, variable or prose is not)
        found.update(c for c in SEP.findall(bare) if shutil.which(c))
    return {c for c in found if c not in KEYWORDS | BUILTINS | funcs and not c.isupper() and not c.startswith('_')}


def python_commands(path):
    text = path.read_text(errors='replace')
    return set(re.findall(r"subprocess\.\w+\(\s*\[\s*['\"]([A-Za-z][\w.+-]*)['\"]", text)) | set(WHICH.findall(text))


class HostDepsTest(unittest.TestCase):
    def test_every_command_is_listed(self):
        declared = set()    # the names the repos' deps.yaml files declare (astro_dock's, src/*/deps.yaml)
        for f in [ROOT / 'deps.yaml', *sorted((ROOT / 'src').glob('*/deps.yaml'))]:
            declared |= set(yaml.safe_load(f.read_text())['deps'])
        self.assertLessEqual(declared, set(CONF['deps']), 'deps.yaml names missing from config/host-deps.yaml')
        known = {c for n in declared for c in CONF['deps'][n]['cmds']}
        known |= {c for names in CONF['allow'].values() for c in names}
        missing = {}
        for path in shell_files():
            if is_shell(path):
                found = commands(path)
            else:
                found = python_commands(path)
            for c in found - known:
                # a script the workspace ships (tools, devices/) or a file beside it is not a package
                if (path.parent / c).exists() or list(ROOT.glob(f'**/{c}')) and c.endswith(('.sh', '.py')):
                    continue
                missing.setdefault(c, set()).add(path.name)
        self.assertFalse(missing, 'commands not in config/host-deps.yaml (add the package, or the allowlist): '
                         + '; '.join(f'{c} ({", ".join(sorted(f))})' for c, f in sorted(missing.items())))

    def test_every_manager_has_every_dep(self):
        for name, dep in CONF['deps'].items():
            for mgr in ('pacman', 'apt', 'dnf', 'brew'):
                self.assertIn(mgr, dep, f'{name} has no {mgr} entry')

    def test_setup_scripts_read_the_declarations(self):
        for script in ('setup.linux.sh', 'setup.mac.sh'):
            self.assertIn('devices/deps.sh', (ROOT / script).read_text())

    def test_deps_script_maps_names(self):
        out = subprocess.run([str(ROOT / 'devices/deps.sh'), 'packages', 'apt'], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('rsync', out.stdout.split())
        self.assertEqual(out.stderr, '')


# sudo runs cp and sed (on the fixture nsswitch); every other command, the package managers included, is logged only.
SUDO = '#!/bin/sh\necho "sudo $*" >> "$LOG"\ncase $1 in cp|sed) exec "$@" ;; esac\n'
NSSWITCH = ('passwd: files systemd\n'
            'hosts: mymachines resolve [!UNAVAIL=return] files myhostname dns\n'
            'networks: files\n')


class MdnsTest(unittest.TestCase):
    """setup.linux.sh --components mdns (ensure_mdns, also run by the devtools and boat stages)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        (self.tmp / 'bin').mkdir()
        for name, text in {'sudo': SUDO, 'systemctl': '#!/bin/sh\necho "systemctl $*" >> "$LOG"\n'}.items():
            (self.tmp / 'bin' / name).write_text(text)
            (self.tmp / 'bin' / name).chmod(0o755)
        self.nss = self.tmp / 'nsswitch.conf'
        self.nss.write_text(NSSWITCH)

    def mdns(self):
        log = self.tmp / 'log'
        log.write_text('')
        run = subprocess.run(['bash', str(ROOT / 'setup.linux.sh'), '--components', 'mdns', '--yes', '--non-interactive'],
                             capture_output=True, text=True, timeout=60,
                             env={**os.environ, 'PATH': f'{self.tmp / "bin"}:{os.environ["PATH"]}',
                                  'LOG': str(log), 'HW_NSSWITCH': str(self.nss)})
        if 'Skipping package mutation on immutable host' in run.stdout:
            self.skipTest('immutable host')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('Completed: mdns', run.stdout)
        return log.read_text().splitlines()

    def test_adds_mdns_once_and_keeps_the_other_hosts_entries(self):
        calls = self.mdns()
        self.assertEqual(self.nss.read_text(), NSSWITCH.replace(
            'files myhostname', 'files mdns4_minimal [NOTFOUND=return] myhostname'))
        self.assertEqual(Path(f'{self.nss}.astro-bak').read_text(), NSSWITCH)
        self.assertIn('sudo systemctl enable --now avahi-daemon', calls)
        installs = [c for c in calls if re.match(r'sudo (pacman|apt-get install|dnf)', c)]
        for c in installs:            # only when Avahi or nss-mdns is missing here; one helper per manager
            self.assertRegex(c, r'^sudo (pacman -S --needed --noconfirm avahi nss-mdns'
                                r'|apt-get install -y avahi-daemon libnss-mdns|dnf -y install avahi nss-mdns)$')
        once = self.nss.read_text()
        calls = self.mdns()                                            # idempotent: nothing more changes
        self.assertEqual(self.nss.read_text(), once)
        self.assertEqual(Path(f'{self.nss}.astro-bak').read_text(), NSSWITCH)
        self.assertFalse([c for c in calls if c.startswith(('sudo sed', 'sudo cp'))])

    def test_net_has_no_other_mdns_setup(self):
        self.assertFalse((ROOT / 'net/mdns.sh').exists())
        for path in sorted((ROOT / 'net').glob('*.sh')):
            self.assertNotIn('nsswitch', path.read_text(), path.name)


if __name__ == '__main__':
    unittest.main()
