"""The one look of astro's terminal screens: the help browser, the pickers and the plain --list/--help text.

The selected row is a left marker plus bold (and the accent colour on its name when colours are on), never a
reversed full-width line; descriptions are dim; nothing else is coloured. The meaning survives without
colour (NO_COLOR, or a terminal without colours): the marker and bold carry it. The colours are the
terminal's own palette entries, so they follow its theme, light or dark.

.devcontainer/astro-help.py loads this file from the workspace; a team package's own screens mirror these
constants (they cannot import astro_dock).
"""
import os

MARK, MARK_ASCII = '›', '>'       # the selected row
CURRENT = '(current)'                  # a picker's current value
ON, OFF = '[x]', '[ ]'                 # an on/off value
SEP, SEP_ASCII = ' · ', ' - '     # between parts of a title or a key group
ACCENT = 'cyan'                        # the one accent: the selected name and titles
ANSI = {'bold': '1', 'dim': '2', 'accent': '36', 'title': '1;36'}
# Param types in plain words (help.yaml type -> what the form and the text say).
TYPES = {'str': 'text', 'int': 'whole number', 'float': 'number', 'bool': 'on/off', 'path': 'file',
         'list': 'text, several'}


def colour(stream=None, env=None):
    """True when STREAM (default stdout) is a terminal and NO_COLOR is not set."""
    import sys
    stream = stream or sys.stdout
    return bool(getattr(stream, 'isatty', lambda: False)()) and 'NO_COLOR' not in (os.environ if env is None
                                                                                  else env)


def paint(name, text, on=True):
    """TEXT in the ANSI style NAME (a key of ANSI) when ON."""
    return f'\033[{ANSI[name]}m{text}\033[0m' if on and text else text


def mark(selected, utf=True):
    """The two cells before a row: the marker and a space when selected, else two spaces."""
    return f'{MARK if utf else MARK_ASCII} ' if selected else '  '


def box(on):
    return ON if on else OFF


def type_name(kind, choices=()):
    """A param type in plain words: number, whole number, on/off, file, text; with choices one of A/B/C
    (more than four: pick from a list)."""
    choices = [str(c) for c in choices or ()]
    if choices:
        return 'one of ' + '/'.join(choices) if len(choices) <= 4 else 'pick from a list'
    return TYPES.get(str(kind), str(kind))
