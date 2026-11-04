"""Every external command the station-side tools call is a host dependency in config/host-deps.yaml
(installed by setup.linux.sh and setup.mac.sh) or on its allowlist (shell builtins, coreutils, other stages)."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONF = yaml.safe_load((ROOT / 'config/host-deps.yaml').read_text())
KEYWORDS = set('if then else elif fi for while until do done case esac in function select time coproc { } ! [[ ]] [ ]'.split())
BUILTINS = set('cd echo printf read exit return export local declare readonly unset set shift source . : eval exec trap '
               'wait break continue getopts let type command builtin alias umask ulimit pushd popd test true false '
               'mapfile readarray hash jobs fg bg disown'.split())
SEP = re.compile(r'(?:^|[|;&(`]|\$\(|\bthen\b|\bdo\b|\belse\b|\bexec\b|\bsetsid\b|\bsudo\b|\bxargs\b|\bnice -n \d+\b|\btimeout \S+)\s*([A-Za-z][A-Za-z0-9_.+-]*)')
WHICH = re.compile(r'(?:command -v|\bwhich|shutil\.which\()\s*[\'"]?([A-Za-z][A-Za-z0-9_.+-]*)')


def shell_files():
    out = [ROOT / 'hw.sh', ROOT / 'fetch_bags.sh']   # the setup scripts are the installers, not callers
    out += sorted((ROOT / 'net').glob('*.sh')) + sorted((ROOT / 'devices').glob('**/*.sh'))
    for sub in ('mhseals_testing/tools', 'mhseals_agents/tools'):
        d = ROOT / 'src' / sub
        out += [p for p in sorted(d.iterdir()) if p.is_file()] if d.is_dir() else []
    for p in ('mhseals_agents/start', 'mhseals_agents/ev'):
        if (ROOT / 'src' / p).is_file():
            out.append(ROOT / 'src' / p)
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


if __name__ == '__main__':
    unittest.main()
