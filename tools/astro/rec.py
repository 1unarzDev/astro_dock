"""`astro rec start [preset] [label] | stop | ls`: ros2 bag record (mcap, 300 s splits) into
<data>/<date>/bags/<HHMMSS>[-label]/, its pid in <data>/<date>/.rec.pid; ls shows each bag's span and the
log lines inside it. `rec fetch` (finding and copying bags between the computers) is fetch.py."""
import datetime
import os
from pathlib import Path
import signal
import subprocess
import time


from . import config
from .engine import log


def presets(vessel):
    data = config.load_yaml(vessel.path('bags')) or {}
    data = data.get('presets', data) if isinstance(data, dict) else {}
    if not isinstance(data, dict) or not data:
        raise config.Error(f'{vessel.path("bags")}: expected presets, NAME: [topics] or NAME: all')
    return data


def pid_file(root):
    return log.day_dir(root) / '.rec.pid'


def running(root):
    path = pid_file(root)
    if not path.exists():
        return None
    pid, _, directory = path.read_text().partition(' ')
    return (int(pid), directory.strip()) if log.pid_alive(pid) else None


def start(vessel, words):
    root = vessel.data_root()
    if running(root):
        raise config.Error(f'already recording {running(root)[1]} (astro rec stop)')
    table = presets(vessel)
    preset = words[0] if words and words[0] in table else next(iter(table))
    label = '-'.join(words[1:] if words and words[0] in table else words)
    topics = table[preset]
    now = datetime.datetime.now()
    name = f'{now:%H%M%S}' + (f'-{label}' if label else '')
    bags = log.day_dir(root) / 'bags'
    bags.mkdir(parents=True, exist_ok=True)
    out = bags / name
    command = ['ros2', 'bag', 'record', '-s', 'mcap', '-d', '300', '-o', str(out)]
    if topics == 'all':
        command.append('--all-topics')
        exclude = (config.load_yaml(vessel.path('bags')) or {}).get('exclude')
        if exclude:
            command += ['--exclude-regex', str(exclude)]
    else:
        topics = list(topics)
        events = vessel.astro.get('events')
        if events and events not in topics:
            topics.append(events)
        command += topics
    with open(bags / f'{name}.log', 'w') as output:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    time.sleep(1.0)
    if process.poll() is not None:
        raise config.Error(f'ros2 bag record exited: see {bags / (name + ".log")}')
    pid_file(root).write_text(f'{process.pid} {out}')
    # The name was fixed before the recorder started (-o), so the line names the bag; it waits until the
    # recorder has subscribed, so the line is in the bag too.
    log.write(root, 'rec', f'start {out.relative_to(log.day_dir(root))} ({preset})', vessel.astro.get('events'),
              wait=True)
    return 0


def stop(vessel):
    root = vessel.data_root()
    found = running(root)
    if not found:
        print('not recording')
        pid_file(root).unlink(missing_ok=True)
        return 0
    pid, directory = found
    log.write(root, 'rec', f'stop  bags/{Path(directory).name}', vessel.astro.get('events'), wait=True)
    os.killpg(pid, signal.SIGINT)
    for _ in range(100):
        if not log.pid_alive(pid):
            break
        time.sleep(0.1)
    else:
        os.killpg(pid, signal.SIGTERM)
    pid_file(root).unlink(missing_ok=True)
    return 0


def span(directory):
    """(start, end) epoch seconds from a bag's metadata.yaml, or None while it records."""
    meta = config.load_yaml(Path(directory) / 'metadata.yaml')
    info = (meta or {}).get('rosbag2_bagfile_information') if isinstance(meta, dict) else None
    if not info:
        return None
    start = info['starting_time']['nanoseconds_since_epoch'] / 1e9
    return start, start + info['duration']['nanoseconds'] / 1e9


def ls(vessel, day=None):
    """Each bag with the log lines inside it, compared on epoch seconds (the log's clock is its .tz)."""
    root = vessel.data_root()
    day = day or datetime.date.today()
    lines = [line for line in map(log.parse_line, log.read(root, day)) if line]
    stamped = [(log.epoch(root, day, stamp), stamp, kind, message) for stamp, kind, message in lines]
    bags = sorted(p for p in (log.day_dir(root, day) / 'bags').glob('*') if p.is_dir())
    if not bags:
        print(f'no bags in {log.day_dir(root, day) / "bags"}')
    for bag in bags:
        found = span(bag)
        if found:
            low, high = found
            head = (f'{datetime.datetime.fromtimestamp(low):%H:%M:%S}-'
                    f'{datetime.datetime.fromtimestamp(high):%H:%M:%S}')
        else:
            starts = [e for e, _, kind, message in stamped if kind == 'rec' and bag.name in message]
            low, high = (starts[-1] if starts else 0.0), time.time()
            head = f'{datetime.datetime.fromtimestamp(low):%H:%M:%S}-(recording)' if starts else '(recording)'
        print(f'{head}  {bag.relative_to(root)}')
        for when, stamp, kind, message in stamped:
            if low - 1 <= when <= high + 1:
                print(f'    {stamp}  {kind:<{log.WIDTH}} {message}')
    return 0
