"""`astro check|issue|sync|show` and automatic issues: the field log, a git repo the vessel profile names.

    field: {repo: OWNER/NAME, url: GIT_URL, dir: ~/.local/share/astro/field, auto: true}

The repo holds checklists/NAME.md and one folder per session, sessions/<date>-<label>/ (plan.md, checks.yaml,
log.txt, results/, bags.txt, issues.pending.yaml); its GitHub issues are the to-do list. The label is an
existing sessions/<date>-* folder's, else the day's first labelled `rec start`, else `session`.

A checklist item is a top-level list line (`1.`, `-`, `- [ ]`). One that carries check: `COMMAND` is run
(exit 0 = ok; its last output line is the evidence); the others are manual (asked y/n on a terminal).

Automatic issues (auto) are filed only for a test aborted by an error, an `astro up` stack FAILED and an apply
the check refused. A failure's signature is kind + name + its first error line, normalised (numbers and
hashes dropped), kept in a hidden marker in the body: an open `auto` issue with it gets a short comment
instead (at most one per signature per hour), and at most 5 new auto issues are filed per day.
When the profile's astro.home is reached over ssh (the operator laptop: the day's data lives on the vessel),
sync first fetches the home host's day folder as a tar (`astro on HOME astro sync --pack DATE`: log.txt, its
.tz, results/*.yaml and a bags.txt index of names, spans and sizes, never the bag data) and sends the marks
kept pending there; show reads the synced copy, else today's `astro log` on home. ASTRO_FIELD_DIR overrides
field.dir (a test clone).

ASTRO_FIELD=off: no gh or git network calls and no auto issues (the unit tests set it); auto: false in the
profile turns off auto issues only.
"""
import datetime
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time

import yaml

from . import config, rec, remote
from .engine import log

ITEM = re.compile(r'^(?:\d+[.)]|[-*])\s+(?:\[[ xX]\]\s+)?(.+)$')
CHECK = re.compile(r'\s*(?:[-–—]\s*)?check:\s*`([^`]+)`')
MARKER = 'astro-signature'
CAP = 5                  # new auto issues per day
QUIET_S = 3600           # one comment per signature per hour
TIMEOUT_S = 20


def online():
    return os.environ.get('ASTRO_FIELD') != 'off'


def run(argv, stdin=None, timeout=TIMEOUT_S, cwd=None):
    """CompletedProcess, or None when the tool is missing, timed out or ASTRO_FIELD=off."""
    if not online() or not shutil.which(argv[0]):
        return None
    try:
        return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        return None


def first_line(text):
    return next((line.strip() for line in str(text).splitlines() if line.strip()), '')


def signature(kind, name, error):
    """A failure's identity: kind, name and the first error line without its numbers and hashes."""
    text = first_line(error).lower()
    text = re.sub(r'\b[0-9a-f]{6,}\b', '#', text)
    text = re.sub(r'[-+]?\d+(?:\.\d+)?', 'N', text)
    text = re.sub(r'\s+', ' ', text)
    return hashlib.sha1(f'{kind}|{name}|{text}'.encode()).hexdigest()[:12]


def parse_checklist(text):
    """[{section, text, check}] of a checklist's top-level items (code blocks and nested lines skipped)."""
    items, section, fence = [], '', False
    for line in text.splitlines():
        if line.lstrip().startswith('```'):
            fence = not fence
            continue
        if fence:
            continue
        if line.startswith('#'):
            section = line.lstrip('#').strip()
            continue
        match = ITEM.match(line)
        if match:
            body = match.group(1)
            found = CHECK.search(body)
            items.append(dict(section=section, text=CHECK.sub('', body).strip(),
                              check=found.group(1) if found else None))
    return items


def strip_samples(data):
    """A result without the raw samples of its holds (each hold keeps its command and times)."""
    if isinstance(data, dict):
        return {k: ([strip_samples({j: w for j, w in h.items() if j != 'samples'}) if isinstance(h, dict) else h
                     for h in v] if k == 'holds' and isinstance(v, list) else strip_samples(v))
                for k, v in data.items()}
    if isinstance(data, list):
        return [strip_samples(v) for v in data]
    return data


def size_text(path):
    total = sum(p.stat().st_size for p in Path(path).rglob('*') if p.is_file())
    for unit in ('B', 'K', 'M', 'G'):
        if total < 1024 or unit == 'G':
            return f'{total:.0f}{unit}' if unit == 'B' else f'{total:.1f}{unit}'
        total /= 1024


def load(path):
    data = config.load_yaml(path)
    return data if data is not None else {}


def dump(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


class Field:
    def __init__(self, vessel):
        spec = vessel.astro.get('field') or {}
        if not spec.get('repo'):
            raise config.Error(f'vessel profile {vessel.name} has no astro.field.repo (the field log repo)')
        self.vessel, self.root = vessel, vessel.data_root()
        self.repo = str(spec['repo'])
        self.url = str(spec.get('url') or f'https://github.com/{self.repo}.git')
        self.dir = Path(remote.expand(os.environ.get('ASTRO_FIELD_DIR') or spec.get('dir')
                                      or '~/.local/share/astro/field'))
        self.auto_on = spec.get('auto', True) is not False

    # ---- where things are -------------------------------------------------------------------------

    def label(self, day):
        for line in filter(None, map(log.parse_line, log.read(self.root, day))):
            found = re.match(r'start bags/\d{6}-(\S+)', line[2]) if line[1] == 'rec' else None
            if found:
                return found.group(1)
        return 'session'

    def session(self, day=None):
        day = day or datetime.date.today()
        found = sorted(p for p in (self.dir / 'sessions').glob(f'{day.isoformat()}-*') if p.is_dir())
        return found[0] if found else self.dir / 'sessions' / f'{day.isoformat()}-{self.label(day)}'

    def git(self, *args, timeout=60):
        return run(['git', '-C', str(self.dir), *args], timeout=timeout)

    # ---- issues -----------------------------------------------------------------------------------

    def body(self, text, detail=''):
        found = rec.running(self.root)
        lines = log.read(self.root)[-10:]
        out = [text, '', f'- time: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}', f'- vessel: {self.vessel.name}',
               f'- bag: {Path(found[1]).name if found else "(not recording)"}']
        if detail:
            out += ['', '```', detail.rstrip(), '```']
        out += ['', 'Last log lines:', '```', *lines, '```']
        return '\n'.join(out) + '\n'

    def create(self, entry):
        """The new issue's URL, or None (offline, or gh refused: its reason is printed)."""
        argv = ['gh', 'issue', 'create', '-R', self.repo, '--title', entry['title'], '--body-file', '-']
        for label in entry.get('labels') or []:
            argv += ['--label', label]
        done = run(argv, entry['body'])
        if done and done.returncode == 0:
            return first_line(done.stdout.splitlines()[-1] if done.stdout.strip() else '')
        if done:
            print(f'gh: {first_line(done.stderr)}', file=sys.stderr)
        return None

    def open_auto(self):
        """{signature: issue number} of the open auto issues, or None offline."""
        done = run(['gh', 'issue', 'list', '-R', self.repo, '--label', 'auto', '--state', 'open', '--limit', '200',
                    '--json', 'number,body'])
        if not done or done.returncode:
            return None
        out = {}
        for row in yaml.safe_load(done.stdout) or []:
            found = re.search(MARKER + r': (\w+)', row.get('body') or '')
            if found:
                out.setdefault(found.group(1), row['number'])
        return out

    def pending_path(self):
        return self.session() / 'issues.pending.yaml'

    def queue(self, entry):
        path = self.pending_path()
        items = load(path).get('issues') or []
        dump(path, {'issues': items + [entry]})
        return path

    def comment(self, number, entry, state):
        """At most one comment per signature per hour; True when the comment went out (or is not due)."""
        last = (state.get('comments') or {}).get(entry['signature'], 0)
        if time.time() - last < QUIET_S:
            print(f'auto issue #{number} is open ({entry["title"]}); commented within the hour')
            return True
        text = f'Again at {entry["time"]} on {self.vessel.name}: {entry["error"]}'
        done = run(['gh', 'issue', 'comment', str(number), '-R', self.repo, '--body', text])
        if not done or done.returncode:
            return False
        state.setdefault('comments', {})[entry['signature']] = time.time()
        print(f'auto issue #{number}: added a comment ({entry["title"]})')
        return True

    def state_path(self):
        return self.dir / '.auto.yaml'

    def auto(self, kind, name, error, detail=''):
        """File one auto issue for a failure, comment on its open twin, or queue it offline (see the module)."""
        error = first_line(error)
        sig = signature(kind, name, error)
        now = datetime.datetime.now()
        entry = dict(title=f'{kind} {name}: {error}'[:120], labels=['auto'], signature=sig, error=error,
                     time=f'{now:%Y-%m-%d %H:%M:%S}',
                     body=self.body(f'astro filed this automatically: {kind} {name} failed.\n\n    {error}', detail)
                     + f'\n<!-- {MARKER}: {sig} -->\n')
        state = load(self.state_path())
        day = now.date().isoformat()
        filed = int((state.get('filed') or {}).get(day, 0))
        twins = self.open_auto()
        if twins is not None and sig in twins:     # never a second issue for an open one
            if self.comment(twins[sig], entry, state):
                dump(self.state_path(), state)
            return 'comment'
        pending = load(self.pending_path()).get('issues') or []
        if any(p.get('signature') == sig for p in pending):
            return 'queued'
        if filed >= CAP:
            print(f'auto issue not filed: {CAP} already today ({entry["title"]})', file=sys.stderr)
            return 'cap'
        state.setdefault('filed', {})[day] = filed + 1
        dump(self.state_path(), state)
        url = self.create(entry) if twins is not None else None
        if url:
            print(f'auto issue: {url}')
            return 'filed'
        print(f'auto issue queued: {self.queue(entry)} (astro sync files it)')
        return 'queued'

    def flush(self):
        """File the session's queued issues; those that fail stay queued. (sent, left)."""
        path = self.pending_path()
        items = load(path).get('issues') or []
        if not items:
            return 0, 0
        twins, state, left = self.open_auto(), load(self.state_path()), []
        for entry in items:
            if twins is None:
                left.append(entry)
            elif entry.get('signature') in twins:
                if not self.comment(twins[entry['signature']], entry, state):
                    left.append(entry)
            elif not self.create(entry):
                left.append(entry)
        dump(self.state_path(), state)
        if left:
            dump(path, {'issues': left})
        else:
            path.unlink()
        return len(items) - len(left), len(left)

    # ---- sync -------------------------------------------------------------------------------------

    def collect(self, day, session):
        """Copy the day's log, results (without raw samples) and a bag index into the session folder."""
        source = log.day_dir(self.root, day)
        written = []
        session.mkdir(parents=True, exist_ok=True)
        if (source / 'log.txt').exists():
            shutil.copyfile(source / 'log.txt', session / 'log.txt')
            written.append('log.txt')
        for path in sorted((source / 'results').glob('*.yaml')):
            dump(session / 'results' / path.name, strip_samples(load(path)))
            written.append(f'results/{path.name}')
        fetched = source / 'bags.txt'
        rows = fetched.read_text().splitlines() if fetched.exists() else bag_rows(source)
        if rows:
            (session / 'bags.txt').write_text('\n'.join(rows) + '\n')
            written.append('bags.txt')
        return written

    def fetch_home(self, home, day, dest):
        """Fetch HOME's day folder (see pack) into DEST/<date>/; True when it came."""
        try:
            _, argv, env = remote.home_words(self.vessel, home, ['sync', '--pack', day.isoformat()], False)
            done = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired, config.Error):
            return False
        start = done.stdout.find(b'\x1f\x8b')          # the gzip stream, past anything a shell rc printed
        if done.returncode or start < 0:
            return False
        target = Path(dest) / day.isoformat()
        target.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(fileobj=io.BytesIO(done.stdout[start:]), mode='r:gz') as tar:
                for member in tar.getmembers():
                    name = Path(member.name)
                    if member.isfile() and not name.is_absolute() and '..' not in name.parts:
                        out = target / name
                        out.parent.mkdir(parents=True, exist_ok=True)
                        out.write_bytes(tar.extractfile(member).read())
        except (tarfile.TarError, OSError):
            return False
        return True

    def fetch(self):
        """Clone, or pull, the repo into dir (keeping files written there before the first clone)."""
        if (self.dir / '.git').exists():
            done = self.git('pull', '--rebase', '--autostash', '-q')
        elif not self.dir.exists() or not any(self.dir.iterdir()):
            done = run(['git', 'clone', '-q', self.url, str(self.dir)], timeout=120)
        else:
            temp = self.dir.with_name(self.dir.name + '.clone')
            shutil.rmtree(temp, ignore_errors=True)
            done = run(['git', 'clone', '-q', '--no-checkout', self.url, str(temp)], timeout=120)
            if done and done.returncode == 0:
                shutil.move(str(temp / '.git'), str(self.dir / '.git'))
                shutil.rmtree(temp, ignore_errors=True)
                self.git('reset', '-q')
                deleted = self.git('ls-files', '-d').stdout.split()
                if deleted:
                    self.git('checkout', '-q', '--', *deleted)
        if not done or done.returncode:
            raise config.Error(f'git: {first_line(done.stderr) if done else "not run"} ({self.dir})')


def bag_rows(source):
    """`NAME  HOST:PATH  START-END  SIZE` of each bag in a day folder."""
    rows = []
    for bag in sorted(p for p in (Path(source) / 'bags').glob('*') if p.is_dir()):
        found = rec.span(bag)
        when = ('-'.join(f'{datetime.datetime.fromtimestamp(t):%H:%M:%S}' for t in found) if found
                else '(recording)')
        rows.append(f'{bag.name}  {socket.gethostname()}:{bag}  {when}  {size_text(bag)}')
    return rows


def pack(vessel, date, out):
    """`astro sync --pack DATE` (run on home): the day's log.txt, .tz, results/*.yaml and bags.txt as a
    tar.gz on OUT. Bag data never goes in."""
    try:
        day = datetime.date.fromisoformat(date)
    except ValueError:
        raise config.Error(f'{date}: expected YYYY-MM-DD') from None
    source = log.day_dir(vessel.data_root(), day)
    with tarfile.open(fileobj=out, mode='w|gz') as tar:
        for path in [source / 'log.txt', source / '.tz', *sorted((source / 'results').glob('*.yaml'))]:
            if path.is_file():
                tar.add(path, arcname=str(path.relative_to(source)))
        rows = bag_rows(source)
        if rows:
            data = ('\n'.join(rows) + '\n').encode()
            info = tarfile.TarInfo('bags.txt')
            info.size, info.mtime = len(data), int(time.time())
            tar.addfile(info, io.BytesIO(data))
    out.flush()
    return 0


# ---- verbs ------------------------------------------------------------------------------------------

def checklists(f):
    return ', '.join(p.stem for p in sorted((f.dir / 'checklists').glob('*.md'))) or 'none (astro sync)'


def run_check(command, timeout=60):
    """(ok, one line of evidence) of a check command."""
    try:
        done = subprocess.run(['bash', '-c', command], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, f'timed out after {timeout} s'
    out = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    err = [line.strip() for line in done.stderr.splitlines() if line.strip()]
    lines = (out or err) if done.returncode == 0 else (err or out)
    return done.returncode == 0, (lines[-1] if lines else f'exit {done.returncode}')[:160]


def check(vessel, name, listing=False):
    f = Field(vessel)
    path = f.dir / 'checklists' / f'{name}.md'
    if not path.exists():
        raise config.Error(f'no checklist {path} (checklists: {checklists(f)})')
    items = parse_checklist(path.read_text())
    store = f.session() / 'checks.yaml'
    saved = load(store)
    ticks = saved.setdefault(name, {})
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    section, counts = None, {}
    for n, item in enumerate(items, 1):
        if item['section'] != section:
            section = item['section']
            print(f'\n{section}' if section else '')
        head = f'{n:>3}. {item["text"]}'
        if listing:
            before = ticks.get(n, {}).get('state')
            print(f'{head}' + (f'   check: {item["check"]}' if item['check'] else '')
                  + (f'   [{before}]' if before else ''))
            continue
        evidence = ''
        if item['check']:
            ok, evidence = run_check(item['check'])
            state = 'ok' if ok else 'FAIL'
            print(f'{head}\n       {state:<4} {evidence}')
        elif tty:
            try:
                answer = input(f'{head}  done? [y/n, enter skips] ').strip().lower()
            except EOFError:
                answer = ''
            state = {'y': 'yes', 'yes': 'yes', 'n': 'no', 'no': 'no'}.get(answer, 'manual')
        else:
            state = 'manual'
            print(f'{head}\n       (manual)')
        counts[state] = counts.get(state, 0) + 1
        if state != 'manual':
            ticks[n] = dict(text=item['text'], state=state, evidence=evidence,
                            time=f'{datetime.datetime.now():%H:%M:%S}')
    if listing:
        return 0
    dump(store, saved)
    print('\n' + ', '.join(f'{v} {k}' for k, v in counts.items()) + f'  -> {store}')
    return 1 if counts.get('FAIL') or counts.get('no') else 0


def issue(vessel, text, labels):
    f = Field(vessel)
    entry = dict(title=text, body=f.body(text), labels=list(labels), time=f'{datetime.datetime.now():%Y-%m-%d %H:%M:%S}')
    url = f.create(entry)
    if url:
        print(url)
    else:
        print(f'queued: {f.queue(entry)} (astro sync files it)')
    return 0


def auto(vessel, kind, name, error, detail=''):
    """Field.auto for the verbs: never raises, a no-op without astro.field, auto: false or ASTRO_FIELD=off."""
    if not online():
        return None
    try:
        f = Field(vessel)
        return f.auto(kind, name, error, detail) if f.auto_on else None
    except config.Error:
        return None
    except Exception as error:     # a failing issue tracker must never hide the failure it reports
        print(f'auto issue skipped: {error}', file=sys.stderr)
        return None


def sync(vessel, home=None):
    """HOME: the astro.home the day's data is fetched from (None: this machine's data root)."""
    f = Field(vessel)
    if home:
        remote.replay_marks(vessel, home)
    probe = run(['git', 'ls-remote', '-q', f.url, 'HEAD'])
    if not probe or probe.returncode:
        print(f'astro sync: offline ({f.url} not reachable); nothing synced')
        return 0
    day = datetime.date.today()
    fetched = tempfile.TemporaryDirectory(prefix='astro-sync-') if home else None
    if fetched:
        if not f.fetch_home(home, day, fetched.name):
            fetched.cleanup()
            print(f'astro sync: {home} not reachable; its day log, results and bag index not synced '
                  '(synced sessions stay)')
            return 0
        f.root = Path(fetched.name)
    try:
        return _sync(f, day)
    finally:
        if fetched:
            fetched.cleanup()


def _sync(f, day):
    f.fetch()
    session = f.session(day)
    written = f.collect(day, session)
    sent, left = f.flush()
    where = str(session.relative_to(f.dir))
    f.git('add', '-A', '--', where)
    if f.git('diff', '--cached', '--quiet').returncode:
        f.git('commit', '-q', '-m', f'session {day.isoformat()}: sync')
        pushed = f.git('push', '-q', timeout=120)
        if pushed.returncode:
            f.git('pull', '--rebase', '--autostash', '-q')
            pushed = f.git('push', '-q', timeout=120)
        if pushed.returncode:
            raise config.Error(f'git push: {first_line(pushed.stderr)}')
        what = 'pushed'
    else:
        what = 'up to date'
    print(f'{where}: {len(written)} files, {what}; issues filed {sent}' + (f', {left} still queued' if left else ''))
    return 0


def home_log(vessel, home):
    """Today's log lines from `astro log` on HOME ([] with one line when it is not reachable)."""
    try:
        _, argv, env = remote.home_words(vessel, home, ['log'], False)
        done = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired, config.Error):
        done = None
    if not done or done.returncode:
        print(f'(not synced, and {home} not reachable)')
        return []
    return done.stdout.splitlines()


def show(vessel, date=None, home=None):
    """HOME: the day's data lives there (read the synced copy, else today's `astro log` on home)."""
    f = Field(vessel)
    try:
        day = datetime.date.fromisoformat(date) if date else datetime.date.today()
    except ValueError:
        raise config.Error(f'{date}: expected YYYY-MM-DD') from None
    session = f.session(day)
    print(f'== {session.name}  ({session})\n')
    plan = session / 'plan.md'
    print(plan.read_text().rstrip() if plan.exists() else '(no plan.md)')
    print('\n== checks')
    checks = load(session / 'checks.yaml')
    for name, ticks in checks.items():
        states = [t.get('state') for t in ticks.values()]
        print(f'{name}: ' + ', '.join(f'{states.count(s)} {s}' for s in dict.fromkeys(states)))
        for n, tick in ticks.items():
            if tick.get('state') in ('FAIL', 'no'):
                print(f'  {n}. {tick["text"]}  {tick["state"]} {tick.get("evidence", "")}'.rstrip())
    if not checks:
        print('(none)')
    print('\n== log')
    synced = (session / 'log.txt').read_text().splitlines() if (session / 'log.txt').exists() else []
    if home:
        lines = synced or (home_log(vessel, home) if day == datetime.date.today() else [])
    else:
        lines = log.read(f.root, day) or synced
    parsed = [p for p in map(log.parse_line, lines) if p]
    tests = [m.split()[0] for _, k, m in parsed if k == 'test' and m.split()]
    print(f'tests: {tests.count("start")} started, {tests.count("done")} done, {tests.count("abort")} aborted; '
          f'applies: {sum(k == "apply" for _, k, _ in parsed)}; marks: {sum(k == "mark" for _, k, _ in parsed)}; '
          f'bags: {sum(k == "rec" and m.startswith("start") for _, k, m in parsed)}')
    print('\n== open issues')
    done = run(['gh', 'issue', 'list', '-R', f.repo, '--state', 'open', '--limit', '50'])
    print(done.stdout.rstrip() or '(none)' if done and done.returncode == 0 else '(offline)')
    return 0
