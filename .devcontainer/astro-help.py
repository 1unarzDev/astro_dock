#!/usr/bin/env python3
"""Browse, fill in and run the commands that workspace packages describe in help.yaml (`astro help`).

    astro help                 interactive browser (a terminal is required)
    astro help --list [WORD]   plain text: the core commands, or full details of matching ones (any level)
    astro help WORD            the same details, without a terminal check
    astro help --all           also the setup-level commands (host setup, overlays, images)

help.yaml schema (unknown keys are ignored):

    package: NAME               # the group shown; the first file found per package wins
    summary: one line
    commands:
      - name: boat_trial check  # display name
        run: ros2 run pkg boat_trial check   # command prefix (default: name)
        summary: one plain sentence: what it does for you and when to use it (no config keys, topics or paths)
        level: core             # core (default) | setup: setup commands show only with --all
        host: ROLE|HOST         # optional: where it runs; `astro NAME` from elsewhere forwards it there
                                # (astro on HOST RUN ..., printing "(on HOST)"); --here runs it here
        ros: optional; the plain ROS 2 an astro verb composes (shown beside it)
        needs: [nav, drive+controller]   # optional: stacks (profile astro.stacks) astro starts first when
                                # down, their needs too; NAME+OPTION starts it with that `with` option
        conflicts: [drive]      # optional: stacks astro stops first when up; {drive: why} says why
                                # (--here or ASTRO_NO_AUTOSTART=1 skips both)
        notes: optional, at most two short plain sentences: what you see when it works, the common mistake
                                # and its fix, what to run next
        details: optional; the technical part (config keys, topics, files, exit codes), shown only when the
                                # entry is opened in the browser and at the end of `astro NAME --help`
        params:
          - name: --window      # "-x": option ("--x value", a bare switch when type bool;
                                # a switch defaulting to true turns off as --no-x);
                                # with "ros2 launch": "x:=value"; otherwise positional
            type: str           # str | int | float | bool | path | list (space-separated)
            default: 5.0
            choices: [a, b]
            choices_from: 'CMD' # shell command, run here: one choice per line, optionally VALUE<TAB>about;
                                # a missing required one is picked from that list on a terminal
                                # (with default: auto, its first line is what auto finds)
            description: one line   # what the value means in real terms; core entries: at least 8 words
                                # and, unless a bool or choice, e.g. VALUE
            free: why it is typed   # a str with no choices: why it cannot be picked (issue text, a label)
            required: false     # positional params default to required
            form: option        # positional | option | switch | launch, overrides the inference
        examples: [full command lines]

Rules: a noun with sub-actions is one entry per action (`rec start`, `rec stop`), never a command/verb param.
A param the program can detect is optional and defaults to auto.

Every command also runs as `astro NAME [args]` (its run line plus the args); the details show both.

Discovery order: astro_dock's own astro-help.yaml beside this script (astro's verbs first), then the workspace
sources ($ROS_WS/src/*/help.yaml and src/*/*/help.yaml), then the EXTRA files (astro sets them from the
vessel profile's astro.help), then share/*/help.yaml under each
AMENT_PREFIX_PATH entry. A confirmed command runs in bash, or, when ASTRO_HELP_OUT names a
file, is written there for the calling shell to run (so it keeps the shell's functions).
"""
import curses
import importlib.util
import locale
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import yaml

OWN = Path(__file__).resolve().with_name('astro-help.yaml')
FORMS = ('positional', 'option', 'switch', 'launch')
LEVELS = ('core', 'setup')
TRUE = ('true', 'on', 'yes', '1')
EXTRA = []                 # more help.yaml paths, set by the caller (astro: the profile's astro.help)


# ---- data -------------------------------------------------------------------------------

def workspace():
    """$ROS_WS, else derived as dev.bashrc does: the directory holding this .devcontainer."""
    return os.environ.get('ROS_WS') or str(OWN.parent).removesuffix('/.devcontainer')


def load_style():
    """astro's shared look (tools/astro/style.py) from the workspace; a plain fallback where it is absent
    (the image copies this file alone)."""
    for root in (workspace(), str(OWN.parent.parent)):
        path = Path(root, 'tools', 'astro', 'style.py')
        if path.is_file():
            spec = importlib.util.spec_from_file_location('astro_style', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    ansi = {'bold': '1', 'dim': '2', 'accent': '36', 'title': '1;36'}
    return types.SimpleNamespace(
        MARK='›', MARK_ASCII='>', CURRENT='(current)', SEP=' · ', SEP_ASCII=' - ', ANSI=ansi,
        paint=lambda name, text, on=True: f'\033[{ansi[name]}m{text}\033[0m' if on and text else text,
        mark=lambda selected, utf=True: ('› ' if utf else '> ') if selected else '  ',
        box=lambda on: '[x]' if on else '[ ]',
        type_name=lambda kind, choices=(): ('one of ' + '/'.join(map(str, choices)) if len(choices) <= 4
                                            else 'pick from a list') if choices else {
            'str': 'text', 'int': 'whole number', 'float': 'number', 'bool': 'on/off', 'path': 'file',
            'list': 'text, several'}.get(str(kind), str(kind)))


style = load_style()


def candidates(ws=None, prefixes=None):
    """Yield help.yaml paths in priority order."""
    yield OWN
    src = Path(ws or workspace()) / 'src'
    for pattern in ('*/help.yaml', '*/*/help.yaml'):
        yield from sorted(src.glob(pattern))
    for extra in EXTRA:                   # absent ones are skipped (an optional checkout)
        if Path(extra).is_file():
            yield Path(extra)
    if prefixes is None:
        prefixes = os.environ.get('AMENT_PREFIX_PATH', '').split(os.pathsep)
    for prefix in filter(None, prefixes):
        yield from sorted(Path(prefix, 'share').glob('*/help.yaml'))


def parse(path):
    """Return one package dict from a help.yaml, normalised; raise ValueError when malformed."""
    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict) or not isinstance(data.get('package'), str):
        raise ValueError('needs a "package" name')
    commands = []
    for command in data.get('commands') or []:
        if not isinstance(command, dict) or not command.get('name'):
            raise ValueError('every command needs a "name"')
        params = []
        for param in command.get('params') or []:
            if not isinstance(param, dict) or not param.get('name'):
                raise ValueError(f'{command["name"]}: every param needs a "name"')
            param = dict(param, type=str(param.get('type', 'str')))
            param['name'] = str(param['name'])
            param['form'] = param.get('form') or infer_form(command, param)
            if param['form'] not in FORMS:
                raise ValueError(f'{command["name"]}: unknown form {param["form"]}')
            params.append(param)
        level = str(command.get('level') or 'core')
        if level not in LEVELS:
            raise ValueError(f'{command["name"]}: level {level} (core or setup)')
        commands.append(dict(command, name=str(command['name']), run=str(command.get('run') or command['name']),
                             level=level, host=str(command.get('host') or ''),
                             summary=str(command.get('summary', '')), notes=str(command.get('notes') or ''),
                             details=str(command.get('details') or ''),
                             ros=str(command.get('ros') or ''),
                             needs=[str(n) for n in command.get('needs') or []],
                             conflicts=[c if isinstance(c, dict) else str(c) for c in command.get('conflicts') or []],
                             params=params, examples=[str(e) for e in command.get('examples') or []]))
    return {'package': data['package'], 'summary': str(data.get('summary', '')),
            'commands': commands, 'path': str(path)}


def discover(paths=None):
    """Return (packages, errors); the first file per package name wins."""
    packages, errors, seen = [], [], set()
    for path in candidates() if paths is None else paths:
        try:
            package = parse(path)
        except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError) as error:
            errors.append(f'help: {path}: {(str(error) or type(error).__name__).splitlines()[0]}')
            continue
        if package['package'] not in seen:
            seen.add(package['package'])
            packages.append(package)
    return packages, errors


def infer_form(command, param):
    name = param['name']
    if name.startswith('-'):
        return 'switch' if param.get('type') == 'bool' else 'option'
    if str(command.get('run') or command.get('name')).split()[:2] == ['ros2', 'launch']:
        return 'launch'
    return 'positional'


def is_required(param):
    return param.get('required', param['form'] == 'positional' and 'default' not in param)


def shown(value, param=None):
    """A value as typed on a command line; bools (and bool-typed strings) as true/false."""
    if isinstance(value, bool) or (param and param['type'] == 'bool' and value is not None):
        return 'true' if str(value).lower() in TRUE else 'false'
    return '' if value is None else str(value)


def build(command, values):
    """Return the command line for the values set; defaults and empty values are omitted."""
    words = [command['run']]
    for param in command['params']:
        name, value = param['name'], values.get(param['name'])
        if value is None or shown(value, param) in ('', shown(param.get('default'), param)):
            if is_required(param):
                hole = f'<{name.lstrip("-")}>'
                words.append({'option': f'{name} {hole}', 'launch': f'{name}:={hole}'}.get(param['form'], hole))
            continue
        if param['form'] == 'switch':
            # Differs from the default: on adds the switch, off (default on) adds --no-NAME.
            words.append(name if shown(value).lower() in TRUE else f'--no-{name.lstrip("-")}')
            continue
        try:
            quoted = (' '.join(map(shlex.quote, shlex.split(str(value)))) if param['type'] == 'list'
                      else shlex.quote(shown(value, param)))
        except ValueError:
            raise ValueError(f'{name}: unbalanced quote') from None
        if param['form'] == 'option':
            words.append(f'{name} {quoted}')
        elif param['form'] == 'launch':
            words.append(f'{name}:={quoted}')
        else:
            words.append(quoted)
    return ' '.join(words)


def missing(command, values):
    return [p['name'] for p in command['params']
            if is_required(p) and shown(values.get(p['name']), p) in ('', shown(p.get('default'), p))]


def wrap(s, width):
    return textwrap.wrap(s, max(width, 10), break_on_hyphens=False)


def matches(package, command, word):
    word = word.lower()
    return any(word in text.lower() for text in (package['package'], command['name'], command['summary']))


def kind(param):
    """What a field holds, as the form shows it: choice, switch, or its type."""
    if param.get('choices') or param.get('choices_from'):
        return 'choice'
    return 'switch' if param['form'] == 'switch' else param['type']


def type_text(param):
    """What a field takes, in plain words: number, whole number, on/off, file, text, one of A/B/C, or pick
    from a list (choices found when opened)."""
    if param.get('choices_from') and not param.get('choices'):
        return 'pick from a list'
    if kind(param) == 'switch' or param['type'] == 'bool':
        return 'on/off'
    return style.type_name(param['type'], param.get('choices') or ())


def synopsis(command):
    """The args part of a usage line, from the params: <file> [--seconds N] {a|b} [names...]."""
    words = []
    for param in command['params']:
        name, choices = param['name'], [str(c) for c in param.get('choices') or []]
        if param['type'] == 'bool' and param['form'] == 'launch':
            choices = ['true', 'false']
        if choices and len(choices) <= 4:
            value = '{' + '|'.join(choices) + '}'
        elif param['form'] == 'positional':
            value = name
        elif param['type'] in ('int', 'float'):
            value = 'N'
        else:
            value = 'PATH' if param['type'] == 'path' else name.lstrip('-').upper()
        word = {'switch': name, 'option': f'{name} {value}', 'launch': f'{name}:={value}'}.get(param['form'], value)
        word += '...' if param['type'] == 'list' and param['form'] == 'positional' else ''
        if not is_required(param):
            word = f'[{word}]'
        elif param['form'] == 'positional' and not value.startswith('{'):
            word = f'<{word}>'
        words.append(word)
    return ' '.join(words)


def tree(commands):
    """[(command or None, depth, label)]: names grouped by noun, each sub-entry under its noun (a noun
    without an entry of its own is a header row, None)."""
    root = {'cmd': None, 'kids': {}}
    for command in commands:
        node = root
        for word in command['name'].split():
            node = node['kids'].setdefault(word, {'cmd': None, 'kids': {}})
        node['cmd'] = command
    out = []

    def walk(node, depth):
        for word, kid in node['kids'].items():
            label = word
            while kid['cmd'] is None and len(kid['kids']) == 1:      # a lone chain reads as one name
                (more, kid), = kid['kids'].items()
                label += ' ' + more
            out.append((kid['cmd'], depth, label))
            walk(kid, depth + 1)
    walk(root, 0)
    return out


def parse_choices(text):
    """[(value, about)] from a choices_from command's output: one per line, optionally VALUE<TAB>about."""
    out = []
    for line in text.splitlines():
        value, _, about = line.partition('\t')
        if value.strip():
            out.append((value.strip(), about.strip()))
    return out


def run_choices(command, env=None, timeout=30):
    """[(value, about)] printed by a choices_from shell command (run here); [] when it fails."""
    try:
        done = subprocess.run(['bash', '-c', command], capture_output=True, text=True, timeout=timeout,
                              env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_choices(done.stdout)


def choice_rows(choices, current=None):
    """[(value column, about)] of a picker: each value, the CURRENT one followed by (current), padded to
    one width so the descriptions line up."""
    heads = [f'{v} {style.CURRENT}' if v == current else str(v) for v, _ in choices]
    width = max(len(h) for h in heads)
    return [(f'{h:<{width}}', about) for h, (_, about) in zip(heads, choices)]


def pick(title, choices, stream=None, current=None):
    """The chosen value from [(value, about)]: an arrow-key list on a terminal (a numbered one without
    curses), the CURRENT value marked (current); None when cancelled. Without a terminal it prints the
    choices and returns None."""
    stream = stream or sys.stderr
    if not choices:
        print(f'{title} nothing to choose from', file=stream)
        return None
    rows = choice_rows(choices, current)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(f'{title} give one of', file=stream)
        for head, about in rows:
            print(f'  {head}  {about}'.rstrip())
        return None
    try:
        locale.setlocale(locale.LC_ALL, '')
        os.environ.setdefault('ESCDELAY', '25')
        return curses.wrapper(lambda screen: Picker(screen, title, choices, current).run())
    except curses.error:
        pass
    print(title, file=stream)
    for i, (head, about) in enumerate(rows, 1):
        print(f'{i:>3}) {head}  {about}'.rstrip(), file=stream)
    try:
        entered = input(f'number or name [1-{len(choices)}, empty cancels]: ').strip()
    except EOFError:
        return None
    if entered.isdigit() and 1 <= int(entered) <= len(choices):
        return choices[int(entered) - 1][0]
    return next((v for v, _ in choices if v == entered), None)


MENU_KEYS = {True: '↑↓ move · Enter choose · Esc/q cancel',
             False: 'up/down move - Enter choose - Esc/q cancel'}


def colours():
    """{'accent': attr} once curses colours are set up (the terminal's own background kept); {} with
    NO_COLOR or a terminal without colours."""
    if not curses.has_colors():
        return {}
    try:
        curses.start_color()
        curses.use_default_colors()       # also without colours: else curses paints its own black background
        if 'NO_COLOR' in os.environ:
            return {}
        curses.init_pair(1, curses.COLOR_CYAN, -1)
    except curses.error:
        return {}
    return {'accent': curses.color_pair(1)}


def menu(screen, y, x, h, w, title, choices, row=0, attr=0, current=None, utf=True, rule=False):
    """Draw and run an arrow-key list in the box (y, x, h, w): the title, one row per choice with its
    dim description (the CURRENT index marked (current); the cursor row marked › and bold, its value in
    the accent ATTR), the keys (and, with RULE, a line closing the box); the chosen index or None (Esc, q)."""
    values = [v for v, _ in choices]
    rows = choice_rows(choices, values[current] if current is not None else None)
    while True:
        room = max(h - 2 - rule, 1)
        top = max(0, min(row - room // 2, len(choices) - room))
        for i in range(h):
            try:
                screen.addstr(y + i, x, ' ' * (w - 1))
            except curses.error:
                pass
        try:
            screen.addstr(y, x, f' {title}'[:w - 1], curses.A_BOLD)
            for i, (head, about) in enumerate(rows[top:top + room], 1):
                index = top + i - 1
                chosen = index == row
                screen.addstr(y + i, x, f' {style.mark(chosen, utf)}'[:w - 1], curses.A_BOLD if chosen else 0)
                screen.addstr(y + i, x + 3, head[:max(w - 4, 0)], (curses.A_BOLD | attr) if chosen else 0)
                if about and x + 5 + len(head) < x + w - 1:
                    screen.addstr(y + i, x + 5 + len(head), about[:w - 6 - len(head)], curses.A_DIM)
            more = f'  ({row + 1}/{len(choices)})' if len(choices) > room else ''
            screen.addstr(y + h - 1 - rule, x, f' {MENU_KEYS[utf]}{more}'[:w - 1], curses.A_DIM)
            if rule:
                screen.addstr(y + h - 1, x, ('─' if utf else '-') * (w - 1), curses.A_DIM)
        except curses.error:
            pass
        key = screen.get_wch()
        if key in (curses.KEY_UP, 'k'):
            row = max(0, row - 1)
        elif key in (curses.KEY_DOWN, 'j'):
            row = min(len(choices) - 1, row + 1)
        elif key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
            row = max(0, min(len(choices) - 1, row + (room if key == curses.KEY_NPAGE else -room)))
        elif key in ('\n', '\r', curses.KEY_ENTER, ' '):
            return row
        elif key in ('q', '\x1b', curses.KEY_LEFT):
            return None


def utf8():
    """True when the terminal takes UTF-8 (Python coerces a C locale itself, so ask the locale variables)."""
    lang = next((os.environ[v] for v in ('LC_ALL', 'LC_CTYPE', 'LANG') if os.environ.get(v)), '')
    return 'utf' in lang.lower().replace('-', '') and os.environ.get('TERM') != 'linux'


class Picker:
    def __init__(self, screen, title, choices, current=None):
        self.screen, self.title, self.choices = screen, title, choices
        values = [v for v, _ in choices]
        self.current = values.index(current) if current in values else None
        self.accent = colours().get('accent', 0)
        curses.curs_set(0)

    def run(self):
        h, w = self.screen.getmaxyx()
        self.screen.erase()
        index = menu(self.screen, 0, 0, min(h, len(self.choices) + 2), w, self.title, self.choices,
                     self.current or 0, self.accent, current=self.current, utf=utf8())
        return None if index is None else self.choices[index][0]


# ---- plain text -------------------------------------------------------------------------

def text_width():
    """The width plain text wraps to: the terminal's, at most 100 (80 without a terminal)."""
    return max(min(shutil.get_terminal_size((80, 24)).columns, 100), 40)


def state_text(param):
    """A param's unset state in plain text: default: X, auto (finds it), required or optional."""
    if 'default' in param:
        default = param['default']
        if shown(default) == 'auto' and 'auto' not in [str(c) for c in param.get('choices') or []]:
            return 'auto (finds it)'
        return f'default: {onoff(default) if type_text(param) == "on/off" else shown(default, param)}'
    return 'required' if is_required(param) else 'optional'


def details(command, paint=lambda code, s: s, width=None):
    """The full description of one command: its command line(s), ros equivalent, notes, params (name, type
    in plain words and state, the description wrapped under them), examples, then its technical details."""
    width = (width or text_width()) - 4
    out = [paint('2', f'    $ {build(command, {})}')]
    if not command['run'].startswith('astro '):
        out.append(paint('2', f'    $ astro {command["name"]}{build(command, {})[len(command["run"]):]}'))
    if command['ros']:
        out.append(paint('2', f'    ros: {command["ros"]}'))
    if command['host']:
        out.append(paint('2', f'    runs on {command["host"]} (forwarded from elsewhere; --here runs it here)'))
    if stacks_note(command):
        out.append(paint('2', f'    {stacks_note(command)}'))
    if command['notes']:
        out.extend('    ' + line for line in wrap(command['notes'], width))
    if command['params']:
        out.append('')
        name_w = min(max(len(p['name']) for p in command['params']), 18)
    for param in command['params']:
        about = param.get('description', '')
        name = paint('1', f'{param["name"]:<{name_w}}')
        out.append(f'    {name}  {type_text(param)}'
                   + paint('2', f'{style.SEP}{state_text(param)}'))
        out.extend(paint('2', ' ' * (name_w + 6) + line) for line in wrap(about, width - name_w - 2))
    if command['examples']:
        out.append('')
    out.extend(paint('2', f'    e.g. {example}') for example in command['examples'])
    if command.get('details'):
        out.append('')
    out.extend(paint('2', '    ' + line) for line in wrap(command.get('details') or '', width))
    return out


def stacks_note(command):
    """One line: the stacks astro starts or stops before the command (needs/conflicts), or ''."""
    parts = []
    if command.get('needs'):
        parts.append('starts first if down: ' + ', '.join(command['needs']))
    if command.get('conflicts'):
        parts.append('stops first if up: ' + ', '.join(next(iter(c)) if isinstance(c, dict) else c
                                                       for c in command['conflicts']))
    return '; '.join(parts) + (' (--here skips this)' if parts else '')


def text(packages, word=None, color=False, exact=None, levels=('core',)):
    """Every command of LEVELS with its summary, or full details of the commands matching word (any level;
    or, with exact, the commands of that package whose name is exact or starts with it)."""
    def paint(code, s):
        return f'\033[{code}m{s}\033[0m' if color and s else s
    full = text_width()
    out = []
    for package in packages:
        if exact:
            commands = [c for c in package['commands'] if package['package'] == exact[0]
                        and (c['name'] == exact[1] or c['name'].startswith(exact[1] + ' '))]
        else:
            commands = [c for c in package['commands']
                        if (matches(package, c, word) if word else c['level'] in levels)]
        if not commands:
            continue
        out.append(paint('1;36', package['package']) + paint('2', f' · {package["summary"]}'))
        ordered = [c for c, _, _ in tree(commands) if c]            # sub-entries right after their noun
        heads = {c['name']: synopsis(c) for c in ordered}
        width = min(max(len(f'{c["name"]} {heads[c["name"]]}'.rstrip()) for c in ordered), 30)
        for command in ordered:
            # The name (bold) and its usage (dim), then the summary in a column wrapped to the terminal;
            # a usage longer than the column puts the summary on the lines below, at the column.
            name, usage = command['name'], heads[command['name']]
            head = f'{name} {usage}'.rstrip()
            painted = paint('1', name) + (' ' + paint('2', usage) if usage else '')
            lines = wrap(command['summary'], full - width - 4) or ['']
            if len(head) <= width:
                out.append(f'  {painted}{" " * (width - len(head))}  {lines[0]}'.rstrip())
                lines = lines[1:]
            else:
                out.extend(' ' * (len(name) + 3) + paint('2', part) if i else '  ' + paint('1', name) + ' ' + paint('2', part)
                           for i, part in enumerate(textwrap.wrap(usage, full - len(name) - 3,
                                                                  break_on_hyphens=False)))
            out.extend(' ' * (width + 4) + line for line in lines)
            if word or exact:
                out.extend(details(command, paint))
                out.append('')
        out.append('')
    return '\n'.join(out).rstrip() + '\n' if out else f'No command matches {word or exact[1]!r}.\n'


# ---- browser ----------------------------------------------------------------------------

UNICODE = dict(tl='╭', tr='╮', bl='╰', br='╯', h='─', v='│', lt='├', rt='┤', bt='┴', tt='┬',
               sep=' · ', move='↑↓', enter='Enter', more='…', dash='—')
ASCII = dict(tl='+', tr='+', bl='+', br='+', h='-', v='|', lt='+', rt='+', bt='+', tt='+',
             sep=' - ', move='up/down', enter='Enter', more='~', dash='-')
# The key line under the screen: groups of (key, what it does), split by meaning (moving, acting).
HINTS = {'list': [[('move', 'move'), ('enter', 'open'), ('/', 'filter'), ('x', 'examples')],
                  [('r', 'run'), ('q', 'quit'), ('?', 'keys')]],
         'params': [[('move', 'move'), ('enter', 'change'), ('Tab', 'next'), ('Del', 'default')],
                    [('r', 'run'), ('q', 'back'), ('?', 'keys')]]}
# Every key, shown by ? in the right pane.
KEYS = [('↑↓ / j k', 'move in the list or the form'),
        ('Enter / →', 'open the command: its form'),
        ('Enter / Space', 'in the form: change the field (pick, type, or switch on/off)'),
        ('Tab / Shift-Tab', 'next / previous field'),
        ('Del / d', 'back to its default'),
        ('← / h', 'back to the list'),
        ('/', 'filter by a word (Esc clears)'),
        ('x', 'examples instead of the form'),
        ('PgUp / PgDn', 'scroll a long command line'),
        ('r', 'run the line shown at the bottom'),
        ('q / Esc', 'back, then quit'),
        ('?', 'this list (any key closes it)')]


def invocation(command, values):
    """The line the browser runs: `astro NAME ...` for a command with a host (so it is forwarded there),
    else its run line."""
    line = build(command, values)
    return f'astro {command["name"]}{line[len(command["run"]):]}' if command['host'] else line


def is_set(param, values):
    """True when the form holds a value for PARAM that differs from its default (so it is on the line)."""
    return param['name'] in values and shown(values[param['name']], param) not in ('', shown(param.get('default'), param))


def placeholder(param, detected=None, dash='—'):
    """What an unset field shows: auto: FOUND, auto (finds it), default: X, required — pick one (a choice;
    else required — type it) or not set."""
    default = param.get('default')
    if shown(default) == 'auto' and 'auto' not in [str(c) for c in param.get('choices') or []]:
        return f'auto: {detected}' if detected else 'auto (finds it)'
    if 'default' in param:
        return f'default: {onoff(default) if kind(param) == "switch" else shown(default, param)}'
    if is_required(param):
        return f'required {dash} {"pick one" if kind(param) == "choice" else "type it"}'
    return 'not set'


def onoff(value):
    return 'on' if shown(value).lower() in TRUE else 'off'


def value_text(param, value=None, set_=False, detected=None, dash='—'):
    """A field's value as the form shows it: the value set, else its unset state (placeholder); on/off as
    [x] on / [ ] off."""
    if kind(param) == 'switch' or (param['type'] == 'bool' and not param.get('choices')):
        return f'{style.box(onoff(value if set_ else param.get("default")) == "on")} ' \
               f'{onoff(value if set_ else param.get("default"))}'
    return shown(value, param) if set_ else placeholder(param, detected, dash)


def field_hint(param, focused=False):
    """The short hint after a field's value: its type in plain words; a focused choice says Enter
    changes it."""
    if focused and kind(param) == 'choice':
        return 'Enter to change'
    return type_text(param)


class Browser:
    def __init__(self, screen, packages, levels=('core',)):
        self.screen, self.packages, self.levels = screen, packages, levels
        self.values, self.word, self.status, self.focus = {}, '', '', 'list'
        self.row = self.param = self.top = self.cmd_top = 0
        self.examples = self.keys = False
        self.opened = None                    # the entry Enter opened (its details show), when it has no params
        self.detected = {}                    # (package, command, param) -> what auto finds ('' none)
        self.g = UNICODE if utf8() else ASCII
        self.color = colours()
        curses.curs_set(0)

    def style(self, name):
        return {'dim': curses.A_DIM, 'bold': curses.A_BOLD}.get(name, self.color.get(name, 0))

    def put(self, y, x, s, attr=0, width=None):
        h, w = self.screen.getmaxyx()
        width = min(width or w, w - x)
        if 0 <= y < h and width > 0:
            try:
                self.screen.addstr(y, x, s[:width], attr)
            except curses.error:  # the bottom-right cell
                pass

    def rows(self):
        """[(package, command or None, depth, label)]: each package, then its commands grouped by noun; a
        blank row (depth -2) between packages."""
        out = []
        for package in self.packages:
            commands = [c for c in package['commands']
                        if (matches(package, c, self.word) if self.word else c['level'] in self.levels)]
            if commands:
                if out:
                    out.append((package, None, -2, ''))
                out.append((package, None, -1, package['package']))
                out.extend((package, c, depth, label) for c, depth, label in tree(commands))
        return out

    def selected(self, rows):
        commands = [r for r in rows if r[1]]
        if not commands:
            return None, None
        self.row = max(0, min(self.row, len(commands) - 1))
        return commands[self.row][:2]

    def values_for(self, package, command):
        return self.values.setdefault((package['package'], command['name']), {})

    def draw(self):
        g, (h, w) = self.g, self.screen.getmaxyx()
        self.screen.erase()
        if h < 12 or w < 50:
            self.put(0, 0, 'Enlarge the terminal (50x12), or press q.')
            return
        rows = self.rows()
        package, command = self.selected(rows)
        # The built line is wrapped, never truncated: it is what runs. Lines that do not fit
        # scroll with PgUp/PgDn, and the separator shows which part is on screen.
        line = invocation(command, self.values_for(package, command)) if command else ''
        parts = [('  ' if i else '$ ') + line[i:i + w - 6] for i in range(0, len(line), w - 6)]
        room = h - 10
        self.cmd_top = max(0, min(self.cmd_top, len(parts) - room))
        shown_line = parts[self.cmd_top:self.cmd_top + room]
        left, bottom = min(34, w // 3), h - 4 - max(len(shown_line), 1)
        count = sum(len(p['commands']) for p in self.packages)
        frame = self.style('dim')
        title = f' astro help{g["sep"]}{count} commands'
        title += f'{g["sep"]}filter: {self.word}' if self.word else ''
        title += f'{g["sep"]}? for keys '
        self.put(0, 0, g['tl'] + g['h'] + g['h'] * (w - 3) + g['tr'], frame)
        self.put(0, 2, title, curses.A_BOLD)
        for y in range(1, h - 2):
            self.put(y, 0, g['v'], frame)
            self.put(y, w - 1, g['v'], frame)
            if y < bottom:
                self.put(y, left + 1, g['v'], frame)
        self.put(bottom, 0, g['lt'] + g['h'] * left + g['bt'] + g['h'] * (w - left - 3) + g['rt'], frame)
        if command:
            self.put(bottom, 2, ' runs ', curses.A_BOLD)
        if len(parts) > room:
            where = f' lines {self.cmd_top + 1}-{self.cmd_top + room} of {len(parts)}, PgUp/PgDn '
            self.put(bottom, w - len(where) - 2, where, curses.A_BOLD)
        self.put(h - 2, 0, g['bl'] + g['h'] * (w - 2) + g['br'], frame)
        self.draw_list(rows, command, left, bottom - 1)
        if self.keys:
            self.draw_keys(left + 3, w - left - 5, bottom - 1)
        elif command:
            self.draw_detail(package, command, left + 3, w - left - 5, bottom - 1)
        if command:
            for y, part in enumerate(shown_line, bottom + 1):
                self.put(y, 2, part, curses.A_BOLD, w - 4)
        self.put(h - 3, 2, self.status, curses.A_BOLD, w - 4)
        x = 1
        for group in HINTS[self.focus]:
            for key, label in group:
                key = g.get(key, key)
                self.put(h - 1, x, key, curses.A_BOLD)
                self.put(h - 1, x + len(key) + 1, label, curses.A_DIM)
                x += len(key) + len(label) + 3
            x += 2                                               # a wider gap between the groups

    def draw_keys(self, x, width, height):
        """Every key and what it does, in the right pane (? shows it, any key closes it)."""
        self.put(1, x, 'Keys', curses.A_BOLD, width)
        key_w = max(len(k) for k, _ in KEYS)
        y = 3
        for key, about in KEYS:
            lines = wrap(about, width - key_w - 2)
            for i, part in enumerate(lines):
                if y > height:
                    return
                if not i:
                    self.put(y, x, key, curses.A_BOLD, width)
                self.put(y, x + key_w + 2, part, 0, width - key_w - 2)
                y += 1

    def draw_list(self, rows, command, width, height):
        index = next((i for i, r in enumerate(rows) if r[1] is command), 0)
        self.top = max(min(self.top, index), index - height + 1, 0)
        for y, (package, cmd, depth, label) in enumerate(rows[self.top:self.top + height], 1):
            x = 2 + 2 * (depth + 1)
            if depth == -2:
                continue
            if depth < 0:
                self.put(y, 2, label, curses.A_BOLD, width - 1)
            elif cmd is None:                                   # a noun with no entry of its own
                self.put(y, x - 2, f'  {label}', self.style('dim'), width - x + 1)
            elif cmd is command:                                # the marker and bold; the accent while here
                self.put(y, x - 2, style.mark(True, self.g is UNICODE), curses.A_BOLD)
                self.put(y, x, label, curses.A_BOLD | (self.style('accent') if self.focus == 'list' else 0),
                         width - x - 1)
            else:
                self.put(y, x - 2, f'  {label}', 0, width - x + 1)

    def fields(self, package, command, width):
        """[(line segments, field index)] of the param form: per field its name, value and a short hint in
        columns, then its description, dim and indented under it (the focused field's whole, the others'
        first two lines)."""
        g, values, params = self.g, self.values_for(package, command), command['params']
        name_w = min(max(len(p['name']) for p in params), width // 3)
        texts = [self.value_text(package, command, p, values) for p in params]
        hint_w = max(len(field_hint(p, True)) for p in params)
        value_w = max(min(max(len(t) for t in texts), width - name_w - hint_w - 6), 8)
        out = []
        for i, (param, text) in enumerate(zip(params, texts)):
            focused = self.focus == 'params' and i == self.param
            if len(text) > value_w:
                text = text[:value_w - 1] + g['more']
            set_ = is_set(param, values)
            if set_:
                vstyle = curses.A_BOLD
            elif is_required(param) and 'default' not in param:
                vstyle = 0
            else:
                vstyle = curses.A_DIM
            head = [(style.mark(focused, g is UNICODE), curses.A_BOLD),
                    (f'{param["name"][:name_w]:<{name_w}}  ',
                     (curses.A_BOLD | self.style('accent')) if focused else 0),
                    (f'{text:<{value_w}}  ', vstyle), (field_hint(param, focused), curses.A_DIM)]
            out.append((head, i))
            about = wrap(param.get('description', '') or '-', width - 4)
            if not focused and len(about) > 2:
                about = about[:1] + [about[1][:width - 5] + g['more']]
            out.extend(([('    ' + s, curses.A_DIM)], i) for s in about)
        return out

    def value_text(self, package, command, param, values):
        """A field's value as the form shows it (value_text)."""
        found = self.detected.get((package['package'], command['name'], param['name']))
        return value_text(param, values.get(param['name']), is_set(param, values), found, self.g['dash'])

    def draw_detail(self, package, command, x, width, height):
        """Name and summary, the param form (scrolled to the focus), its notes; once opened, its details."""
        header = [([(command['name'], curses.A_BOLD)], None)] + \
            [([(s, 0)], None) for s in wrap(command['summary'], width)] + [([('', 0)], None)]
        notes = [([(s, 0)], None) for s in wrap(command['notes'], width)]
        opened = self.focus == 'params' or self.opened is command
        technical = ' '.join(filter(None, (command.get('details'), command['ros'] and 'ros: ' + command['ros'])))
        if technical and opened:              # the technical part only once the entry is opened
            notes += [([('', 0)], None)] + [([(s, self.style('dim'))], None) for s in wrap(technical, width)]
        if stacks_note(command):
            notes = [([(s, self.style('dim'))], None) for s in wrap(stacks_note(command), width)] + notes
        if notes:
            notes = [([('', 0)], None)] + notes
        if self.examples or not command['params']:
            examples = command['examples'] or ['(no examples)' if self.examples else '(no parameters: r runs it)']
            body = [([(s, self.style('dim'))], None) for e in examples for s in textwrap.wrap(
                ('$ ' + e) if command['examples'] else e, max(width, 10), subsequent_indent='  ',
                break_on_hyphens=False)]
        else:
            body = self.fields(package, command, width)
        # In the form the fields come first (the notes get what is left); else the notes stay whole.
        room = max(height - len(header) - (0 if self.focus == 'params' else len(notes)), min(len(body), 4))
        start = 0
        if self.focus == 'params' and len(body) > room:
            mine = [i for i, (_, f) in enumerate(body) if f == self.param]
            start = max(0, min(mine[0] - 1, len(body) - room)) if mine[-1] - (mine[0] - 1) < room else mine[0]
        lines = header + body[start:start + room] + notes
        for y, (segments, _) in enumerate(lines[:height], 1):
            cx = x
            for segment, attr in segments:
                self.put(y, cx, segment, attr, x + width - cx)
                cx += len(segment)

    def ask(self, prompt, initial=''):
        """Read one line on the status row; None when cancelled with Esc."""
        h, w = self.screen.getmaxyx()
        text = list(initial)
        curses.curs_set(1)
        while True:
            self.status = prompt + ''.join(text)
            self.draw()
            self.screen.move(h - 3, min(2 + len(self.status), w - 2))
            key = self.screen.get_wch()
            if key in ('\n', '\r', curses.KEY_ENTER):
                break
            if key == '\x1b':
                text = None
                break
            if key in (curses.KEY_BACKSPACE, '\x7f', '\b'):
                text = text[:-1]
            elif isinstance(key, str) and key.isprintable():
                text.append(key)
        curses.curs_set(0)
        self.status = ''
        return None if text is None else ''.join(text)

    def detect(self, package, command):
        """Run each auto field's choices_from once: its first line is what auto finds."""
        for param in command['params']:
            key = (package['package'], command['name'], param['name'])
            if shown(param.get('default')) == 'auto' and param.get('choices_from') and key not in self.detected:
                self.status = f'detecting {param["name"]} ...'
                self.draw()
                self.screen.refresh()
                found = run_choices(param['choices_from'], timeout=10)
                self.detected[key] = found[0][0] if found else ''
        self.status = ''

    def choose(self, package, command, param, values):
        """The picker of a choice field: the unset row (auto, default or not set) first unless the field is
        required, then each choice with its description; the current one marked."""
        if param.get('choices_from'):
            self.status = f'listing {param["name"]} ...'
            self.draw()
            self.screen.refresh()
            choices = run_choices(param['choices_from'])
            self.status = ''
            if not choices and not param.get('choices'):
                self.status = f'{param["choices_from"]}: no choices; Enter on a text field types one'
                return
        else:
            choices = [(str(c), 'default' if shown(c) == shown(param.get('default')) else '')
                       for c in param['choices']]
        rows = list(choices)
        default = shown(param.get('default'), param) if 'default' in param else None
        if not is_required(param) and default not in [v for v, _ in rows]:     # else that row is the default
            found = self.detected.get((package['package'], command['name'], param['name']))
            rows.insert(0, (placeholder(param, found), 'leave it out of the command line'))
        now = shown(values[param['name']], param) if is_set(param, values) else default
        current = next((i for i, (v, _) in enumerate(rows) if v == now), None)
        unset = rows[0][1] == 'leave it out of the command line'
        if current is None and unset and not is_set(param, values):
            current = 0
        h, w = self.screen.getmaxyx()
        x = min(34, w // 3) + 3                                  # the right pane, as drawn
        index = menu(self.screen, 1, x, min(h - 6, len(rows) + 3), w - x - 1,
                     f'Choose {param["name"]} for {command["name"]}', rows, current or 0, self.style('accent'),
                     current, utf=self.g is UNICODE, rule=True)
        if index is None:
            return
        if unset and index == 0:
            values.pop(param['name'], None)
        else:
            values[param['name']] = rows[index][0]

    def edit(self, package, command):
        param, values = command['params'][self.param], self.values_for(package, command)
        current = values.get(param['name'], param.get('default'))
        if param['form'] == 'switch' or param['type'] == 'bool':
            values[param['name']] = shown(current).lower() not in TRUE
        elif param.get('choices') or param.get('choices_from'):
            self.choose(package, command, param, values)
        else:
            entered = self.ask(f'{param["name"]} = ', shown(values.get(param['name'])))
            if entered is None:
                return
            try:
                build(command, dict(values, **{param['name']: entered}))
            except ValueError as error:
                self.status = f'{error}; not set.'  # e.g. "--only: unbalanced quote; not set."
                return
            values[param['name']] = entered

    def run(self):
        """Event loop; return the confirmed command line, or None."""
        while True:
            self.draw()
            package, command = self.selected(self.rows())
            key = self.screen.get_wch()
            self.status = ''
            if self.keys or key == '?':       # ? shows every key; any key closes the list
                self.keys = not self.keys
                continue
            if key == '\x1b' and self.focus == 'list' and self.word:
                self.word = ''  # Esc clears the filter first, then quits
                continue
            if key in ('q', '\x1b'):
                if self.focus == 'params':
                    self.focus = 'list'
                    continue
                return None
            if command is None:
                if key == '/':
                    self.word = self.ask('/', self.word) or ''
                continue
            params = command['params']
            if key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
                self.cmd_top += 1 if key == curses.KEY_NPAGE else -1  # draw() clamps it
            elif key in (curses.KEY_UP, 'k'):
                if self.focus == 'list':
                    self.row -= 1
                else:
                    self.param = max(0, self.param - 1)
            elif key in (curses.KEY_DOWN, 'j'):
                if self.focus == 'list':
                    self.row += 1
                else:
                    self.param = min(len(params) - 1, self.param + 1)
            elif self.focus == 'list' and key in (curses.KEY_RIGHT, 'l', '\n', '\r', curses.KEY_ENTER, '\t'):
                if params:
                    self.focus, self.param, self.examples = 'params', 0, False
                    self.detect(package, command)
                else:
                    self.opened, self.status = command, 'No parameters: r runs it.'
            elif self.focus == 'params' and key == '\t':
                self.param = (self.param + 1) % len(params)
            elif self.focus == 'params' and key == curses.KEY_BTAB:
                self.param = (self.param - 1) % len(params)
            elif self.focus == 'params' and key in (curses.KEY_LEFT, 'h'):
                self.focus = 'list'
            elif self.focus == 'params' and key in ('\n', '\r', curses.KEY_ENTER, ' '):
                self.edit(package, command)
            elif self.focus == 'params' and key in (curses.KEY_DC, curses.KEY_BACKSPACE, '\x7f', 'd'):
                self.values_for(package, command).pop(params[self.param]['name'], None)
            elif key == '/':
                entered = self.ask('/', self.word)
                if entered is not None:
                    self.word, self.row, self.focus = entered, 0, 'list'
            elif key == 'x':
                self.examples, self.focus = not self.examples, 'list'
            elif key == 'r':
                values = self.values_for(package, command)
                if missing(command, values):
                    self.status = 'Fill in first: ' + ', '.join(missing(command, values))
                    continue
                self.status = 'Run this command? [y/N] '
                self.draw()
                if self.screen.get_wch() in ('y', 'Y'):
                    return invocation(command, values)
                self.status = 'Not run.'


def execute(line):
    print(f'$ {line}', flush=True)
    out = os.environ.get('ASTRO_HELP_OUT')
    if out:
        Path(out).write_text(line + '\n')
        return 0
    return subprocess.call(['bash', '-ic', line])


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] in (['-h'], ['--help']):
        print(__doc__.split('\n\n')[1])
        return 0
    packages, errors = discover()
    for error in errors:
        print(error, file=sys.stderr)
    levels = LEVELS if '--all' in argv else ('core',)
    argv = [a for a in argv if a != '--all']
    if argv[:1] == ['--list'] or argv or not (sys.stdin.isatty() and sys.stdout.isatty()):
        words = [a for a in argv if a != '--list']
        sys.stdout.write(text(packages, ' '.join(words) or None,
                              color=sys.stdout.isatty() and 'NO_COLOR' not in os.environ, levels=levels))
        if levels == ('core',) and not words:
            hidden = sum(c['level'] != 'core' for p in packages for c in p['commands'])
            if hidden:
                print(f'(+{hidden} setup commands: astro help --all)')
        return 0
    locale.setlocale(locale.LC_ALL, '')
    os.environ.setdefault('ESCDELAY', '25')
    line = curses.wrapper(lambda screen: Browser(screen, packages, levels).run())
    return execute(line) if line else 0


if __name__ == '__main__':
    sys.exit(main())
