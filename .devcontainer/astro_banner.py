"""astro dock shell banner: space art beside workspace telemetry, sized to the terminal.

astro-banner.sh runs this once per interactive terminal; the rows come from
astro_banner_stats.py. Stdlib plus PyYAML, Python 3.8-safe. Settings live in
config/banner.yaml, the art in astro_banner_art.txt.
"""
import os
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # python3 -I leaves the script's directory off the path
from astro_banner_stats import collect, read  # noqa: E402

CONFIGS = (HERE.parent / 'config' / 'banner.yaml', Path.home() / '.config' / 'mhseals' / 'banner.yaml')
# Star glyphs by brightness: near, mid and distant.
STARS = {'✦': 'star', '⋆': 'faint', '+': 'faint', '·': 'dim', '.': 'dim'}
# 16-colour code and soft truecolour per tone (cx: 16 ANSI colours, cyan accent).
TONES = {'accent': (36, (125, 196, 228)), 'ok': (32, (166, 218, 149)), 'warn': (33, (238, 212, 159)),
         'bad': (31, (237, 135, 150)), 'dim': (90, (110, 115, 141)), 'star': (37, (244, 219, 214)),
         'faint': (37, (183, 189, 222))}
# One-for-one so widths, and with them the layout, survive the ASCII fallback;
# braille shading becomes a density ramp by its dot count.
ASCII = str.maketrans('█▓▒░▄▀▟▙▛▜▁▔─━═│║╭╮╰╯╗╔╝╚·✦⋆…↑✓✗±▰▱',
                      '##%:######_~--=||++++++++.*+~^+x~#-')
ASCII.update({0x2800 + bits: ' .,:;+*#@'[bin(bits).count('1')] for bits in range(256)})


# ---------------------------------------------------------------- config

def load_config(paths=CONFIGS):
    """Merge YAML files; later files replace top-level keys. Bad files are skipped."""
    import yaml
    config = {}
    for path in paths:
        try:
            data = yaml.safe_load(Path(path).read_text())
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict):
            config.update(data)
    return config


# ---------------------------------------------------------------- art & layout
# A line is a list of (text, style); style is a tone name, an (r, g, b) or None.

def load_art(path=HERE / 'astro_banner_art.txt'):
    art, name = {}, None
    for line in read(path).splitlines():
        if line.startswith('@@ '):
            name = line[3:].strip()
            art[name] = []
        elif name:
            art[name].append(line.rstrip())
    return art


def hex_rgb(value):
    value = str(value).lstrip('#')
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def gradient(stops, t):
    t = min(max(t, 0.0), 1.0) * (len(stops) - 1)
    i = min(int(t), len(stops) - 2)
    a, b, f = stops[i], stops[i + 1], t - i
    return tuple(int(a[k] + (b[k] - a[k]) * f) for k in range(3))


def paint(lines, stops, width=None):
    """Tint art along a gentle diagonal; stars keep their own tone."""
    width = width or max([len(line) for line in lines] + [1])
    height = max(len(lines), 1)
    out = []
    for y, line in enumerate(lines):
        out.append([(ch, STARS.get(ch) or (None if ch == ' ' else
                     shade(gradient(stops, 0.8 * x / max(width - 1, 1) + 0.2 * y / height), ch)))
                    for x, ch in enumerate(line)])
    return out


def shade(rgb, ch):
    """Dim sparse braille cells so a dithered sphere reads lit limb to shadow."""
    if not 0x2800 < ord(ch) <= 0x28ff:
        return rgb
    k = 0.45 + 0.55 * bin(ord(ch) - 0x2800).count('1') / 8.0
    return tuple(int(v * k) for v in rgb)


def width_of(line):
    return sum(len(text) for text, _ in line)


def clip(line, width):
    out, used = [], 0
    for text, style in line:
        if used + len(text) > width:
            out.append((text[:max(width - used - 1, 0)] + '…', style))
            return out
        out.append((text, style))
        used += len(text)
    return out + [(' ' * (width - used), None)]


def stat_line(label, segments, width):
    line = [('%-10s' % label, 'accent')]
    for i, segment in enumerate(segments):
        if i:
            line.append((' · ', 'dim'))
        line.append(segment)
    return clip(line, width)


def box(title, rows, width):
    inner = width - 4
    top = [('╭─ ', 'dim'), (title, 'accent'), (' ' + '─' * max(width - 5 - len(title), 0) + '╮', 'dim')]
    body = [[('│ ', 'dim')] + clip(row, inner) + [(' │', 'dim')] for row in rows]
    return [top] + body + [[('╰' + '─' * (width - 2) + '╯', 'dim')]]


def fitting_layouts(layouts, cols, rows):
    return [layout for layout in layouts or []
            if cols >= layout.get('min_cols', 0) and rows >= layout.get('min_rows', 0)]


def compose(layout, art, stats, cols, rows, stops, title):
    """Lay out art, wordmark and the stats box; stats are trimmed to the height."""
    pic = paint(art.get(layout.get('art'), []), stops)
    mark = art.get(layout.get('wordmark'), [])
    mark = paint(mark, stops) + [[]] if mark else []
    beside = layout.get('stats') == 'beside' and pic
    left = max([width_of(line) for line in pic] + [0]) + 3 if beside else 0
    width = max(min(cols - left - 2, 78), 30)
    room = rows - 3 - len(mark) - 2 - (0 if beside else len(pic) + (1 if pic else 0))
    entries = [stat_line(name, segments, width - 4) for name, segments in stats][:max(room, 0)]
    right = mark + box(title, entries, width)
    if not beside:
        return ([[(' ', None)] + line for line in pic] + ([[]] if pic else []) +
                [[(' ', None)] + line for line in right])
    top = max((len(right) - len(pic)) // 2, 0)
    out = []
    for y in range(max(len(pic) + top, len(right))):
        art_line = pic[y - top] if 0 <= y - top < len(pic) else []
        pad = [(' ' * (left - 1 - width_of(art_line)), None)]
        out.append([(' ', None)] + art_line + pad + (right[y] if y < len(right) else []))
    return out


def sgr(style, depth):
    if style is None or not depth:
        return ''
    code16, rgb = TONES[style] if isinstance(style, str) else (None, style)
    if depth == 24:
        return '\x1b[38;2;%d;%d;%dm' % rgb
    if depth == 8:
        r, g, b = (int(round(v / 255.0 * 5)) for v in rgb)
        return '\x1b[38;5;%dm' % (16 + 36 * r + 6 * g + b)
    if code16 is None:  # gradient on 16 colours: magenta, blue, cyan by hue
        code16 = 35 if rgb[0] > rgb[2] * 0.85 else 34 if rgb[1] < rgb[2] * 0.85 else 36
    return '\x1b[%dm' % code16


def to_text(lines, depth, ascii_only):
    out = []
    for line in lines:
        text, last = '', ''
        for chunk, style in line:
            code = sgr(style, depth) if chunk.strip() else last
            if code != last:
                text += '\x1b[0m' + code if last else code
                last = code
            text += chunk
        text = text.rstrip(' ') + ('\x1b[0m' if last else '')
        out.append(text.translate(ASCII).encode('ascii', 'replace').decode() if ascii_only else text)
    return '\n'.join(out)


def color_depth(setting, env):
    if env.get('NO_COLOR') or setting == 'none':
        return 0
    if setting in ('truecolor', '256', '16'):
        return {'truecolor': 24, '256': 8, '16': 4}[setting]
    if env.get('COLORTERM', '').lower() in ('truecolor', '24bit'):
        return 24
    return 8 if '256' in env.get('TERM', '') else 4


def terminal_size(env):
    try:
        size = os.get_terminal_size(sys.stdout.fileno())
        cols, rows = size.columns, size.lines
    except OSError:
        cols, rows = 80, 24
    return int(env.get('COLUMNS') or cols), int(env.get('LINES') or rows)


# ---------------------------------------------------------------- main

def render(env, cols, rows, config=None, now=None):
    """The banner text for a cols x rows terminal, or '' when nothing fits."""
    config = load_config() if config is None else config
    layouts = fitting_layouts(config.get('layouts'), cols, rows)
    if not layouts:
        return ''
    now = time.time() if now is None else now
    stats = collect(env, config, Path(env.get('ROS_WS') or HERE.parent), now)
    stops = [hex_rgb(stop) for stop in config.get('gradient') or ['#c6a0f6', '#7dc4e4']]
    title = 'telemetry · %s' % time.strftime('%a %d %b %H:%M', time.localtime(now))
    art = load_art()
    for layout in layouts:  # art wider than its minimum promised falls through to the next
        lines = compose(layout, art, stats, cols, rows, stops, title)
        if max(width_of(line) for line in lines) <= cols:
            break
    else:
        return ''
    charset = config.get('charset', 'auto')
    ascii_only = charset == 'ascii' or (charset == 'auto' and 'utf' not in (sys.stdout.encoding or '').lower())
    return to_text([[]] + lines, color_depth(config.get('color', 'auto'), env), ascii_only)


def main():
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    env = dict(os.environ)
    cols, rows = terminal_size(env)
    text = render(env, cols, rows)
    if text:
        sys.stdout.write(text + '\n')
        sys.stdout.flush()


if __name__ == '__main__':
    try:
        main()
    except Exception:  # a banner never breaks a shell
        pass
