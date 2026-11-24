"""`astro docs`: where the information lives, in plain text. It reads files only (and `git remote` of each repo):
the field repo (the profile's astro.field: its clone, today's session, checklists, handoff/, plans/, issues),
today's log and bags, and the README.md and docs/*.md of the workspace and of each repo under src/."""
from pathlib import Path
import re
import subprocess

from . import config
from .engine import log


def github(repo):
    """https://github.com/OWNER/NAME/blob/BRANCH of a repo whose origin is on GitHub, else None."""
    try:
        url = subprocess.run(['git', '-C', str(repo), 'remote', 'get-url', 'origin'], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        branch = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--abbrev-ref', 'HEAD'], capture_output=True,
                                text=True, timeout=5).stdout.strip() or 'main'
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = re.match(r'(?:https://github\.com/|git@github\.com:)([^/]+/[^/]+?)(?:\.git)?/?$', url)
    return f'https://github.com/{found.group(1)}/blob/{branch}' if found else None


def repo_docs(repo, skip=()):
    """README.md and docs/*.md of one repo (and of its packages one level down), relative paths; not in
    hidden folders, nested repos (listed on their own) or the ``skip`` folders (data)."""
    repo = Path(repo)
    found = [p for p in [repo / 'README.md', *sorted(repo.glob('docs/*.md')), *sorted(repo.glob('*/README.md')),
                         *sorted(repo.glob('*/docs/*.md'))] if p.is_file()]
    out = []
    for p in found:
        rel = p.relative_to(repo)
        top = repo / rel.parts[0]
        if any(part.startswith('.') for part in rel.parts) or (len(rel.parts) > 1 and (
                (top / '.git').exists() or any(top == s or top in s.parents for s in skip))):
            continue
        out.append(rel)
    return out


def lines(vessel, ws=config.WS, day=None):
    out = [f'vessel {vessel.name}  state {vessel.state()}']
    spec = vessel.astro.get('field') or {}
    if spec.get('repo'):
        from .field import Field
        f = Field(vessel)
        session = f.session(day)
        out += ['', f'field repo  {f.url}', f'  clone     {f.dir}' + ('' if f.dir.exists() else '  (not cloned: astro sync)'),
                f'  session   {session}' + ('' if session.exists() else '  (not created yet)')]
        out += [f'            {p.name}' for p in sorted(session.glob('*')) if p.name in ('plan.md', 'checks.yaml')
                or p.name.startswith('summary')]
        lists = sorted((f.dir / 'checklists').glob('*.md'))
        out.append('  checklists ' + (', '.join(p.stem for p in lists) + '  (astro check NAME)' if lists else 'none'))
        for folder in ('handoff', 'plans'):
            found = sorted((f.dir / folder).glob('*.md'))
            out.append(f'  {folder + "/":<10} ' + ('none' if not found else f'{len(found)} file(s)'))
            out += [f'            {p.relative_to(f.dir)}' for p in found]
        out.append(f'  issues    https://github.com/{f.repo}/issues  (astro show: the open ones)')
    else:
        out += ['', 'field repo  none (no astro.field.repo in the vessel profile)']
    root = vessel.data_root()
    out += ['', f'day log     {log.day_dir(root, day) / "log.txt"}  (astro log)',
            f'bags        {log.day_dir(root, day) / "bags"}  (astro rec ls)', '', 'docs']
    ws = Path(ws)
    repos = [ws] + sorted(p for p in [*ws.glob('*'), *ws.glob('src/*')] if (p / '.git').exists())
    skip = [Path(root).resolve()]
    for repo in repos:
        found = repo_docs(repo, skip)
        if not found:
            continue
        url = github(repo)
        out.append(f'  {repo.name}' + (f'  {url}' if url else f'  {repo}'))
        out += [f'    {p}' for p in found]
    return out


def main(vessel):
    print('\n'.join(lines(vessel)))
    return 0
