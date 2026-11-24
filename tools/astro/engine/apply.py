"""Write a result's changes into the state file: a timestamped backup first, then the profile's check, rolled
back when the check refuses. A change is a YAML key (dotted path, value merged or replaced) or one column of a
flat row-major n x n matrix.

Each apply appends one line to the history next to the state file (STATE.history.jsonl): its time, test, the
backup's name (its id) and each key's old and new value. `revert` puts one apply's keys back to their old
values, leaving every other key as it is, and appends an undo line naming that apply."""
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
    out, items = laid(state, defaults, changes, axes)
    return out, [(i['label'], i['old'], i['new']) for i in items]


def laid(state, defaults, changes, axes=None):
    """(new state, [item]): each item the change's key (column, fallback), label, old and new value, and
    whether the state held the key before (absent: the defaults or the fallback were in force)."""
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
            absent = get(out, key) is None
            put(out, key, matrix)
            diffs.append(dict(where(change), label=label(change, axes), old=old,
                              new=[float(v) for v in change['value']], absent=absent))
        else:
            old, absent = current(out, defaults, key), get(out, key) is None
            put(out, key, copy.deepcopy(change['value']))
            diffs.append(dict(where(change), label=label(change, axes), old=old, new=change['value'], absent=absent))
    return out, diffs


def where(change):
    return {k: change[k] for k in ('key', 'column', 'fallback') if change.get(k) is not None}


def drop(data, key):
    """Remove KEY (dotted) and the parents it leaves empty."""
    parts = key.split('.')
    nodes = [data]
    for part in parts[:-1]:
        node = nodes[-1].get(part)
        if not isinstance(node, dict):
            return
        nodes.append(node)
    nodes[-1].pop(parts[-1], None)
    for depth in range(len(parts) - 1, 0, -1):
        if not nodes[depth]:
            nodes[depth - 1].pop(parts[depth - 1], None)


def stamp(now=None):
    return f'{now or datetime.datetime.now():%Y%m%d-%H%M%S-%f}'


def new_backup(path, text, now=None):
    """A fresh backup file holding ``text``; never overwrites one (two applies in one microsecond, or a
    clock step back, get a numbered suffix)."""
    base = path.with_name(f'{path.name}.bak-{stamp(now)}')
    for n in range(100):
        backup = base if n == 0 else base.with_name(f'{base.name}.{n:02d}')
        try:
            with open(backup, 'x') as handle:
                handle.write(text)
            return backup
        except FileExistsError:
            continue
    raise Refused(f'no free backup name next to {path}')


def backups(path):
    path = Path(path)
    return sorted(path.parent.glob(path.name + '.bak-*'), key=lambda p: (p.stat().st_mtime_ns, p.name))


def write_text(path, text):
    """Atomic: a sibling temp file renamed over ``path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text)
    tmp.replace(path)


def write_state(path, data):
    write_text(path, '# Written by astro apply: measured and tuned state. Undo: astro undo.\n'
               + yaml.safe_dump(data, sort_keys=False, default_flow_style=None, width=110))


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


def apply(path, changes, defaults=None, check=None, axes=None, live=(), now=None, test=None, result=None):
    """Back up, write, check (rolled back on refusal), then record it in the history. Returns (backup path,
    changed diffs); (None, []) when nothing would change (no write, no backup)."""
    path = Path(path)
    state = yaml.safe_load(path.read_text()) if path.exists() else {}
    new, items = laid(state or {}, defaults, changes, axes)
    items = [i for i in items if i['old'] != i['new']]
    if not items:
        return None, []
    backup = commit(path, new, check, now)
    record(path, dict(kind='apply', id=backup.name, time=f'{now or datetime.datetime.now():%Y-%m-%d %H:%M:%S}',
                      test=test or '', result=str(result or ''), backup=backup.name, live=list(live), keys=items))
    return backup, [(i['label'], i['old'], i['new']) for i in items]


def commit(path, new, check, now=None):
    """Back up the state file, write NEW atomically and run the check; the backup put back on refusal."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = new_backup(path, path.read_text() if path.exists() else ABSENT, now)
    write_state(path, new)
    ok, output = run_check(check, path)
    if not ok:
        restore(path, backup)
        raise Refused(f'check refused the new state, restored {path.name}:\n{output}')
    return backup


def restore(path, backup):
    path, backup = Path(path), Path(backup)
    text = backup.read_text()
    if text == ABSENT:
        path.unlink(missing_ok=True)
    else:
        write_text(path, text)
    backup.unlink()


def history_path(path):
    return Path(str(path) + '.history.jsonl')


def record(path, entry):
    with open(history_path(path), 'a') as handle:
        handle.write(json.dumps(entry, default=str) + '\n')


def history(path):
    """Every apply in the history, newest first, each with `undone` (the undo's time) when reverted."""
    found = history_path(path)
    entries = []
    for line in found.read_text().splitlines() if found.exists() else ():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    undone = {e['of']: e['time'] for e in entries if e.get('kind') == 'undo'}
    return [dict(e, undone=undone.get(e['id'])) for e in reversed(entries) if e.get('kind') == 'apply']


def in_force(state, defaults, item):
    value = current(state or {}, defaults, item['key'], item.get('fallback'))
    if 'column' in item and isinstance(value, list):
        return column_of(value, item['column'])
    return value


def plan(path, entry, defaults=None):
    """[(item, value now, later applies that changed it since)] for each key of ENTRY. A key is in conflict
    when its value now is not the one ENTRY wrote."""
    path = Path(path)
    state = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    newer = []
    for other in history(path):
        if other['id'] == entry['id']:
            break
        if not other.get('undone'):
            newer.append(other)
    out = []
    for item in entry['keys']:
        later = [o for o in reversed(newer) if any(k['label'] == item['label'] for k in o['keys'])]
        out.append((item, in_force(state, defaults, item), later))
    return out


def revert(path, entry, defaults=None, check=None, skip=(), now=None):
    """Put ENTRY's keys (but those labelled in SKIP) back to their old values, every other key untouched:
    backup, atomic write, check (rolled back on refusal), then an undo line in the history. A key the state
    did not hold before the apply is removed again when that gives the same value. Returns [(label, value
    now, restored value)] of the keys that changed."""
    path = Path(path)
    state = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    items = [i for i in entry['keys'] if i['label'] not in skip]
    new = copy.deepcopy(state)
    for item in items:
        change = dict(where(item), value=item['old'])
        if item['old'] is None and 'column' not in item:
            drop(new, item['key'])
            continue
        new, _ = laid(new, defaults, [change])
        if item.get('absent'):
            without = copy.deepcopy(new)
            drop(without, item['key'])
            if current(without, defaults, item['key'], item.get('fallback')) == get(new, item['key']):
                new = without
    done = [(i['label'], in_force(state, defaults, i), in_force(new, defaults, i)) for i in items]
    done = [d for d in done if d[1] != d[2]]
    if new == state:
        return []
    backup = commit(path, new, check, now)
    record(path, dict(kind='undo', of=entry['id'], time=f'{now or datetime.datetime.now():%Y-%m-%d %H:%M:%S}',
                      test=entry.get('test', ''), backup=backup.name, skipped=sorted(skip),
                      keys=[dict(label=label, old=old, new=value) for label, old, value in done]))
    return done


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
