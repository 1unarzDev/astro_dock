"""The test types a tests file composes: still, couple, speed, steps, nav2. Each runs alone, writes one result
file (its changes, the upstream keys it ran on, a one-line summary), logs `test start/done/abort` and prints
its layer, old -> new and the exact `astro apply` command. Nothing here names a robot: topics, axes, keys,
spaces and parameters all come from the tests file."""
import statistics

import yaml

from . import apply as applying
from . import log, results     # fit (numpy) is imported by the test types that run

TYPES = ('still', 'couple', 'speed', 'steps', 'nav2')
DEFAULT_LAYER = {'still': 'conditions', 'couple': 'decoupling', 'speed': 'speed curve',
                 'steps': 'velocity loop', 'nav2': 'nav2 limits'}


class Abort(Exception):
    pass


class Spec:
    """A tests file: topics, axes, spaces, tests, groups, and optionally `defaults:` (a file whose keys
    stand in for keys the state file does not set), `rotational:` (the rotational axis) and `gates:` (latched
    JSON states that may stop the boat: [{state, held, cause, clear}], dotted paths; a test refuses to start
    while a `held` field is set, naming its cause and how to clear it)."""

    def __init__(self, path, find=None):
        self.path = path
        data = yaml.safe_load(open(path)) or {}
        self.topics = data.get('topics') or {}
        self.axes = list(data.get('axes') or [])
        self.rotational = data.get('rotational') or (self.axes[-1] if self.axes else None)
        self.spaces = data.get('spaces') or {}
        self.tests = data.get('tests') or {}
        self.groups = data.get('groups') or {}
        self.defaults_path = data.get('defaults')
        self.lane = data.get('lane') or {}        # {state, field, want, cause, fix}: who drives during a hold
        self.nodes = list(data.get('nodes') or [])          # the nodes astro status names
        self.gates = list(data.get('gates') or ([data['gate']] if data.get('gate') else []))
        self._find = find
        for name, test in self.tests.items():
            if test.get('type') not in TYPES:
                raise ValueError(f'{path}: tests.{name}.type must be one of {", ".join(TYPES)}')
            if test.get('axis') is not None and test['axis'] not in self.axes:
                raise ValueError(f'{path}: tests.{name}.axis {test["axis"]!r} is not one of axes {self.axes}')

    def defaults(self):
        if not self.defaults_path:
            return {}
        path = self._find(self.defaults_path) if self._find else self.defaults_path
        return yaml.safe_load(open(path)) or {}

    def expand(self, name, seen=()):
        if name in self.tests:
            return [name]
        if name in self.groups:
            if name in seen:
                raise ValueError(f'group cycle at {name}')
            return [t for item in self.groups[name] for t in self.expand(item, (*seen, name))]
        raise KeyError(name)

    def order(self):
        """Every test in ladder order: the `all` group, else the file's order."""
        out = []
        for test in (self.expand('all') if 'all' in self.groups else []) + list(self.tests):
            if test not in out:
                out.append(test)
        return out

    def layer(self, name):
        test = self.tests[name]
        return test.get('layer') or DEFAULT_LAYER[test['type']]

    def space(self, name):
        if name not in self.spaces:
            raise ValueError(f'no space {name!r} (spaces: {", ".join(self.spaces)})')
        return self.spaces[name]


def fmt(value):
    if isinstance(value, float):
        return f'{value + 0.0:.4g}'
    if isinstance(value, list):
        return '[' + ', '.join(fmt(v) for v in value) + ']'
    if isinstance(value, dict):
        return '{' + ', '.join(f'{k}: {fmt(v)}' for k, v in value.items()) + '}'
    return str(value)


class Context:
    """What a run needs: the spec, the state file, the data root, the events topic, a runner factory."""

    def __init__(self, spec, state_path, data_root, events=None, sim_time=False, make_runner=None, out=print):
        self.spec, self.state_path, self.data_root = spec, state_path, data_root
        self.events, self.sim_time, self.out = events, sim_time, out
        self.make_runner = make_runner
        self.progress = 'start'
        self.software = {}            # repo -> commit, recorded in each result
        self.holds = []               # raw samples of each hold, recorded in each result

    def state(self):
        data = applying.yaml.safe_load(open(self.state_path)) if self.state_path.exists() else {}
        return data or {}

    def log(self, kind, message):
        return log.write(self.data_root, kind, message, self.events)


# ---- one test --------------------------------------------------------------------------------------

def run(ctx, name, space='large', amp=None):
    spec, test = ctx.spec, ctx.spec.tests[name]
    kind = test['type']
    defaults = spec.defaults()
    state = ctx.state()
    room = spec.space(space) if kind in ('couple', 'speed', 'steps') else {}
    amplitude = float(amp if amp is not None else room.get('amp', 0.5)) if room else None
    upstream = results.snapshot(state, defaults, test.get('upstream'))
    base = dict(test=name, type=kind, layer=spec.layer(name), space=space if room else None,
                amp=amplitude, state=results.state_hash(ctx.state_path), upstream=upstream,
                software=ctx.software)
    ctx.holds = []
    ctx.out(f'{name}: {spec.layer(name)} ({kind})')
    if kind == 'nav2':
        outcome = nav2(ctx, test, state, defaults)
        return finish(ctx, name, test, base, outcome, state, defaults)
    topics = spec.topics
    command_topic = topics.get('velocity' if kind == 'steps' else 'effort')
    if not command_topic or not topics.get('odom'):
        raise Abort(f'{spec.path}: topics needs {"velocity" if kind == "steps" else "effort"} and odom')
    runner = ctx.make_runner(command_topic, topics['odom'], topics.get('battery'), ctx.sim_time)
    try:
        if not runner.wait_ready():
            raise Abort(f'no odometry on {topics["odom"]} (or no /clock with sim time)')
        batt = f' batt={runner.battery:.1f}V' if runner.battery else ''
        amp_text = f' space={space} amp={amplitude:g}' if room else ''
        ctx.log('test', f'start {name} {amp_text} state={base["state"]}{batt}')
        for gate in spec.gates:
            seen, held = runner.gate_state(gate)
            if held:
                raise Abort(f'{gate.get("name", gate["state"])}: {fmt(held)} ({gate["state"]}); '
                            f'{gate.get("clear", "clear it")}, then run again')
            if not seen:
                ctx.out(f'  warning: no state on {gate["state"]} (is the stack up?)')
        if spec.lane.get('state') and hasattr(runner, 'watch'):
            runner.watch(spec.lane)
        procedure = {'still': still, 'couple': couple, 'speed': speed, 'steps': steps}[kind]
        outcome = procedure(ctx, runner, test, state, defaults, room, amplitude)
    except KeyboardInterrupt:
        runner.close()
        ctx.log('test', f'abort {name} at {ctx.progress} (ctrl-c)')
        results.save(log.day_dir(ctx.data_root), dict(base, status='aborted', reason='ctrl-c', at=ctx.progress,
                                                      holds=ctx.holds))
        raise
    except (Abort, ValueError) as error:
        runner.close()
        ctx.log('test', f'abort {name} at {ctx.progress} ({error})')
        results.save(log.day_dir(ctx.data_root), dict(base, status='aborted', reason=str(error), at=ctx.progress,
                                                      holds=ctx.holds))
        raise Abort(str(error)) from None
    runner.close()
    return finish(ctx, name, test, base, outcome, state, defaults)


def finish(ctx, name, test, base, outcome, state, defaults):
    result = dict(base, status='done', **outcome)
    if (test.get('apply') or {}).get('restart'):
        result['restart'] = test['apply']['restart']
    if ctx.holds:
        result['holds'] = ctx.holds
    path = results.save(log.day_dir(ctx.data_root), result)
    tail = f' -> astro apply {name}' if result.get('changes') else ''
    ctx.log('test', f'done  {name}  {result["summary"]}{tail}')
    if result.get('changes'):
        _, diffs = applying.changed(state, defaults, result['changes'], ctx.spec.axes)
        for key, old, new in diffs:
            ctx.out(f'  {key}  {fmt(old)} -> {fmt(new)}')
        ctx.out(f'  apply: astro apply {name}    (result {path})')
    else:
        ctx.out(f'  result {path} (nothing to apply)')
    return path


def neutral(n):
    return [0.0] * n


def _hold(ctx, runner, n, command, baseline_s, settle_s, measure_s, main=None):
    """Neutral baseline, then the command for settle + measure; (baseline rows, measure rows, all, t_on, t_cut).
    The neutral lasts until the boat stops coasting: its velocity no longer changes (a steady current is
    allowed, so this works in a current where "below the noise" never comes), at most coast_max_s."""
    start = runner.now()
    b0, b1 = runner.hold(neutral(n), baseline_s)
    while b1 - start < COAST_MAX_S and not coasted(runner.window(b1 - 2 * COAST_WINDOW_S, b1)):
        _, b1 = runner.hold(neutral(n), 0.5)
    b0 = b1 - baseline_s
    t_on, _ = runner.hold(command, settle_s)
    m0, t_cut = runner.hold(command, measure_s)
    record(ctx, main, command, t_on, t_cut, runner.window(t_on, t_cut))
    check_lane(ctx, runner, m0, t_cut)
    return runner.window(b0, b1), runner.window(m0, t_cut), runner.window(b0, t_cut), t_on, t_cut


COAST_MAX_S, COAST_WINDOW_S, COAST_TOLERANCE = 8.0, 1.0, 0.03   # s, s, m/s (rad/s)


def coasted(rows):
    """True when the mean velocity of the last window differs from the one before by under the tolerance."""
    if len(rows) < 4:
        return True
    middle = rows[0][0] + (rows[-1][0] - rows[0][0]) / 2
    first, last = [r for r in rows if r[0] < middle], [r for r in rows if r[0] >= middle]
    if not first or not last:
        return True
    return all(abs(sum(r[k] for r in last) / len(last) - sum(r[k] for r in first) / len(first)) < COAST_TOLERANCE
               for k in (1, 2, 3))


LANE_MAX_OFF_S = 0.5


def off_lane(log, t0, t1, want):
    """Seconds of [t0, t1] the lane log ((t, value, cause), in time order) shows a value other than
    ``want``, and the last such (value, cause). Before the first message the value is unknown (not counted)."""
    off, last, bad = 0.0, None, None
    for i, (t, value, cause) in enumerate(log):
        end = log[i + 1][0] if i + 1 < len(log) else t1
        low, high = max(t, t0), min(end, t1)
        if high > low and value != want:
            off += high - low
            bad = (value, cause)
        last = t
    return off, bad


def check_lane(ctx, runner, t0, t1):
    lane = ctx.spec.lane
    log = getattr(runner, 'lane_log', None)
    if not lane.get('state') or log is None:
        return
    off, bad = off_lane(log, t0, t1, lane.get('want', 'auto'))
    if off > LANE_MAX_OFF_S:
        raise Abort(f'{lane["state"]} input is {bad[0]} ({bad[1]}) for {off:.1f} s of the measure window, '
                    f'not {lane.get("want", "auto")}: {lane.get("fix", "give the lane back")}')


HOLD_HZ = 30.0       # result samples are decimated to about this rate


def record(ctx, main, command, settle, end, rows):
    """One hold for the result file: its command (axis, sign, level; the efforts sent), the settle start and
    hold end on the node clock, and body-frame odometry from settle to end (t on that clock, t_sensor the
    message stamps), decimated to about HOLD_HZ."""
    kept, last = [], None
    for r in rows:
        if last is None or r[0] - last >= 1.0 / HOLD_HZ - 1e-6:
            kept.append(r)
            last = r[0]

    def column(k):
        return [None if len(r) <= k or r[k] is None else round(float(r[k]), 4) for r in kept]
    axis, sign, level = main or (None, None, None)
    ctx.holds.append(dict(command=dict(axis=axis, sign=sign, level=level,
                                       efforts=[round(float(v) + 0.0, 4) for v in command]),
                          start=round(settle, 3), end=round(end, 3), settle=round(settle, 3),
                          samples=dict(t=column(0), vx=column(1), vy=column(2), wz=column(3), yaw=column(4),
                                       t_sensor=[None if len(r) < 8 else round(float(r[7]), 4) for r in kept])))


def still(ctx, runner, test, state, defaults, room, amplitude):
    from . import fit
    seconds = float(test.get('seconds', 20))
    ctx.progress = 'neutral'
    t0, t1 = runner.hold(neutral(len(ctx.spec.axes)), seconds)
    rows = runner.window(t0, t1)
    if len(rows) < 5:
        raise Abort(f'{len(rows)} odometry samples in {seconds:g} s')
    sigma = [round(statistics.pstdev(r[k] for r in rows), 4) for k in (1, 2, 3)]
    drift = fit.drift(rows)
    summary = f'noise sigma={fmt(sigma)}'
    if drift:
        summary = f'drift={drift["speed"]:.2f}m/s@{drift["heading_deg"]:.0f}deg ' + summary
    return dict(summary=summary, measured=dict(drift=drift, sigma=dict(zip(ctx.spec.axes, sigma)),
                                               samples=len(rows)))


def couple(ctx, runner, test, state, defaults, room, amplitude):
    """The decoupling column of one axis, measured as applied: each hold sets the candidate column live on
    the driver (both signed matrices in one request) and commands the main axis alone, so the search's root
    is the column to write. The original live matrices come back at the end, on abort and on Ctrl-C."""
    from . import fit
    axes, target = ctx.spec.axes, test.get('apply') or {}
    j, n = axes.index(test['axis']), len(axes)
    key, signed = target.get('key'), target.get('signed')
    plus = applying.current(state, defaults, key)
    minus = applying.current(state, defaults, signed, key) if signed else None
    if not isinstance(plus, list) or len(plus) != n * n:
        raise Abort(f'{key}: no {n}x{n} matrix in the state or the defaults')
    live = {item.get('key') or item['param']: item for item in target.get('live') or []}
    if key not in live or (signed and signed not in live):
        raise Abort(f'apply.live must name the driver parameters of {key}' + (f' and {signed}' if signed else ''))
    node = live[key]['node']
    params = {1: live[key]['param'], -1: live[signed]['param'] if signed else None}
    matrices = {1: [float(v) for v in plus], -1: [float(v) for v in minus] if signed else None}
    cross = fit.cross_axes(j, n)
    signs = (1, -1) if signed else (1,)
    tolerance = test.get('tolerance', [0.05, 0.05])
    holds = int(test.get('holds', 4))
    searches = {s: fit.Search(tolerance, float(test.get('probe', 0.15)), float(test.get('limit', 1.0)), holds,
                              start=tuple(matrices[s][n * k + j] for k in cross)) for s in signs}
    min_response = float(test.get('min_response', 0.03))

    def candidate(sign):
        matrix = list(matrices[sign])
        for k, value in zip(cross, searches[sign].column):
            matrix[n * k + j] = float(value)
        return matrix
    original = {params[s]: matrices[s] for s in signs}
    measured, count, total = [], 0, len(signs) * holds
    try:
        while not all(search.done for search in searches.values()):
            for sign in signs:
                search = searches[sign]
                if search.done:
                    continue
                count += 1
                ctx.progress = f'hold {count}/{total}'
                runner.set_params(node, {params[s]: candidate(s) for s in signs})
                command = fit.command_for(j, sign, amplitude, (0.0,) * len(cross), n)
                before, during, _, _, _ = _hold(ctx, runner, n, command, float(test.get('baseline_s', 2)),
                                                float(test.get('settle_s', 4)), float(test.get('measure_s', 3)),
                                                (axes[j], sign, amplitude))
                delta = fit.response(before, during)
                main, ratios = fit.cross_response(delta, j, sign)
                if sign * delta[j] < min_response:
                    raise Abort(f'{axes[j]}{"+" if sign > 0 else "-"} moved {sign * delta[j]:.3f} '
                                f'(under min_response {min_response:g}): too small to measure coupling')
                measured.append(dict(sign=sign, column=[round(c + 0.0, 4) for c in search.column],
                                     response=[round(v, 4) for v in delta], cross=[round(r, 4) for r in ratios]))
                ctx.out(f'  {ctx.progress} {axes[j]}{"+" if sign > 0 else "-"} '
                        f'c={fmt([round(c, 4) for c in search.column])} main={main:.3f} cross={fmt(list(ratios))}')
                search.add(ratios)
    finally:
        runner.neutral()
        try:
            runner.set_params(node, original)
        except Exception as error:      # noqa: BLE001 - say it, never hide the abort that brought us here
            ctx.out(f'  WARNING: could not restore {node} {", ".join(original)} ({error}): restart the driver')
    changes, parts = [], []
    for sign in signs:
        best = searches[sign].result()
        searches[sign].column = best['column']
        column = [round(v + 0.0, 4) + 0.0 for v in applying.column_of(candidate(sign), j)]
        name = f'{axes[j]}{"+" if sign > 0 else "-"}'
        parts.append(f'{name} cross={fmt(list(best["response"]))} '
                     f'{"converged" if best["converged"] else "best of " + str(best["holds"])}')
        changes.append(dict(key=key, column=j, value=column) if sign > 0 else
                       dict(key=signed, column=j, value=column, fallback=key))
    return dict(summary=' | '.join(parts), measured=dict(holds=measured), changes=changes,
                live=list(target.get('live') or []))


def _levels(room, amplitude):
    levels = [float(v) for v in room.get('levels') or [amplitude]]
    if amplitude is not None and room.get('amp') and amplitude != float(room['amp']):
        scale = amplitude / max(levels)
        levels = [round(v * scale, 3) for v in levels]
    return levels


def speed(ctx, runner, test, state, defaults, room, amplitude):
    from . import fit
    axes, target = ctx.spec.axes, test.get('apply') or {}
    j, n, axis = ctx.spec.axes.index(test['axis']), len(ctx.spec.axes), test['axis']
    levels = _levels(room, amplitude)
    baseline_s, coast_s = float(test.get('baseline_s', 2)), float(test.get('coast_s', 4))
    settle_s, measure_s = float(test.get('settle_s', 5)), float(test.get('measure_s', 3))
    points, rises, count = {1: [], -1: []}, [], 0
    for level in levels:
        for sign in (1, -1):
            count += 1
            ctx.progress = f'hold {count}/{2 * len(levels)}'
            runner.hold(neutral(n), max(0.0, coast_s - baseline_s))
            command = neutral(n)
            command[j] = sign * level
            before, during, rows, t_on, t_cut = _hold(ctx, runner, n, command, baseline_s, settle_s, measure_s,
                                                      (axis, sign, level))
            delta = fit.response(before, during)
            points[sign].append((sign * delta[j], level))
            rise = fit.t95([r[0] for r in rows], [sign * r[1 + j] for r in rows], t_on, t_cut)
            if rise is not None:
                rises.append(rise)
            ctx.out(f'  {ctx.progress} {axis}{"+" if sign > 0 else "-"} e={level:g} v={sign * delta[j]:.3f}'
                    + (f' t95={rise:.2f}s' if rise is not None else ''))
    runner.hold(neutral(n), coast_s)
    prefix = target.get('key', 'characterization')
    curves, parts = {}, []
    for sign in (1, -1):
        name = f'{axis}{"+" if sign > 0 else "-"}'
        curve = fit.speed_curve(points[sign])
        if not curve:
            raise Abort(f'{name}: no level moved the boat forward along the axis ({points[sign]})')
        curves[name] = curve
    rise = round(statistics.median(rises), 3) if rises else None
    velocity = round(min(fit.speed_at(c['d1'], c['d2'], 1.0) for c in curves.values()), 4)
    changes = [dict(key=f'{prefix}.speed_curves.{name}', value=dict(d1=round(c['d1'], 5), d2=round(c['d2'], 5)))
               for name, c in curves.items()]
    changes.append(dict(key=f'{prefix}.motion_limits.velocity.{axis}', value=velocity))
    if rise:
        changes.append(dict(key=f'{prefix}.t95.{axis}', value=rise))
        changes.append(dict(key=f'{prefix}.motion_limits.acceleration.{axis}', value=round(velocity * 3 / rise, 4)))
    if target.get('version') is not None:
        changes.append(dict(key=f'{prefix}.version', value=target['version']))
    for name, c in curves.items():
        parts.append(f'{name} d1={c["d1"]:.3g} d2={c["d2"]:.3g}')
    summary = ' | '.join(parts) + (f' t95={rise:.2f}s' if rise else '') + f' vmax={velocity:.3g}'
    return dict(summary=summary, changes=changes, live=list(target.get('live') or []),
                measured=dict(points={k: [list(p) for p in v] for k, v in
                                      (('+', points[1]), ('-', points[-1]))}, t95=rises))


def steps(ctx, runner, test, state, defaults, room, amplitude):
    from . import fit
    axes = ctx.spec.axes
    j, n, axis = axes.index(test['axis']), len(axes), test['axis']
    limit_key = test.get('limits')
    limit = applying.get(state, f'{limit_key}.{axis}') if limit_key else None
    if limit_key and not limit:
        raise Abort(f'no {limit_key}.{axis} in the state: run and apply the speed test first')
    target_v = float(test.get('fraction', 0.5)) * float(limit) * (amplitude / float(room.get('amp', amplitude))) \
        if limit else float(test.get('target', 0.3))
    baseline_s, step_s, coast_s = (float(test.get(k, d)) for k, d in
                                   (('baseline_s', 2), ('step_s', 6), ('coast_s', 3)))
    metrics, parts = {}, []
    for count, sign in enumerate((1, -1), 1):
        ctx.progress = f'step {count}/2'
        runner.hold(neutral(n), baseline_s)
        command = neutral(n)
        command[j] = sign * target_v
        t_on, t_off = runner.hold(command, step_s)
        runner.hold(neutral(n), coast_s)
        record(ctx, (axis, sign, target_v), command, t_on, t_off, runner.window(t_on, t_off))
        check_lane(ctx, runner, t_on, t_off)
        samples = [(r[0], r[1 + j]) for r in runner.window(t_on, t_off)]
        m = fit.step_metrics(samples, sign * target_v, t_on, t_off)
        name = f'{axis}{"+" if sign > 0 else "-"}'
        metrics[name] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()}
        parts.append(f'{name} rise={fmt(m.get("rise_time"))} over={fmt(m.get("overshoot"))} '
                     f'settle={fmt(m.get("settling_time"))} sse={fmt(m.get("steady_state_error"))}')
    gains = test.get('gains')
    hint = f' (tune live: ros2 param set {gains})' if gains else ''
    return dict(summary=' | '.join(parts) + hint, measured=metrics)


def nav2(ctx, test, state, defaults):
    from . import fit
    axes, source, target = ctx.spec.axes, test.get('from') or {}, test.get('apply') or {}
    values = {}
    for part in ('velocity', 'acceleration', 't95'):
        key = source.get(part)
        values[part] = applying.get(state, key) if key else None
        missing = [a for a in axes if not isinstance(values[part], dict) or values[part].get(a) is None]
        if missing:
            raise Abort(f'no {key}.{"/".join(missing)} in the state: run and apply the speed tests first')
    limits = fit.nav2_limits(values['velocity'], values['acceleration'], values['t95'], test['rules'],
                             axes, ctx.spec.rotational)
    summary = f'velocity={fmt(limits["velocity"])} acceleration={fmt(limits["acceleration"])}'
    return dict(summary=summary, changes=[dict(key=target.get('key'), value=limits)],
                live=list(target.get('live') or []))
