"""install-check.py [WS]: warn when the workspace's install/ holds copies of Python sources that src/ has
since changed (a build without --symlink-install: the code that runs is not the code checked out).
colcon_defaults.yaml makes every build in the workspace a symlink-install; this finds an older one.
Exit 0 when current (or nothing to check), 1 with the stale packages on stderr.
"""
import filecmp
import os
from pathlib import Path
import sys

SKIP = {'build', 'install', 'log', '.git', '__pycache__'}


def sources(src):
    """{module name: source dir} of the Python packages beside each setup.py under src."""
    out = {}
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith('.')]
        if 'COLCON_IGNORE' in files:
            dirs[:] = []
        elif 'setup.py' in files:
            for d in dirs:
                if (Path(root, d, '__init__.py')).is_file():
                    out.setdefault(d, Path(root, d))
    return out


def site_dirs(install):
    for pattern in ('lib/python3*/site-packages', 'local/lib/python3*/dist-packages',
                    '*/lib/python3*/site-packages', '*/local/lib/python3*/dist-packages'):
        yield from install.glob(pattern)


def stale(ws):
    """[(package, files that differ)] for copied modules out of step with src/."""
    install, out = ws / 'install', []
    found = sources(ws / 'src') if install.is_dir() else {}
    for site in (s for s in site_dirs(install) if found):
        for module in sorted(site.iterdir()):
            source = found.get(module.name)
            if source is None or module.is_symlink() or not module.is_dir():
                continue
            copies = {p.relative_to(module) for p in module.rglob('*.py') if not p.is_symlink()}
            if not copies:
                continue
            originals = {p.relative_to(source) for p in source.rglob('*.py') if '__pycache__' not in p.parts}
            differ = [r for r in originals | copies
                      if r not in copies or r not in originals or not filecmp.cmp(module / r, source / r, shallow=False)]
            if differ:
                out.append((module.name, len(differ)))
    return out


def main(argv):
    ws = Path(argv[1] if len(argv) > 1 else os.environ.get('ROS_WS', '.')).resolve()
    found = stale(ws)
    if not found:
        return 0
    names = ', '.join(f'{name} ({count} files)' for name, count in found)
    print(f'WARNING: {ws}/install is stale against src/: {names} differ, so the code that runs is an old copy '
          f'(a build without --symlink-install). In the dev container: rm -rf build install && colcon build '
          f'(colcon_defaults.yaml symlinks)', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
