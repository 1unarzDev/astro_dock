"""Write a result's changes into the state file: a timestamped backup first, then the profile's check, rolled
back when the check refuses; `undo` restores the latest backup. A change is a YAML key (dotted path, value
merged or replaced) or one column of a flat row-major n x n matrix."""
import copy
import datetime
import json
from pathlib import Path
import shlex
import subprocess

import yaml

ABSENT = '# astro: the state file did not exist before this apply\n'


class Refused(Exception):
    pass


def get(data, key, default=None):
    node = data
    for part in key.split('.'):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def put(data, key, value):
    parts = key.split('.')
    node = data
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


def current(state, defaults, key, fallback=None):
    """The value in force: the state's, else the defaults', else (null) the fallback key's."""
    value = get(state, key)
    if value is None:
        value = get(defaults or {}, key)
    if value is None and fallback:
        value = current(state, defaults, fallback)
    return copy.deepcopy(value)


def column_of(matrix, column):
    n = round(len(matrix) ** 0.5)
    return [matrix[n * row + column] for row in range(n)]


def label(change, axes=None):
    if 'column' in change:
        name = axes[change['column']] if axes else change['column']
        return f'{change["key"]}[:,{name}]'
    return change['key']


def changed(state, defaults, changes, axes=None):
    """(new state, [(label, old, new)]): the changes laid over a copy of ``state``."""
    out, diffs = copy.deepcopy(state or {}), []
    for change in changes:
        key = change['key']
        if 'column' in change:
            matrix = current(out, defaults, key)
            if matrix is None and change.get('fallback'):     # the fallback as it was before these changes
                matrix = current(state or {}, defaults, change['fallback'])
            if not isinstance(matrix, list):
                raise Refused(f'{key}: no matrix in the state or its defaults')
            n = round(len(matrix) ** 0.5)
            old = column_of(matrix, change['column'])
            for row, value in enumerate(change['value']):
                matrix[n * row + change['column']] = float(value)
            put(out, key, matrix)
            diffs.append((label(change, axes), old, [float(v) for v in change['value']]))
        else:
            old = current(out, defaults, key)
            put(out, key, copy.deepcopy(change['value']))
            diffs.append((label(change, axes), old, change['value']))
    return out, diffs


def stamp(now=None):
    return f'{now or datetime.datetime.now():%Y%m%d-%H%M%S}'


def backups(path):
    path = Path(path)
    return sorted(path.parent.glob(path.name + '.bak-*'))


def write_state(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text('# Written by astro apply: measured and tuned state. Undo: astro undo.\n'
                   + yaml.safe_dump(data, sort_keys=False, default_flow_style=None, width=110))
    tmp.replace(path)


def run_check(check, path):
    """(ok, output) of the check command with the state path appended; no check passes."""
    if not check:
        return True, ''
    words = (shlex.split(check) if isinstance(check, str) else list(check)) + [str(path)]
    try:
        run = subprocess.run(words, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f'{words[0]}: {error}'
    return run.returncode == 0, (run.stdout + run.stderr).strip()


def apply(path, changes, defaults=None, check=None, axes=None, live=(), now=None):
    """Back up, write, check (rolled back on refusal). Returns (backup path, changed diffs); (None, [])
    when nothing would change (no write, no backup)."""
    path = Path(path)
    state = yaml.safe_load(path.read_text()) if path.exists() else {}
    new, diffs = changed(state or {}, defaults, changes, axes)
    diffs = [d for d in diffs if d[1] != d[2]]
    if not diffs:
        return None, []
    backup = path.with_name(f'{path.name}.bak-{stamp(now)}')
    backup.write_text(path.read_text() if path.exists() else ABSENT)
    Path(str(backup) + '.json').write_text(json.dumps({'live': list(live)}))
    write_state(path, new)
    ok, output = run_check(check, path)
    if not ok:
        restore(path, backup)
        raise Refused(f'check refused the new state, restored {path.name}:\n{output}')
    return backup, diffs


def restore(path, backup):
    path, backup = Path(path), Path(backup)
    text = backup.read_text()
    if text == ABSENT:
        path.unlink(missing_ok=True)
    else:
        path.write_text(text)
    backup.unlink()
    side = Path(str(backup) + '.json')
    live = json.loads(side.read_text()).get('live', []) if side.exists() else []
    side.unlink(missing_ok=True)
    return live


def undo(path, check=None):
    """Restore the newest backup (and remove it). Returns (backup name, live params to re-set)."""
    found = backups(path)
    found = [b for b in found if not b.name.endswith('.json')]
    if not found:
        raise Refused(f'no backup of {path}')
    newest = found[-1]
    live = restore(path, newest)
    ok, output = run_check(check, path) if Path(path).exists() else (True, '')
    if not ok:
        print(f'warning: the restored state fails the check:\n{output}')
    return newest.name, live


def param_value(value):
    """A YAML value as `ros2 param set` takes it."""
    if isinstance(value, list):
        return '[' + ', '.join(repr(float(v)) if isinstance(v, (int, float)) and not isinstance(v, bool)
                               else json.dumps(v) for v in value) + ']'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def set_live(live, state_path, defaults=None, timeout=10):
    """`ros2 param set NODE PARAM VALUE` for each {node, param, key}; messages, never raises."""
    state = yaml.safe_load(Path(state_path).read_text()) if Path(state_path).exists() else {}
    out = []
    for item in live:
        value = current(state or {}, defaults, item.get('key') or item['param'], item.get('fallback'))
        if value is None:
            continue
        words = ['ros2', 'param', 'set', item['node'], item['param'], param_value(value)]
        try:
            run = subprocess.run(words, capture_output=True, text=True, timeout=timeout)
            ok = run.returncode == 0 and 'Set parameter successful' in run.stdout
            out.append(('live ' if ok else 'NOT live ') + f'{item["node"]} {item["param"]}'
                       + ('' if ok else f': {(run.stdout + run.stderr).strip()[:200]} (applies at the next launch)'))
        except (OSError, subprocess.TimeoutExpired) as error:
            out.append(f'NOT live {item["node"]} {item["param"]}: {error} (applies at the next launch)')
    return out
