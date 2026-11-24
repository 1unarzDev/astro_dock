"""The bag directories under one host's data root: imported here, or sent to a registry host's own shell as
`python3 - CONTAINER DATA_EXPR` (this file on stdin; stdlib only, Python 3.6+), where the data root is
DATA_EXPR as the dev container CONTAINER sees it, mapped through its mounts to the host's path.

Prints one tab-separated line per finding:
  astro-root  ROOT  FREE_BYTES
  astro-bag   REL  BYTES  START  END  MTIME  TOPICS  OPEN   (START/END epoch s from metadata.yaml or -;
                                                           OPEN why it is still being written, or -)
  astro-error MESSAGE
A bag is a directory holding metadata.yaml or .mcap/.db3 files (any layout: DATE/bags/NAME, sessions/...);
it is open while it has no metadata.yaml or its day's .rec.pid names it and that recorder still runs."""
import json
import os
import re
import subprocess
import sys

DEPTH = 5
DAY_BAG = re.compile(r'^(\d{4}-\d\d-\d\d)/bags/([^/]+)$')


def is_bag(files):
    return 'metadata.yaml' in files or any(f.endswith(('.mcap', '.db3')) for f in files)


def walk(root):
    """[(rel, files)] of the bag directories under ROOT, never descending into a bag or hidden dirs."""
    out = []
    for base, dirs, files in os.walk(root):
        rel = os.path.relpath(base, root)
        if rel != '.' and is_bag(files):
            out.append((rel.replace(os.sep, '/'), files))
            dirs[:] = []
            continue
        depth = 0 if rel == '.' else rel.count(os.sep) + 1
        dirs[:] = [] if depth >= DEPTH else sorted(d for d in dirs if not d.startswith('.'))
    return out


def meta(path):
    """(start, end, topics) from a bag's metadata.yaml; (None, None, 0) without one."""
    try:
        with open(os.path.join(path, 'metadata.yaml')) as handle:
            text = handle.read()
    except OSError:
        return None, None, 0
    t0 = re.search(r'starting_time:\s*nanoseconds_since_epoch:\s*(\d+)', text)
    dt = re.search(r'duration:\s*nanoseconds:\s*(\d+)', text)
    topics = len(re.findall(r'topic_metadata:', text))
    if not (t0 and dt):
        return None, None, topics
    return int(t0.group(1)) / 1e9, (int(t0.group(1)) + int(dt.group(1))) / 1e9, topics


def size(path):
    total = 0
    for base, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total


def recording(root, rel, alive):
    """True when REL is DATE/bags/NAME and DATE/.rec.pid names it with a recorder still running."""
    found = DAY_BAG.match(rel)
    if not found:
        return False
    try:
        with open(os.path.join(root, found.group(1), '.rec.pid')) as handle:
            pid, _, directory = handle.read().partition(' ')
    except OSError:
        return False
    return os.path.basename(directory.strip().rstrip('/')) == found.group(2) and alive(pid.strip())


def local_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def container_alive(container):
    def alive(pid):
        if not pid.isdigit():
            return False
        return subprocess.run(['docker', 'exec', container, 'kill', '-0', pid], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    return alive


def scan(root, alive=local_alive):
    """[{rel, size, start, end, mtime, topics, open}] of the bags under ROOT."""
    out = []
    for rel, files in walk(root):
        path = os.path.join(root, rel)
        start, end, topics = meta(path)
        why = ''
        if recording(root, rel, alive):
            why = 'recording'
        elif 'metadata.yaml' not in files:
            why = 'no metadata.yaml (recording, or never closed)'
        out.append({'rel': rel, 'size': size(path), 'start': start, 'end': end,
                    'mtime': os.path.getmtime(path), 'topics': topics, 'open': why})
    return out


def free(root):
    path = root
    while path and not os.path.exists(path):
        path = os.path.dirname(path.rstrip('/'))
    try:
        stat = os.statvfs(path or '/')
    except OSError:
        return 0
    return stat.f_bavail * stat.f_frsize


def lines(root, alive=local_alive):
    out = ['astro-root\t%s\t%d' % (root, free(root))]
    for bag in scan(root, alive) if os.path.isdir(root) else []:
        number = lambda v: '-' if v is None else '%.3f' % v
        out.append('\t'.join(['astro-bag', bag['rel'], str(bag['size']), number(bag['start']), number(bag['end']),
                              '%.0f' % bag['mtime'], str(bag['topics']), bag['open'] or '-']))
    return out


def host_root(container, expr):
    """The host's path of the data root EXPR as CONTAINER sees it (through the mount that holds it)."""
    # sh, not bash: the container's BASH_ENV (its dev.bashrc) can take many seconds
    done = subprocess.run(['docker', 'exec', container, 'sh', '-c', 'echo ' + expr], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True)
    inside = done.stdout.strip()
    if done.returncode != 0 or not inside:
        raise RuntimeError('no dev container running (start it on that host)')
    done = subprocess.run(['docker', 'inspect', '-f', '{{json .Mounts}}', container], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True)
    mounts = json.loads(done.stdout or '[]') or []
    best = None
    for mount in mounts:
        target = mount.get('Destination', '').rstrip('/')
        if target and (inside == target or inside.startswith(target + '/')):
            if best is None or len(target) > len(best[0]):
                best = (target, mount.get('Source', ''))
    if not best:
        raise RuntimeError('the data root %s is not on a mount of the dev container' % inside)
    return best[1].rstrip('/') + inside[len(best[0]):]


def main(argv):
    container, expr = argv[1], argv[2]
    try:
        root = host_root(container, expr)
    except (RuntimeError, OSError, ValueError) as error:
        print('astro-error\t%s' % error)
        return 1
    print('\n'.join(lines(root, container_alive(container))))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
