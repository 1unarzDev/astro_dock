#!/usr/bin/env python3
"""Browse, fill in and run the commands that workspace packages describe in help.yaml.

    astro-help                 interactive browser (a terminal is required)
    astro-help --list [WORD]   plain text: every command, or full details of matching ones

help.yaml schema (unknown keys are ignored):

    package: NAME               # the group shown; the first file found per package wins
    summary: one line
    commands:
      - name: boat_trial check  # display name
        run: ros2 run pkg boat_trial check   # command prefix (default: name)
        summary: one line
        notes: optional text (safety, which host)
        params:
          - name: --window      # "-x": option ("--x value", a bare switch when type bool);
                                # with "ros2 launch": "x:=value"; otherwise positional
            type: str           # str | int | float | bool | path | list (space-separated)
            default: 5.0
            choices: [a, b]
            description: one line
            required: false     # positional params default to required
            form: option        # positional | option | switch | launch, overrides the inference
        examples: [full command lines]

Discovery order: astro_dock's own astro-help.yaml beside this script, then the workspace
sources ($ROS_WS/src/*/help.yaml and src/*/*/help.yaml), then share/*/help.yaml under each
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
TRUE = ('true', 'on', 'yes', '1')


# ---- data -------------------------------------------------------------------------------

def candidates(ws=None, prefixes=None):
    """Yield help.yaml paths in priority order."""
    yield OWN
    src = Path(ws or os.environ.get('ROS_WS', Path.home() / 'roboboat_ws')) / 'src'
    for pattern in ('*/help.yaml', '*/*/help.yaml'):
        yield from sorted(src.glob(pattern))
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
        commands.append(dict(command, name=str(command['name']), run=str(command.get('run') or command['name']),
                             summary=str(command.get('summary', '')), notes=str(command.get('notes') or ''),
                             params=params, examples=[str(e) for e in command.get('examples') or []]))
    return {'package': data['package'], 'summary': str(data.get('summary', '')),
            'commands': commands, 'path': str(path)}


def discover(paths=None):
    """Return (packages, errors); the first file per package name wins."""
    packages, errors, seen = [], [], set()
    for path in candidates() if paths is None else paths:
        try:
            package = parse(path)
        except (OSError, ValueError, yaml.YAMLError) as error:
            errors.append(f'help: {path}: {str(error).splitlines()[0]}')
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
            if shown(value).lower() in TRUE:
                words.append(name)
            continue
        quoted = (' '.join(map(shlex.quote, shlex.split(str(value)))) if param['type'] == 'list'
                  else shlex.quote(shown(value, param)))
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


# ---- plain text -------------------------------------------------------------------------

def text(packages, word=None, color=False):
    """Every command with its summary, or full details of the commands matching word."""
    def paint(code, s):
        return f'\033[{code}m{s}\033[0m' if color else s
    out = []
    for package in packages:
        commands = [c for c in package['commands'] if not word or matches(package, c, word)]
        if not commands:
            continue
        out.append(paint('1;36', package['package']) + paint('2', f' · {package["summary"]}'))
        width = max(len(c['name']) for c in commands)
        for command in commands:
            out.append(f'  {command["name"]:<{width}}  {command["summary"]}')
            if not word:
                continue
            out.append(paint('2', f'    $ {build(command, {})}'))
            if command['notes']:
                out.extend(paint('33', '    ' + line) for line in wrap(command['notes'], 88))
            for param in command['params']:
                default = f' (default {shown(param["default"])})' if 'default' in param else ''
                choices = f' [{"|".join(map(str, param["choices"]))}]' if param.get('choices') else ''
                out.append(f'    {param["name"]:<22} {param["type"]:<6} '
                           f'{param.get("description", "")}{choices}{paint("2", default)}')
            out.extend(paint('2', f'    e.g. {example}') for example in command['examples'])
        out.append('')
    return '\n'.join(out).rstrip() + '\n' if out else f'No command matches {word!r}.\n'


# ---- browser ----------------------------------------------------------------------------

UNICODE = dict(tl='╭', tr='╮', bl='╰', br='╯', h='─', v='│', lt='├', rt='┤', bt='┴', tt='┬',
               mark='▸', sep=' · ', move='↑↓', open='→/⏎', enter='⏎', back='←')
ASCII = dict(tl='+', tr='+', bl='+', br='+', h='-', v='|', lt='+', rt='+', bt='+', tt='+',
             mark='>', sep=' - ', move='j/k', open='l/enter', enter='enter', back='h')
HINTS = {'list': [('move', 'move'), ('open', 'params'), ('/', 'filter'), ('x', 'examples'),
                  ('r', 'run'), ('q', 'quit')],
         'params': [('move', 'move'), ('enter', 'edit'), ('del', 'default'), ('back', 'back'),
                    ('r', 'run'), ('q', 'quit')]}


class Browser:
    def __init__(self, screen, packages):
        self.screen, self.packages = screen, packages
        self.values, self.word, self.status, self.focus = {}, '', '', 'list'
        self.row = self.param = self.top = 0
        self.examples = False
        # Python coerces a C locale to UTF-8 itself, so ask the terminal's locale variables.
        lang = next((os.environ[v] for v in ('LC_ALL', 'LC_CTYPE', 'LANG') if os.environ.get(v)), '')
        self.g = UNICODE if 'utf' in lang.lower().replace('-', '') and \
            os.environ.get('TERM') != 'linux' else ASCII
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
        out = []
        for package in self.packages:
            commands = [c for c in package['commands'] if not self.word or matches(package, c, self.word)]
            if commands:
                out.append((package, None))
                out.extend((package, c) for c in commands)
        return out

    def selected(self, rows):
        commands = [r for r in rows if r[1]]
        if not commands:
            return None, None
        self.row = max(0, min(self.row, len(commands) - 1))
        return commands[self.row]

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
        # The built line is shown whole (wrapped), never truncated: it is what runs.
        line = build(command, self.values_for(package, command)) if command else ''
        shown_line = [('  ' if i else '$ ') + line[i:i + w - 6] for i in range(0, len(line), w - 6)][:h - 10]
        left, bottom = min(38, w // 2 - 2), h - 4 - max(len(shown_line), 1)
        count, n = sum(len(p['commands']) for p in self.packages), len(self.packages)
        title = f'{g["h"]} help{g["sep"]}{n} package{"s" * (n != 1)}{g["sep"]}{count} commands'
        title += f'{g["sep"]}/{self.word} ' if self.word else ' '
        self.put(0, 0, g['tl'] + title + g['h'] * (w - len(title) - 2) + g['tr'], self.style('accent'))
        for y in range(1, h - 2):
            self.put(y, 0, g['v'], self.style('accent'))
            self.put(y, w - 1, g['v'], self.style('accent'))
            if y < bottom:
                self.put(y, left + 1, g['v'], self.style('dim'))
        self.put(bottom, 0, g['lt'] + g['h'] * left + g['bt'] + g['h'] * (w - left - 3) + g['rt'],
                 self.style('accent'))
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
            self.put(h - 1, x, key, self.style('accent'))
            self.put(h - 1, x + len(key) + 1, label, self.style('dim'))
            x += len(key) + len(label) + 3

    def draw_list(self, rows, command, width, height):
        index = next((i for i, r in enumerate(rows) if r[1] is command), 0)
        self.top = max(min(self.top, index), index - height + 1, 0)
        for y, (package, cmd) in enumerate(rows[self.top:self.top + height], 1):
            if cmd is None:
                self.put(y, 2, package['package'], self.style('accent') | curses.A_BOLD, width - 1)
            elif cmd is command:
                attr = curses.A_BOLD | (self.style('accent') if self.focus == 'list' else 0)
                self.put(y, 2, f'{self.g["mark"]} {cmd["name"]}', attr, width - 1)
            else:
                self.put(y, 4, cmd['name'], 0, width - 3)

    def draw_detail(self, package, command, x, width, height):
        """Name and summary, the parameter table (scrolled to the focus), its description, notes."""
        header = [(command['name'], curses.A_BOLD)] + [(s, 0) for s in wrap(command['summary'], width)]
        header.append(('', 0))
        notes = [(s, self.style('note')) for s in wrap(command['notes'], width)]
        values, body, footer = self.values_for(package, command), [], []
        if self.examples or not command['params']:
            examples = command['examples'] or ['(no examples)' if self.examples else '(no parameters)']
            body = [(s, self.style('dim')) for e in examples for s in textwrap.wrap(
                ('$ ' + e) if command['examples'] else e, max(width, 10), subsequent_indent='  ',
                break_on_hyphens=False)]
        else:
            params = command['params']
            name_w = min(max(len(p['name']) for p in params), width // 2)
            value_w = min(max([14] + [len(shown(values.get(p['name'], p.get('default')))) for p in params]),
                          width - name_w - 12)
            for i, param in enumerate(params):
                value = shown(values.get(param['name'], param.get('default')), param)
                kind = 'choice' if param.get('choices') else 'switch' if param['form'] == 'switch' \
                    else param['type']
                mark = self.g['mark'] if self.focus == 'params' and i == self.param else ' '
                body.append(([(f'{mark} {param["name"][:name_w]:<{name_w}} ', curses.A_BOLD if mark != ' ' else 0),
                              (f'{(value or "-")[:value_w]:<{value_w}} ',
                               self.style('value') if param['name'] in values else self.style('dim')),
                              (kind, self.style('kind'))], None))
            if self.focus == 'params':
                focused = params[self.param]
                about = focused.get('description', '')
                if focused.get('choices'):
                    about += f' [{"|".join(map(str, focused["choices"]))}]'
                if 'default' in focused:
                    about += f' (default {shown(focused["default"], focused)})'
                footer = [('', 0)] + [(s, self.style('dim')) for s in wrap(about, width)]
        if notes:
            footer += [('', 0)] + notes
        room = max(height - len(header) - len(footer), min(len(body), 3))
        start = max(0, min(self.param - room // 2, len(body) - room)) if self.focus == 'params' else 0
        lines = header + body[start:start + room] + footer
        for y, (line, attr) in enumerate(lines[:height], 1):
            if attr is None:  # a parameter row: coloured segments
                cx = x
                for segment, seg_attr in line:
                    self.put(y, cx, segment, seg_attr, x + width - cx)
                    cx += len(segment)
            else:
                self.put(y, x, line, attr, width)

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

    def edit(self, package, command):
        param, values = command['params'][self.param], self.values_for(package, command)
        current = values.get(param['name'], param.get('default'))
        if param['form'] == 'switch' or param['type'] == 'bool':
            values[param['name']] = shown(current).lower() not in TRUE
        elif param.get('choices'):
            choices = [str(c) for c in param['choices']]
            values[param['name']] = choices[(choices.index(shown(current)) + 1) % len(choices)
                                            if shown(current) in choices else 0]
        else:
            entered = self.ask(f'{param["name"]} = ', shown(current))
            if entered is not None:
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
            if key in ('q', '\x1b') and not (key == '\x1b' and self.focus == 'params'):
                return None
            if command is None:
                if key == '/':
                    self.word = self.ask('/', self.word) or ''
                continue
            params = command['params']
            if key in (curses.KEY_UP, 'k'):
                if self.focus == 'list':
                    self.row -= 1
                else:
                    self.param = max(0, self.param - 1)
            elif key in (curses.KEY_DOWN, 'j'):
                if self.focus == 'list':
                    self.row += 1
                else:
                    self.param = min(len(params) - 1, self.param + 1)
            elif self.focus == 'list' and key in (curses.KEY_RIGHT, 'l', '\n', '\r', curses.KEY_ENTER):
                if params:
                    self.focus, self.param, self.examples = 'params', 0, False
            elif self.focus == 'params' and key in (curses.KEY_LEFT, 'h', '\x1b'):
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
                    return build(command, values)
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
    if argv[:1] == ['--list'] or not (sys.stdin.isatty() and sys.stdout.isatty()):
        words = [a for a in argv if a != '--list']
        sys.stdout.write(text(packages, ' '.join(words) or None,
                              color=sys.stdout.isatty() and 'NO_COLOR' not in os.environ))
        return 0
    if argv:
        print('Usage: astro-help [--list [WORD]]', file=sys.stderr)
        return 2
    locale.setlocale(locale.LC_ALL, '')
    os.environ.setdefault('ESCDELAY', '25')
    line = curses.wrapper(lambda screen: Browser(screen, packages).run())
    return execute(line) if line else 0


if __name__ == '__main__':
    sys.exit(main())
