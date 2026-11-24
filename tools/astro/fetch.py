"""`astro rec fetch`: find the bags on every computer of the vessel and copy the picked ones to one of them.

Search: each registry ssh host (its own shell finds the dev container's data root on the host, bagscan.py)
and this machine (its data root), in parallel with a short timeout; one line per host that can't be reached.
Copy: rsync -a --partial between the hosts' own shells over the ssh `astro on` uses (resumable), one bag at a
time; a bag that is still being written is skipped, one already at the destination (same path and size) too.

  astro rec fetch                                  a wizard on a terminal; else the list and exit 2
  astro rec fetch --list [--json|--names]          list only (--names: BAG<TAB>about, for pickers)
  astro rec fetch [--from HOST] [--to HOST] BAG... | --new | --date DATE     copy without asking
"""
import argparse
import concurrent.futures
import datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from . import bagscan, config, remote
from .engine import log

TIMEOUT = 25          # seconds one host's search may take
HERE = 'here'


# ---- search -----------------------------------------------------------------------------------------------

def data_expr(vessel):
    """The data root as the dev container's shell sees it (config.Vessel.data_root inside the container)."""
    data = vessel.astro.get('data')
    if not data:
        return '"${ASTRO_DATA:-${ROS_WS:-$PWD}/bags}"'
    data = str(data)
    if data.startswith('~/'):
        return '"$HOME"/' + shlex.quote(data[2:])
    return shlex.quote(data) if data.startswith('/') else '"${ROS_WS:-$PWD}"/' + shlex.quote(data)


def places(vessel, me):
    """[(name, spec or None)]: this machine (None) first, then every other registry ssh host with an address."""
    hosts = vessel.hosts()
    out = [(me or HERE, None)]
    return out + [(n, s) for n, s in hosts.items() if s['ssh'] and s['address'] and n != me]


def scan_argv(vessel, host, spec):
    """ssh argv running bagscan.py (on stdin) in HOST's own shell, against its dev container's data root."""
    find, container = remote.container_find(vessel, spec)
    words, target = remote.ssh_base(vessel, host, spec)
    script = f'{find}python3 - {container} {shlex.quote(data_expr(vessel))}'
    return words + ['-o', 'BatchMode=yes', target, script]


def parse(host, lines):
    """{host, root, free, bags, error} from bagscan lines; each bag a dict (see bag())."""
    out = {'host': host, 'root': '', 'free': 0, 'bags': [], 'error': ''}
    for line in lines:
        fields = line.rstrip('\n').split('\t')
        if fields[0] == 'astro-root' and len(fields) == 3:
            out['root'], out['free'] = fields[1], int(fields[2]) if fields[2].isdigit() else 0
        elif fields[0] == 'astro-bag' and len(fields) == 8:
            out['bags'].append(bag(host, fields[1], int(fields[2]), num(fields[3]), num(fields[4]),
                                   num(fields[5]) or 0.0, int(fields[6]), '' if fields[7] == '-' else fields[7]))
        elif fields[0] == 'astro-error':
            out['error'] = '\t'.join(fields[1:])
    return out


def num(text):
    try:
        return float(text)
    except ValueError:
        return None


def bag(host, rel, size, start, end, mtime, topics, why):
    """One bag: its id (DATE/NAME for astro's own DATE/bags/NAME, else the path under the data root), date,
    label and the rest."""
    found = bagscan.DAY_BAG.match(rel)
    when = start if start is not None else mtime
    if found:
        date, name = found.groups()
        ident, label = f'{date}/{name}', name.partition('-')[2] or name
    else:
        date = datetime.datetime.fromtimestamp(when).strftime('%Y-%m-%d') if when else '-'
        ident, label = rel, short(rel)
    return {'host': host, 'id': ident, 'rel': rel, 'date': date, 'label': label, 'size': size, 'start': start,
            'end': end, 'mtime': mtime, 'topics': topics, 'open': why}


def short(rel):
    """A label for a bag outside DATE/bags/: its path without trailing bag/ and numbered parts, last part."""
    parts = rel.split('/')
    while len(parts) > 1 and (parts[-1] == 'bag' or parts[-1].isdigit()):
        parts.pop()
    return parts[-1]


def scan_here(vessel, me):
    root = vessel.data_root()
    container = '' if remote.in_container() else config.dev_container()
    alive = bagscan.container_alive(container) if container else bagscan.local_alive
    return parse(me or HERE, bagscan.lines(str(root), alive))


def scan_host(vessel, host, spec):
    try:
        done = subprocess.run(scan_argv(vessel, host, spec), input=Path(bagscan.__file__).read_text(),
                              capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return dict(parse(host, []), error=f'no answer in {TIMEOUT} s')
    found = parse(host, done.stdout.splitlines())
    if not found['root'] and not found['error']:
        found['error'] = (done.stderr.strip().splitlines() or [f'ssh exited {done.returncode}'])[-1]
    return found


def search(vessel, me, only=None, say=print):
    """{host: parse()} of this machine and every registry ssh host (ONLY: just those), in parallel; one line
    each for those that can't be searched."""
    wanted = [(n, s) for n, s in places(vessel, me) if not only or n in only]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(wanted), 1)) as pool:
        jobs = {n: pool.submit(scan_here, vessel, me) if s is None else pool.submit(scan_host, vessel, n, s)
                for n, s in wanted}
        found = {n: job.result() for n, job in jobs.items()}
    for name, result in found.items():
        if result['error']:
            say(f'{name}: skipped, {result["error"]}')
    return {n: r for n, r in found.items() if not r['error']}


# ---- what is where ----------------------------------------------------------------------------------------

def size_text(size):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024


def span_text(item):
    if item['start'] is None:
        return '(no metadata)'
    clock = lambda t: datetime.datetime.fromtimestamp(t).strftime('%H:%M:%S')
    return f'{clock(item["start"])}-{clock(item["end"])}'


def copied(item, dest):
    """True when DEST (a parse() result) holds the same path with the same size."""
    return dest is not None and any(b['rel'] == item['rel'] and b['size'] == item['size'] for b in dest['bags'])


def state(item, dest):
    if item['open']:
        return 'recording' if item['open'] == 'recording' else 'not closed'
    if dest is not None and item['host'] == dest['host']:
        return 'at destination'
    return 'copied' if copied(item, dest) else 'new'


def rows(found, sources=None):
    """Every bag of the SOURCES hosts (all when None), newest first."""
    out = [b for h, r in found.items() if sources is None or h in sources for b in r['bags']]
    return sorted(out, key=lambda b: (b['date'], b['start'] or b['mtime'], b['host']), reverse=True)


def table(items, dest):
    """Plain aligned lines grouped by date: host, time range, label, size, topics, state, id."""
    if not items:
        return []
    host_w = max(len(b['host']) for b in items)
    label_w = min(max(len(b['label']) for b in items), 36)
    out, date = [], None
    for item in items:
        if item['date'] != date:
            date = item['date']
            out.append(date)
        out.append(f'  {item["host"]:<{host_w}}  {span_text(item):<17}  {item["label"][:label_w]:<{label_w}}  '
                   f'{size_text(item["size"]):>9}  {item["topics"]:>3} topics  {state(item, dest):<14}  {item["id"]}')
    return out


def find(items, wanted):
    """The bags WANTED names: its id or path, else the one whose id contains it (several hosts: each)."""
    exact = [b for b in items if wanted in (b['id'], b['rel'], f'{b["host"]}:{b["id"]}')]
    if exact:
        return exact
    found = [b for b in items if wanted in b['id']]
    if len({b['id'] for b in found}) == 1:
        return found
    if found:
        raise config.Error(f'{wanted!r} matches {len({b["id"] for b in found})} bags: '
                           + ', '.join(sorted({b['id'] for b in found})[:6]) + ' (give more of the name)')
    raise config.Error(f'no bag {wanted!r} (astro rec fetch --list shows them)')


def one_source(matches, wanted):
    hosts = sorted({b['host'] for b in matches})
    if len(hosts) > 1:
        raise config.Error(f'{wanted!r} is on {", ".join(hosts)}: add --from HOST')
    return matches[0]


# ---- copy -------------------------------------------------------------------------------------------------

def ssh_e(vessel, host, spec):
    """(rsync -e words, user@address) reaching HOST's own shell as `astro on` does."""
    words, target = remote.ssh_base(vessel, host, spec)
    return shlex.join(words + ['-o', 'BatchMode=yes']), target


def copy_argv(vessel, src, dest, rel, tty=False):
    """argv run here copying the bag REL from SRC to DEST ({host, spec (None: here), root}): rsync -a --partial
    (resumable) here, or on DEST's own shell when neither end is here."""
    flags = ['-a', '--partial'] + (['--info=progress2'] if tty else [])
    src_path = f'{src["root"].rstrip("/")}/{rel}/'
    dest_path = f'{dest["root"].rstrip("/")}/{rel}/'
    parent = os.path.dirname(dest_path.rstrip('/'))
    if src['spec'] is None and dest['spec'] is None:
        raise config.Error('the source and destination are both this machine')
    if dest['spec'] is None:
        shell, target = ssh_e(vessel, src['host'], src['spec'])
        return ['rsync', *flags, '-e', shell, f'{target}:{src_path}', dest_path]
    if src['spec'] is None:
        shell, target = ssh_e(vessel, dest['host'], dest['spec'])
        return ['rsync', *flags, '-e', shell, '--rsync-path', f'mkdir -p {shlex.quote(parent)} && rsync',
                src_path, f'{target}:{dest_path}']
    # Neither is here: DEST pulls from SRC over its own ssh (the operator key there when it has one).
    _, target = remote.ssh_base(vessel, src['host'], src['spec'])
    key = vessel.hosts_kind.get('operator_key')
    script = 'e="ssh -o BatchMode=yes -o ConnectTimeout=8"; '
    if key:
        k = '"$HOME"/.config/' + shlex.quote(str(key))
        script += (f'[ -f {k}/id_ed25519 ] && e="$e -i {k}/id_ed25519 -o UserKnownHostsFile={k}/known_hosts"; ')
    script += (f'mkdir -p {shlex.quote(parent)} && rsync {" ".join(flags)} -e "$e" '
               f'{shlex.quote(f"{target}:{src_path}")} {shlex.quote(dest_path)}')
    return remote.host_shell(vessel, dest['host'], dest['spec'], script)


def plan(items, dest, say=print):
    """The bags of ITEMS to copy to DEST: those still being written, at DEST already or on DEST are said
    and left out."""
    out = []
    for item in items:
        where = f'{item["host"]}:{item["id"]}'
        if item['host'] == dest['host']:
            say(f'{where}: skipped, it is on {dest["host"]} already')
        elif item['open']:
            say(f'{where}: skipped, still being written ({item["open"]}); astro rec stop closes it')
        elif copied(item, dest):
            say(f'{where}: already on {dest["host"]} ({size_text(item["size"])})')
        else:
            out.append(item)
    return out


def copy(vessel, found, items, dest_name, me, say=print):
    """Copy ITEMS to DEST_NAME one by one; one summary line and a `rec` log line here. The exit code."""
    dest = found[dest_name]
    todo = plan(items, dest, say)
    if not todo:
        say(f'nothing to copy to {dest_name}')
        return 0
    total = sum(b['size'] for b in todo)
    if dest['free'] and total > dest['free']:
        raise config.Error(f'{size_text(total)} picked but {dest_name} has {size_text(dest["free"])} free '
                           '(pick fewer, or free space there)')
    specs = {n: s for n, s in places(vessel, me)}
    end = {'host': dest_name, 'spec': specs.get(dest_name), 'root': dest['root']}
    if end['spec'] is None or any(specs.get(b['host']) is None for b in todo):
        if not shutil.which('rsync'):
            raise config.Error('rsync is not installed on this machine (install the rsync package)')
    tty = sys.stdout.isatty()
    done, failed = [], []
    for i, item in enumerate(todo, 1):
        start = {'host': item['host'], 'spec': specs.get(item['host']), 'root': found[item['host']]['root']}
        say(f'[{i}/{len(todo)}] {item["host"]}:{item["id"]}  {size_text(item["size"])}  -> {dest_name}')
        sys.stdout.flush()
        if end['spec'] is None:                     # a remote end makes its own (--rsync-path, mkdir -p)
            Path(end['root'], item['rel']).parent.mkdir(parents=True, exist_ok=True)
        try:
            code = subprocess.call(copy_argv(vessel, start, end, item['rel'], tty))
        except KeyboardInterrupt:
            say('stopped; run the same command again to resume')
            return 130
        (done if code == 0 else failed).append(item)
        if code:
            say(f'{item["host"]}:{item["id"]}: rsync exited {code}; run the same command again to resume')
    summary = (f'fetch {len(done)} bag{"s" if len(done) != 1 else ""} '
               f'({size_text(sum(b["size"] for b in done))}) to {dest_name}: '
               + ', '.join(f'{b["host"]}:{b["id"]}' for b in done) if done else f'fetch to {dest_name}: nothing copied')
    if failed:
        summary += f'; {len(failed)} failed'
    log.write(vessel.data_root(), 'rec', summary, vessel.astro.get('events') if config.have_ros() else None)
    return 1 if failed else 0


# ---- the wizard -------------------------------------------------------------------------------------------

def select(items, dest, title):
    """Indexes of ITEMS picked in a curses list grouped by date: space toggles, a all new, Enter confirms,
    Esc/q cancels (None)."""
    import curses
    import locale
    locale.setlocale(locale.LC_ALL, '')
    os.environ.setdefault('ESCDELAY', '25')
    lines = table(items, dest)
    rowmap, k = [], 0                       # display row -> item index (None: a date header)
    for line in lines:
        if line.startswith('  '):
            rowmap.append(k)
            k += 1
        else:
            rowmap.append(None)
    picked = set()
    pickable = lambda i: state(items[i], dest) in ('new', 'copied')

    def run(screen):
        curses.curs_set(0)
        row = next((r for r, i in enumerate(rowmap) if i is not None), 0)
        while True:
            h, w = screen.getmaxyx()
            room = max(h - 3, 1)
            top = max(0, min(row - room // 2, len(lines) - room))
            screen.erase()
            total = sum(items[i]['size'] for i in picked)
            head = f' {title}  ·  {len(picked)} picked, {size_text(total)} '
            try:
                screen.addstr(0, 0, head[:w - 1], curses.A_BOLD)
                for y, r in enumerate(range(top, min(top + room, len(lines))), 1):
                    i = rowmap[r]
                    if i is None:
                        screen.addstr(y, 0, lines[r][:w - 1], curses.A_BOLD)
                        continue
                    mark = '[x]' if i in picked else ('[ ]' if pickable(i) else ' - ')
                    text = f'{">" if r == row else " "}{mark}{lines[r][1:]}'
                    attr = curses.A_REVERSE if r == row else (0 if pickable(i) else curses.A_DIM)
                    screen.addstr(y, 0, text[:w - 1], attr)
                screen.addstr(h - 1, 0, ' up/down move  space pick  a all new  Enter copy  Esc/q cancel'[:w - 1],
                              curses.A_DIM)
            except curses.error:
                pass
            key = screen.get_wch()
            step = {curses.KEY_UP: -1, 'k': -1, curses.KEY_DOWN: 1, 'j': 1,
                    curses.KEY_PPAGE: -room, curses.KEY_NPAGE: room}.get(key)
            if step:
                target = max(0, min(len(lines) - 1, row + step))
                while rowmap[target] is None and 0 < target < len(lines) - 1:
                    target += 1 if step > 0 else -1
                row = target if rowmap[target] is not None else row
            elif key == ' ' and rowmap[row] is not None and pickable(rowmap[row]):
                picked.symmetric_difference_update({rowmap[row]})
            elif key == 'a':
                picked.update(i for i in range(len(items)) if state(items[i], dest) == 'new')
            elif key in ('\n', '\r', curses.KEY_ENTER):
                return sorted(picked)
            elif key in ('q', '\x1b'):
                return None
    return curses.wrapper(run)


def destinations(vessel, found, me):
    """[(host, about)] for the destination picker: this machine, operator hosts, then the others."""
    hosts = vessel.hosts()
    roles = lambda n: hosts.get(n, {}).get('roles', [])
    order = sorted(found, key=lambda n: (n != me and n != HERE, 'operator' not in roles(n), n))
    return [(n, f'{size_text(found[n]["free"])} free  '
                + ('this machine' if n in (me, HERE) else ' '.join(roles(n)))) for n in order]


def wizard(vessel, found, me, pick, sources=None, dest=None):
    items = rows(found, sources)
    if not items:
        print('no bags found')
        return 0
    here = me if me in found else HERE
    chosen = select(items, found.get(dest or here), f'Pick bags to copy (shown against {dest or here})')
    if chosen is None:
        raise Cancelled
    if not chosen:
        print('nothing picked')
        return 0
    if not dest:
        options = destinations(vessel, found, me)
        dest = pick(f'Copy {len(chosen)} bag{"s" if len(chosen) > 1 else ""} '
                    f'({size_text(sum(items[i]["size"] for i in chosen))}) to:', options, current=here)
    return copy(vessel, found, [items[i] for i in chosen], dest, me)


class Cancelled(Exception):
    pass


# ---- the command ------------------------------------------------------------------------------------------

def parser():
    p = argparse.ArgumentParser(prog='astro rec fetch', add_help=False)
    p.add_argument('bags', nargs='*')
    p.add_argument('--from', dest='source', action='append', default=[])
    p.add_argument('--to', dest='dest')
    p.add_argument('--new', action='store_true')
    p.add_argument('--date')
    p.add_argument('--list', dest='listing', action='store_true')
    p.add_argument('--json', action='store_true')
    p.add_argument('--names', action='store_true')
    return p


class Usage(config.Error):
    pass


def resolve(vessel, me, name):
    """A --from/--to name as searched: here, this machine's name, a registry host, or a host's role."""
    if name in (None, '', HERE, me):
        return me or HERE
    try:
        return remote.place(vessel, name)[0]
    except config.Error:
        raise Usage(f'no host {name!r} (astro rec fetch --list names the hosts it searched)') from None


def main(vessel, argv, pick, tty=None):
    """The exit code; PICK(title, options, current) is the destination picker (cli.choose)."""
    try:
        args = parser().parse_args([a for a in argv if a != '--here'])
    except SystemExit:
        raise Usage('usage: astro rec fetch [--list [--json|--names]] [--from HOST] [--to HOST] '
                    '[BAG...|--new|--date YYYY-MM-DD]') from None
    tty = (sys.stdin.isatty() and sys.stdout.isatty()) if tty is None else tty
    me = config.this_host()
    sources = [resolve(vessel, me, s) for s in args.source] or None
    dest = resolve(vessel, me, args.dest) if args.dest else None
    quiet = args.json or args.names
    say = (lambda line: print(line, file=sys.stderr)) if quiet else print
    if not quiet:
        say(f'searching {len(places(vessel, me))} computers for bags ...')
    found = search(vessel, me, say=say)
    if sources:
        missing = [s for s in sources if s not in found]
        if missing:
            raise config.Error(f'{", ".join(missing)} could not be searched (see above)')
    to = dest or (me if me in found else HERE)
    if to not in found:
        raise config.Error(f'{to} could not be searched, so nothing can be copied there')
    items = rows(found, sources)
    copying = args.bags or args.new or args.date
    if args.listing or (not copying and not tty):
        if args.json:
            print(json.dumps({'destination': to, 'hosts': {n: {'root': r['root'], 'free': r['free']}
                                                           for n, r in found.items()},
                              'bags': [dict(b, state=state(b, found[to])) for b in items]}, indent=1))
        elif args.names:
            for ident in dict.fromkeys(b['id'] for b in items if not b['open']):    # copyable ones
                about = next(b for b in items if b['id'] == ident)
                print(f'{ident}\t{about["host"]}  {size_text(about["size"])}  {span_text(about)}')
        else:
            print('\n'.join(table(items, found[to])) or 'no bags found')
            if not args.listing:
                print('copy with: astro rec fetch [--from HOST] [--to HOST] BAG... | --new | --date YYYY-MM-DD')
        return 0 if args.listing else 2
    if not copying:
        return wizard(vessel, found, me, pick, sources, dest)
    picked = []
    for wanted in args.bags:
        matches = [b for b in find(items, wanted) if b['host'] != to] or find(items, wanted)
        picked.append(one_source(matches, wanted))
    if args.new or args.date:
        chosen = [b for b in items if b['host'] != to and (not args.date or b['date'] == args.date)
                  and (not args.new or state(b, found[to]) == 'new')]
        if not chosen:
            say('no bags match' + (f' --date {args.date}' if args.date else '') + (' --new' if args.new else ''))
        seen = {b['rel'] for b in picked}
        picked += [b for b in chosen if not (b['rel'] in seen or seen.add(b['rel']))]
    return copy(vessel, found, picked, to, me, say)
