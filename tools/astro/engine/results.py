"""Result files (<data>/<date>/results/<test>-<HHMMSS>.yaml) and each test's status:

  pending   no result yet
  aborted   the latest run was stopped
  stale     an upstream key (the test's `upstream:`) changed since the run
  measured  a result waits for `astro apply` (or the test applies nothing)
  applied   the latest result was applied
"""
import datetime
import hashlib
import json
from pathlib import Path

import yaml

from .apply import current


def state_hash(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()[:6] if path.exists() else 'none'


def snapshot(state, defaults, keys):
    return {key: current(state or {}, defaults, key) for key in keys or ()}


def save(day_dir, result, now=None):
    now = now or datetime.datetime.now()
    directory = Path(day_dir) / 'results'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{result["test"]}-{now:%H%M%S}.yaml'
    result = dict(result, date=result.get('date') or now.date().isoformat(), time=result.get('time') or f'{now:%H:%M:%S}')
    if 'holds' in result:                 # raw samples last, so the file reads top-down
        result['holds'] = result.pop('holds')
    path.write_text(yaml.safe_dump(json.loads(json.dumps(result)), sort_keys=False, width=110))
    return path


def load_all(data_root):
    """Every result under the data root, oldest first (by date dir, then file name)."""
    out = []
    for path in sorted(Path(data_root).glob('*/results/*.yaml'), key=lambda p: (p.parent.parent.name, p.name)):
        try:
            data = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict) and data.get('test'):
            out.append(dict(data, path=str(path)))
    return out


def latest(data_root, test):
    found = [r for r in load_all(data_root) if r['test'] == test]
    return found[-1] if found else None


def mark_applied(path, backup, when=None):
    path = Path(path)
    data = yaml.safe_load(path.read_text())
    data['applied'] = f'{when or datetime.datetime.now():%Y-%m-%d %H:%M:%S}'
    data['backup'] = str(backup)
    path.write_text(yaml.safe_dump(data, sort_keys=False, width=110))


def unmark_applied(data_root, backup):
    """An undo: the result applied with that backup is measured again."""
    for result in load_all(data_root):
        if result.get('backup') == str(backup):
            path = Path(result.pop('path'))
            result.pop('applied', None)
            result.pop('backup', None)
            path.write_text(yaml.safe_dump(result, sort_keys=False, width=110))


def classify(result, state, defaults):
    if result is None:
        return 'pending'
    if result.get('status') == 'aborted':
        return 'aborted'
    if any(current(state or {}, defaults, key) != value for key, value in (result.get('upstream') or {}).items()):
        return 'stale'
    if result.get('changes') and not result.get('applied'):
        return 'measured'
    return 'applied' if result.get('changes') else 'measured'


def statuses(tests, data_root, state, defaults):
    """[(test, status, latest result)] in the given order."""
    every = load_all(data_root)
    out = []
    for test in tests:
        mine = [r for r in every if r['test'] == test]
        result = mine[-1] if mine else None
        out.append((test, classify(result, state, defaults), result))
    return out


def next_step(rows):
    """The next command: apply a waiting result, else run the first test not done."""
    for test, status, result in rows:
        if status == 'measured' and result and result.get('changes'):
            return f'astro apply {test}'
        if status in ('pending', 'aborted', 'stale'):
            return f'astro test {test}'
    return 'all tests done'
