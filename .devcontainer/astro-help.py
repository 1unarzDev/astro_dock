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
        summary: one line
        level: core             # core (default) | setup: setup commands show only with --all
        host: ROLE|HOST         # optional: where it runs; `astro NAME` from elsewhere forwards it there
                                # (astro on HOST RUN ..., printing "(on HOST)"); --here runs it here
        ros: optional; the plain ROS 2 an astro verb composes (shown beside it)
        needs: [nav, drive+controller]   # optional: stacks (profile astro.stacks) astro starts first when
                                # down, their needs too; NAME+OPTION starts it with that `with` option
        conflicts: [drive]      # optional: stacks astro stops first when up; {drive: why} says why
                                # (--here or ASTRO_NO_AUTOSTART=1 skips both)
        notes: optional text (safety, which host)
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
            description: one line   # core entries: at least 8 words and, unless a bool or choice, e.g. VALUE
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
import locale
import os
import shlex
import subprocess
import sys
import textwrap
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


def pick(title, choices, stream=None, current=None):
    """The chosen value from [(value, about)]: an arrow-key list on a terminal (a numbered one without
    curses), the CURRENT value marked *; None when cancelled. Without a terminal it prints the choices
    and returns None."""
    stream = stream or sys.stderr
    if not choices:
        print(f'{title} nothing to choose from', file=stream)
        return None
    width = max(len(v) for v, _ in choices)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(f'{title} give one of', file=stream)
        for value, about in choices:
            print(f'  {"*" if value == current else " "} {value:<{width}}  {about}'.rstrip())
        return None
    try:
        locale.setlocale(locale.LC_ALL, '')
        os.environ.setdefault('ESCDELAY', '25')
        return curses.wrapper(lambda screen: Picker(screen, title, choices, current).run())
    except curses.error:
        pass
    print(title, file=stream)
    for i, (value, about) in enumerate(choices, 1):
        print(f'{i:>3}) {"*" if value == current else " "} {value:<{width}}  {about}'.rstrip(), file=stream)
    try:
        entered = input(f'number or name [1-{len(choices)}, empty cancels]: ').strip()
    except EOFError:
        return None
    if entered.isdigit() and 1 <= int(entered) <= len(choices):
        return choices[int(entered) - 1][0]
    return next((v for v, _ in choices if v == entered), None)


MENU_KEYS = {True: '↑↓ move  Enter choose  Esc/q cancel  * current',
             False: 'up/down move  Enter choose  Esc/q cancel  * current'}


def menu(screen, y, x, h, w, title, choices, row=0, attr=0, current=None, utf=True):
    """Draw and run an arrow-key list in the box (y, x, h, w): the title, one row per choice with its
    description (the CURRENT index marked *, the cursor row > and reversed), the keys; the chosen index or
    None (Esc, q)."""
    width = max(len(v) for v, _ in choices)
    while True:
        room = max(h - 2, 1)
        top = max(0, min(row - room // 2, len(choices) - room))
        for i in range(h):
            try:
                screen.addstr(y + i, x, ' ' * (w - 1))
            except curses.error:
                pass
        try:
            screen.addstr(y, x, f' {title} '[:w - 1], curses.A_BOLD | attr)
            for i, (value, about) in enumerate(choices[top:top + room], 1):
                index = top + i - 1
                line = f'{">" if index == row else " "} {"*" if index == current else " "} {value:<{width}}  {about}'
                screen.addstr(y + i, x, line[:w - 1].ljust(w - 1) if index == row else line[:w - 1],
                              curses.A_REVERSE if index == row else 0)
            more = f'  ({row + 1}/{len(choices)})' if len(choices) > room else ''
            screen.addstr(y + h - 1, x, f' {MENU_KEYS[utf]}{more}'[:w - 1], curses.A_DIM)
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
        curses.curs_set(0)

    def run(self):
        h, w = self.screen.getmaxyx()
        self.screen.erase()
        index = menu(self.screen, 0, 0, min(h, len(self.choices) + 2), w, self.title, self.choices,
                     self.current or 0, current=self.current, utf=utf8())
        return None if index is None else self.choices[index][0]


# ---- plain text -------------------------------------------------------------------------

def details(command, paint=lambda code, s: s):
    """The full description of one command: its command line(s), ros equivalent, notes, params, examples."""
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
        out.extend(paint('33', '    ' + line) for line in wrap(command['notes'], 88))
    for param in command['params']:
        default = f' (default {shown(param["default"])})' if 'default' in param else ''
        choices = f' [{"|".join(map(str, param["choices"]))}]' if param.get('choices') else ''
        choices += f' [from: {param["choices_from"]}]' if param.get('choices_from') else ''
        out.append(f'    {param["name"]:<22} {param["type"]:<6} '
                   f'{param.get("description", "")}{choices}{paint("2", default)}')
    out.extend(paint('2', f'    e.g. {example}') for example in command['examples'])
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
        return f'\033[{code}m{s}\033[0m' if color else s
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
        heads = {c['name']: f'{c["name"]} {synopsis(c)}'.rstrip() for c in ordered}
        width = min(max(len(h) for h in heads.values()), 40)
        for command in ordered:
            head = heads[command['name']]
            if len(head) > width:                                 # a long usage: the summary goes below it
                out.extend([f'  {head}', f'  {"":<{width}}  {command["summary"]}'])
            else:
                out.append(f'  {head:<{width}}  {command["summary"]}')
            if word or exact:
                out.extend(details(command, paint))
        out.append('')
    return '\n'.join(out).rstrip() + '\n' if out else f'No command matches {word or exact[1]!r}.\n'


# ---- browser ----------------------------------------------------------------------------

UNICODE = dict(tl='╭', tr='╮', bl='╰', br='╯', h='─', v='│', lt='├', rt='┤', bt='┴', tt='┬',
               sep=' · ', move='↑↓', enter='Enter', left='◂', right='▸', more='…')
ASCII = dict(tl='+', tr='+', bl='+', br='+', h='-', v='|', lt='+', rt='+', bt='+', tt='+',
             sep=' - ', move='up/down', enter='Enter', left='<', right='>', more='~')
HINTS = {'list': [('move', 'move'), ('enter', 'fill in'), ('/', 'filter'), ('x', 'examples'),
                  ('r', 'run'), ('q', 'quit')],
         'params': [('move', 'move'), ('enter', 'edit/pick'), ('Tab', 'next'), ('Del', 'default'),
                    ('r', 'run'), ('q', 'back')]}


def invocation(command, values):
    """The line the browser runs: `astro NAME ...` for a command with a host (so it is forwarded there),
    else its run line."""
    line = build(command, values)
    return f'astro {command["name"]}{line[len(command["run"]):]}' if command['host'] else line


def is_set(param, values):
    """True when the form holds a value for PARAM that differs from its default (so it is on the line)."""
    return param['name'] in values and shown(values[param['name']], param) not in ('', shown(param.get('default'), param))


def placeholder(param, detected=None):
    """What an unset field shows: (auto: FOUND), (default X), (required) or (not set)."""
    default = param.get('default')
    if shown(default) == 'auto' and 'auto' not in [str(c) for c in param.get('choices') or []]:
        return f'(auto: {detected})' if detected else '(auto)'
    if 'default' in param:
        return f'(default {onoff(default) if kind(param) == "switch" else shown(default, param)})'
    return '(required)' if is_required(param) else '(not set)'


def onoff(value):
    return 'on' if shown(value).lower() in TRUE else 'off'


class Browser:
    def __init__(self, screen, packages, levels=('core',)):
        self.screen, self.packages, self.levels = screen, packages, levels
        self.values, self.word, self.status, self.focus = {}, '', '', 'list'
        self.row = self.param = self.top = self.cmd_top = 0
        self.examples = False
        self.detected = {}                    # (package, command, param) -> what auto finds ('' none)
        self.g = UNICODE if utf8() else ASCII
        self.color = {}
        if curses.has_colors() and 'NO_COLOR' not in os.environ:
            curses.use_default_colors()
            for i, (name, fg) in enumerate((('accent', curses.COLOR_CYAN), ('note', curses.COLOR_YELLOW),
                                            ('value', curses.COLOR_GREEN), ('kind', curses.COLOR_MAGENTA)), 1):
                curses.init_pair(i, fg, -1)
                self.color[name] = curses.color_pair(i)
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
        """[(package, command or None, depth, label)]: each package, then its commands grouped by noun."""
        out = []
        for package in self.packages:
            commands = [c for c in package['commands']
                        if (matches(package, c, self.word) if self.word else c['level'] in self.levels)]
            if commands:
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
        count, n = sum(len(p['commands']) for p in self.packages), len(self.packages)
        title = f'{g["h"]} help{g["sep"]}{n} package{"s" * (n != 1)}{g["sep"]}{count} commands'
        title += f'{g["sep"]}/{self.word} ' if self.word else ' '
        self.put(0, 0, g['tl'] + title + g['h'] * (w - len(title) - 2) + g['tr'], self.style('accent'))
        for y in range(1, h - 2):
            self.put(y, 0, g['v'], self.style('accent'))
            self.put(y, w - 1, g['v'], self.style('accent'))
            if y < bottom:
                self.put(y, left + 1, g['v'], self.style('dim'))
        runs = ' runs ' if command else ''
        self.put(bottom, 0, g['lt'] + g['h'] * left + g['bt'] + g['h'] * (w - left - 3) + g['rt'],
                 self.style('accent'))
        self.put(bottom, 2, runs, self.style('bold'))
        if len(parts) > room:
            where = f' lines {self.cmd_top + 1}-{self.cmd_top + room} of {len(parts)}, PgUp/PgDn '
            self.put(bottom, w - len(where) - 2, where, self.style('note'))
        self.put(h - 2, 0, g['bl'] + g['h'] * (w - 2) + g['br'], self.style('accent'))
        self.draw_list(rows, command, left, bottom - 1)
        if command:
            self.draw_detail(package, command, left + 3, w - left - 5, bottom - 1)
            for y, part in enumerate(shown_line, bottom + 1):
                self.put(y, 2, part, self.style('bold'), w - 4)
        self.put(h - 3, 2, self.status, self.style('note'), w - 4)
        x = 1
        for key, label in HINTS[self.focus]:
            key = g.get(key, key)
            self.put(h - 1, x, key, self.style('accent') | curses.A_BOLD)
            self.put(h - 1, x + len(key) + 1, label)
            x += len(key) + len(label) + 3

    def draw_list(self, rows, command, width, height):
        index = next((i for i, r in enumerate(rows) if r[1] is command), 0)
        self.top = max(min(self.top, index), index - height + 1, 0)
        for y, (package, cmd, depth, label) in enumerate(rows[self.top:self.top + height], 1):
            x = 2 + 2 * (depth + 1)
            if depth < 0:
                self.put(y, 2, label, self.style('accent') | curses.A_BOLD, width - 1)
            elif cmd is None:                                   # a noun with no entry of its own
                self.put(y, x - 2, f'  {label}', self.style('dim'), width - x + 1)
            elif cmd is command:
                attr = curses.A_REVERSE | (curses.A_BOLD if self.focus == 'list' else 0)
                self.put(y, x - 2, f'> {label}'.ljust(width - x + 1), attr, width - x + 1)
            else:
                self.put(y, x - 2, f'  {label}', 0, width - x + 1)

    def fields(self, package, command, width):
        """[(line segments, field index)] of the param form: per field its name, value, type, then its
        description (the focused field's whole, the others' first line)."""
        g, values, params = self.g, self.values_for(package, command), command['params']
        name_w = min(max(len(p['name']) for p in params), width // 3)
        type_w = max(len(kind(p)) for p in params)
        room = max(width - name_w - type_w - 7, 8)
        texts = [self.value_text(package, command, p, values) for p in params]
        value_w = min(max(len(t) for t in texts), room)
        out = []
        for i, (param, text) in enumerate(zip(params, texts)):
            focused = self.focus == 'params' and i == self.param
            if len(text) > value_w:
                text = text[:value_w - 1] + g['more']
            vstyle = self.style('value') if is_set(param, values) else self.style('dim')
            head = [(f'{">" if focused else " "} {param["name"][:name_w]:<{name_w}}  ', curses.A_BOLD),
                    (f'{text:<{value_w}}  ', vstyle), (f'{kind(param):<{type_w}}', self.style('kind'))]
            if focused:                                           # unmistakable: > and reverse video
                head = [(t, a | curses.A_REVERSE) for t, a in head]
            out.append((head, i))
            about = param.get('description', '') or '-'
            if focused:
                lines = wrap(about, width - 4)
            else:
                lines = wrap(about, width - 4)[:1]
                if len(wrap(about, width - 4)) > 1:
                    lines = [lines[0][:width - 5] + g['more']]
            out.extend(([('    ' + s, curses.A_NORMAL if focused else self.style('dim'))], i) for s in lines)
        return out

    def value_text(self, package, command, param, values):
        """A field's value as the form shows it: the value set, else (auto: X), (default X), (required) or
        (not set); a switch as [x] on / [ ] off, a choice between arrows."""
        found = self.detected.get((package['package'], command['name'], param['name']))
        text = shown(values[param['name']], param) if is_set(param, values) else placeholder(param, found)
        if kind(param) == 'switch':
            on = onoff(values.get(param['name'], param.get('default')))
            return f'[{"x" if on == "on" else " "}] {on}' + ('' if is_set(param, values) else ' (default)')
        if kind(param) == 'choice':
            return f'{self.g["left"]} {text} {self.g["right"]}'
        return text

    def draw_detail(self, package, command, x, width, height):
        """Name and summary, the param form (scrolled to the focus), its notes."""
        header = [([(command['name'], curses.A_BOLD)], None)] + \
            [([(s, 0)], None) for s in wrap(command['summary'], width)] + [([('', 0)], None)]
        notes = [([(s, self.style('note'))], None) for s in wrap(command['notes'], width)]
        if stacks_note(command):
            notes = [([(s, self.style('dim'))], None) for s in wrap(stacks_note(command), width)] + notes
        if command['ros']:
            notes = [([(s, self.style('dim'))], None) for s in wrap('ros: ' + command['ros'], width)] + notes
        if notes:
            notes = [([('', 0)], None)] + notes
        if self.examples or not command['params']:
            examples = command['examples'] or ['(no examples)' if self.examples else '(no parameters: r runs it)']
            body = [([(s, self.style('dim'))], None) for e in examples for s in textwrap.wrap(
                ('$ ' + e) if command['examples'] else e, max(width, 10), subsequent_indent='  ',
                break_on_hyphens=False)]
        else:
            body = self.fields(package, command, width)
        room = max(height - len(header) - len(notes), min(len(body), 4))
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
        x = max(w // 3, 2)
        index = menu(self.screen, 2, x, min(h - 6, len(rows) + 2), w - x - 2,
                     f'{param["name"]}: pick one', rows, current or 0, self.style('accent'), current,
                     utf=self.g is UNICODE)
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
                    self.status = 'No parameters: r runs it.'
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
