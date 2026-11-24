"""The test types a tests file composes: still, couple, speed, steps, nav2, watch (passive: someone drives). Each runs alone, writes one result
file (its changes, the upstream keys it ran on, a one-line summary), logs `test start/done/abort` and prints
its layer, old -> new and the exact `astro apply` command. Nothing here names a robot: topics, axes, keys,
spaces and parameters all come from the tests file."""
import math
import statistics

import yaml

from . import apply as applying
from . import log, results     # fit (numpy) is imported by the test types that run

TYPES = ('still', 'couple', 'speed', 'steps', 'nav2', 'watch')
DEFAULT_LAYER = {'still': 'conditions', 'couple': 'decoupling', 'speed': 'speed curve',
                 'steps': 'velocity loop', 'nav2': 'nav2 limits', 'watch': 'speed curve (driven)'}


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
    if kind == 'watch':                    # passive: listens to the applied command, publishes nothing
        command_topic, listen = None, topics.get('applied')
        if not listen or not topics.get('odom'):
            raise Abort(f'{spec.path}: topics needs applied and odom')
    else:
        command_topic = topics.get('velocity' if kind == 'steps' else 'effort')
        if not command_topic or not topics.get('odom'):
            raise Abort(f'{spec.path}: topics needs {"velocity" if kind == "steps" else "effort"} and odom')
    runner = ctx.make_runner(command_topic, topics['odom'], topics.get('battery'), ctx.sim_time)
    if kind == 'watch':
        runner.listen(listen)
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
        procedure = {'still': still, 'couple': couple, 'speed': speed, 'steps': steps, 'watch': watch}[kind]
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
            if old != new:
                ctx.out(f'  {key}  {fmt(old)} -> {fmt(new)}')
        ctx.out(f'  apply: astro apply {name}    (result {path})')
    else:
        ctx.out(f'  result {path} (nothing to apply)')
    return path


def neutral(n):
    return [0.0] * n


def _hold(ctx, runner, n, command, baseline_s, settle_s, measure_s, main=None):
    """Neutral baseline, then the command for settle + measure; (baseline rows, measure rows, all, t_on, t_cut).
    The neutral lasts until the boat stops coasting: its velocity no longer changes and its speed is near the
    drift the last `still` measured (a steady current is allowed), at most COAST_MAX_S; a hold that starts
    unsettled says so and is recorded `settled: false`."""
    start = runner.now()
    reference = still_drift(ctx)
    b0, b1 = runner.hold(neutral(n), baseline_s)
    while b1 - start < COAST_MAX_S and not settled(runner.window(b1 - 2 * COAST_WINDOW_S, b1), reference):
        _, b1 = runner.hold(neutral(n), 0.5)
    calm = settled(runner.window(b1 - 2 * COAST_WINDOW_S, b1), reference)
    if not calm:
        ctx.out(f'  warning: still coasting after {COAST_MAX_S:g} s of neutral; this hold is recorded unsettled')
    b0 = b1 - baseline_s
    t_on, _ = runner.hold(command, settle_s)
    m0, t_cut = runner.hold(command, measure_s)
    record(ctx, main, command, t_on, t_cut, runner.window(b0, t_cut), (b0, b1), (m0, t_cut), calm)
    check_lane(ctx, runner, m0, t_cut)
    return runner.window(b0, b1), runner.window(m0, t_cut), runner.window(b0, t_cut), t_on, t_cut


COAST_MAX_S, COAST_WINDOW_S, COAST_TOLERANCE = 8.0, 1.0, 0.03   # s, s, m/s (rad/s)
DRIFT_TOLERANCE = 0.05                                          # m/s (rad/s) over the still drift


def still_drift(ctx):
    """The drift speed (m/s) of the latest `still` result, or None (then only the coasting check counts)."""
    names = [k for k, t in ctx.spec.tests.items() if t.get('type') == 'still']
    found = [r for r in results.load_all(ctx.data_root) if r.get('test') in names and r.get('status') == 'done']
    drift = ((found[-1].get('measured') or {}).get('drift') or {}) if found else {}
    return float(drift['speed']) if drift.get('speed') is not None else None


def settled(rows, reference):
    """Coasted, and (with a still drift) the speed within DRIFT_TOLERANCE of it and no turn left."""
    if not coasted(rows):
        return False
    if reference is None or len(rows) < 4:
        return True
    from . import fit
    now = fit.drift(rows)
    wz = sum(r[3] for r in rows) / len(rows)
    speed = now['speed'] if now else math.hypot(sum(r[1] for r in rows) / len(rows), sum(r[2] for r in rows) / len(rows))
    return abs(speed - reference) < DRIFT_TOLERANCE and abs(wz) < DRIFT_TOLERANCE


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
CLOCKS_APART_S = 1.0  # t_sensor is left out when the message stamps are this far from the node clock


def record(ctx, main, command, start, end, rows, baseline=None, measure=None, calm=True):
    """One hold for the result file: its command (axis, sign, level; the efforts sent), start (command on) and
    end on the node clock, the neutral baseline [b0, b1] and measure window [m0, end] before and in it, and
    body-frame odometry from the baseline's start to end (t on the node clock; t_sensor, the message stamps,
    only when they are on the same clock), decimated to about HOLD_HZ."""
    kept, last = [], None
    for r in rows:
        if last is None or r[0] - last >= 1.0 / HOLD_HZ - 1e-6:
            kept.append(r)
            last = r[0]

    def column(k):
        return [None if len(r) <= k or r[k] is None else round(float(r[k]), 4) for r in kept]
    axis, sign, level = main or (None, None, None)
    samples = dict(t=column(0), vx=column(1), vy=column(2), wz=column(3), yaw=column(4))
    offsets = sorted(abs(r[0] - r[7]) for r in kept if len(r) >= 8 and r[7] is not None)
    if offsets and offsets[len(offsets) // 2] <= CLOCKS_APART_S:
        samples['t_sensor'] = [None if len(r) < 8 or r[7] is None else round(float(r[7]), 4) for r in kept]
    hold = dict(command=dict(axis=axis, sign=sign, level=level, efforts=[round(float(v) + 0.0, 4) for v in command]),
                start=round(start, 3), end=round(end, 3))
    if baseline:
        hold['baseline'] = [round(t, 3) for t in baseline]
    if measure:
        hold['measure'] = [round(t, 3) for t in measure]
    if not calm:
        hold['settled'] = False
    ctx.holds.append(dict(hold, samples=samples))


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
    found = conditions(rows, sigma, drift, test, results.state_hash(ctx.state_path))
    summary += f' {found["verdict"]} (trust up to {found["ceiling"]})'
    return dict(summary=summary, measured=dict(drift=drift, sigma=dict(zip(ctx.spec.axes, sigma)),
                                               samples=len(rows), conditions=found))


# still's thresholds, each overridable in its tests.yaml entry
ROUGH = {'rough_sigma': 0.05, 'rough_drift': 0.3, 'steady_drift': 0.05}    # m/s (rad/s), m/s, m/s


def conditions(rows, sigma, drift, test, state):
    """What the water allows, from a still run:
    verdict: ok, or rough when a noise sigma is over rough_sigma or the drift speed over rough_drift;
    drift_trust: the drift is steady (the two halves' world-frame means differ by under steady_drift), so a
      hold's baseline removes it;
    ceiling: the highest layer worth trusting: L7 (all) when ok and steady, else L4 (the large-signal speed
      curves; small coupling and step metrics drown);
    state: the state hash the still ran on."""
    from . import fit
    limits = {k: float(test.get(k, v)) for k, v in ROUGH.items()}
    rough = max(sigma) > limits['rough_sigma'] or bool(drift and drift['speed'] > limits['rough_drift'])
    halves = [fit.drift(rows[:len(rows) // 2]), fit.drift(rows[len(rows) // 2:])]
    steady = all(halves) and math.hypot(halves[0]['east'] - halves[1]['east'],
                                        halves[0]['north'] - halves[1]['north']) < limits['steady_drift']
    return dict(verdict='rough' if rough else 'ok', ceiling='L7' if not rough and steady else 'L4',
                drift_trust=bool(steady), state=state)


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
    square = test.get('drag', 'quadratic') == 'quadratic'     # cross velocity ~ sqrt(cross force)
    searches = {s: fit.Search(tolerance, float(test.get('probe', 0.15)), float(test.get('limit', 1.0)), holds,
                              start=tuple(matrices[s][n * k + j] for k in cross), square=square) for s in signs}
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
        if column == [round(v + 0.0, 4) + 0.0 for v in applying.column_of(matrices[sign], j)]:
            parts[-1] += ' (the current column is the best measured: nothing to apply)'
            continue
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
    unit = 'rad/s' if axis == ctx.spec.rotational else 'm/s'
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
            ctx.out(f'  {ctx.progress} {axis}{"+" if sign > 0 else "-"} e={level:g} v={sign * delta[j]:.3f}{unit}'
                    + (f' t95={rise:.2f}s' if rise is not None else ''))
    runner.hold(neutral(n), coast_s)
    prefix = target.get('key', 'characterization')
    curves, parts, unclean = {}, [], []
    for sign in (1, -1):
        name = f'{axis}{"+" if sign > 0 else "-"}'
        kept, dropped = monotone(points[sign])
        for v, level in dropped:
            ctx.out(f'  warning: {name} e={level:g} moved {v:.3f} {unit}, slower than a lower level: left out of the fit '
                    f'(blocked, grounded or taken over? check the bag; run it again for a clean curve)')
        if dropped:
            unclean.extend(f'{name}@{level:g}' for _, level in dropped)
        curve = fit.speed_curve(kept)
        if not curve:
            raise Abort(f'{name}: no level moved the boat forward along the axis ({points[sign]})')
        curves[name] = curve
    rise = round(statistics.median(rises), 3) if rises else None
    changes, velocity, extrapolated = curve_changes(prefix, axis, curves, rise)
    if target.get('version') is not None:
        changes.append(dict(key=f'{prefix}.version', value=target['version']))
    for name, c in curves.items():
        parts.append(f'{name} d1={c["d1"]:.3g} d2={c["d2"]:.3g}')
    summary = (' | '.join(parts) + (f' dropped {",".join(unclean)}' if unclean else '') +
               (f' t95={rise:.2f}s' if rise else '') +
               f' vmax={velocity:.3g}{unit} (measured at e={max(levels):g}; curve at e=1: {extrapolated:.3g})')
    return dict(summary=summary, changes=changes, live=list(target.get('live') or []),
                measured=dict(points={k: [list(p) for p in v] for k, v in
                                      (('+', points[1]), ('-', points[-1]))}, t95=rises,
                              extrapolated_velocity=extrapolated, dropped=unclean))


def curve_changes(prefix, axis, curves, rise):
    """The speed-curve changes of one axis: its signed curves, velocity limit (the highest measured level's
    speed, never the curve extrapolated to full effort), t95 and acceleration; (changes, velocity, extrapolated)."""
    from . import fit
    velocity = round(min(c['max_speed'] for c in curves.values()), 4)
    extrapolated = round(min(fit.speed_at(c['d1'], c['d2'], 1.0) for c in curves.values()), 4)
    changes = [dict(key=f'{prefix}.speed_curves.{name}', value=dict(d1=round(c['d1'], 5), d2=round(c['d2'], 5)))
               for name, c in curves.items()]
    changes.append(dict(key=f'{prefix}.motion_limits.velocity.{axis}', value=velocity))
    if rise:
        changes.append(dict(key=f'{prefix}.t95.{axis}', value=rise))
        changes.append(dict(key=f'{prefix}.motion_limits.acceleration.{axis}', value=round(velocity * 3 / rise, 4)))
    return changes, velocity, extrapolated


def monotone(points):
    """(kept, dropped) of (speed, level) points: a level slower than a lower level's speed is dropped (the boat
    was blocked or taken over); faster levels must not go slower."""
    kept, dropped, fastest = [], [], -math.inf
    for v, level in sorted(points, key=lambda p: p[1]):
        if v < fastest:
            dropped.append((v, level))
        else:
            kept.append((v, level))
            fastest = v
    return kept, dropped


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
        b0, b1 = runner.hold(neutral(n), baseline_s)
        command = neutral(n)
        command[j] = sign * target_v
        t_on, t_off = runner.hold(command, step_s)
        runner.hold(neutral(n), coast_s)
        record(ctx, (axis, sign, target_v), command, t_on, t_off, runner.window(b0, t_off), (b0, b1), (t_on, t_off))
        check_lane(ctx, runner, t_on, t_off)
        samples = [(r[0], r[1 + j]) for r in runner.window(t_on, t_off)]
        m = fit.step_metrics(samples, sign * target_v, t_on, t_off)
        name = f'{axis}{"+" if sign > 0 else "-"}'
        metrics[name] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()}
        unit = 'rad/s' if axis == ctx.spec.rotational else 'm/s'

        def sec(v):
            return 'none' if v is None else f'{v:.2f}s'
        over, sse = m.get('overshoot'), m.get('steady_state_error')
        parts.append(f'{name} to {target_v:.3g}{unit}: rise={sec(m.get("rise_time"))} '
                     f'over={"none" if over is None else f"{100 * over:.0f}%"} settle={sec(m.get("settling_time"))} '
                     f'sse={"none" if sse is None else f"{sse:.3f}{unit}"}')
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
    limits, why = fit.nav2_limits(values['velocity'], values['acceleration'], values['t95'], test['rules'],
                                  axes, ctx.spec.rotational)
    for k, a in enumerate(axes):
        unit = 'rad/s' if a == ctx.spec.rotational else 'm/s'
        ctx.out(f'  {a}: velocity {limits["velocity"][k]:g} {unit} ({why[a]["velocity"]}), '
                f'acceleration {limits["acceleration"][k]:g} {unit}/s ({why[a]["acceleration"]})')
    summary = f'velocity={fmt(limits["velocity"])} acceleration={fmt(limits["acceleration"])}'
    return dict(summary=summary, changes=[dict(key=target.get('key'), value=limits)],
                measured=dict(why=why), live=list(target.get('live') or []))


# ---- watch: someone else drives (RC, keyboard), the test only listens ----------------------------------------

WATCH = {'levels': 3, 'level_gap': 0.08, 'min_effort': 0.08, 'steady_tol': 0.04, 'cross_max': 0.3,
         'baseline_s': 1.0, 'settle_s': 2.5, 'measure_s': 1.5, 'drift_s': 10.0, 'max_s': 900.0}
EFFORT_GAP_S = 0.5        # a pause this long in the applied command ends a run
NEUTRAL_E = 0.02          # every effort under this is neutral
STATUS_S = 2.0


def watch_options(test):
    return {k: float(test.get(k, v)) for k, v in WATCH.items()}


def runs(efforts, o):
    """Runs of the applied command (t, e0, e1, e2), in time order: (kind, j, sign, level, t0, t1, mean efforts),
    kind 'neutral' or 'hold'. A hold is one axis at |e| >= min_effort with the others at most cross_max of it,
    its effort within steady_tol of the run's mean; anything else (mixed, ramping) ends the run."""
    out, current = [], None

    def label(e):
        mags = [abs(v) for v in e]
        if max(mags) < NEUTRAL_E:
            return ('neutral', None, 0)
        j = mags.index(max(mags))
        if mags[j] < o['min_effort'] or any(m > o['cross_max'] * mags[j] for k, m in enumerate(mags) if k != j):
            return None
        return ('hold', j, 1 if e[j] > 0 else -1)

    def close(run):
        if run and run['t1'] > run['t0']:
            mean = [s / run['n'] for s in run['sum']]
            level = abs(mean[run['j']]) if run['j'] is not None else 0.0
            out.append((run['kind'], run['j'], run['sign'], round(level, 3), run['t0'], run['t1'], mean))

    for t, *e in efforts:
        key = label(e)
        if current is not None:
            same = key is not None and key == current['key'] and t - current['t1'] <= EFFORT_GAP_S
            if same and key[0] == 'hold':
                mean = current['sum'][key[1]] / current['n']
                same = abs(e[key[1]] - mean) <= o['steady_tol']
            if same:
                current['t1'] = t
                current['n'] += 1
                current['sum'] = [a + b for a, b in zip(current['sum'], e)]
                continue
            close(current)
            current = None
        if key is not None:
            current = dict(key=key, kind=key[0], j=key[1], sign=key[2], t0=t, t1=t, n=1, sum=list(e))
    return out, current


def clusters(levels, gap):
    """Greedy groups of sorted levels, each spanning less than ``gap``."""
    groups = []
    for level in sorted(levels):
        if groups and level - groups[-1][0] < gap:
            groups[-1].append(level)
        else:
            groups.append([level])
    return groups


class Coverage:
    """What the driving has covered so far: holds per axis and sign, onsets from rest, calm neutral time."""

    def __init__(self, axes, o):
        self.axes, self.o = axes, o
        self.holds = []           # dicts: j, sign, level, t0, t1, baseline (b0, b1) or None
        self.neutral_s = 0.0
        self.calm = []            # (t0, t1) neutral windows the boat had stopped coasting in
        self.seen = set()

    def add(self, run, previous, window):
        kind, j, sign, level, t0, t1, mean = run
        if (t0, kind) in self.seen:
            return None
        if kind == 'neutral':
            if t1 - t0 >= self.o['baseline_s']:
                b0 = t1 - self.o['baseline_s']
                if coasted(window(b0, t1)):
                    self.seen.add((t0, kind))
                    self.calm.append((max(t0, t1 - 4 * self.o['baseline_s']), t1))
                    self.neutral_s += t1 - t0
            return None
        if t1 - t0 < self.o['settle_s'] + self.o['measure_s']:
            return None
        self.seen.add((t0, kind))
        baseline = None
        if previous and previous[0] == 'neutral' and t0 - previous[5] <= EFFORT_GAP_S and \
                previous[5] - previous[4] >= self.o['baseline_s']:
            b1 = previous[5]
            b0 = b1 - self.o['baseline_s']
            if coasted(window(b0, b1)):
                baseline = (b0, b1)
        hold = dict(j=j, sign=sign, level=level, t0=t0, t1=t1, baseline=baseline, efforts=mean)
        self.holds.append(hold)
        return hold

    def pair(self, j, sign):
        groups = clusters([h['level'] for h in self.holds if h['j'] == j and h['sign'] == sign], self.o['level_gap'])
        return min(len(groups), int(self.o['levels']))

    def onset(self, j):
        return any(h['baseline'] for h in self.holds if h['j'] == j)

    def fraction(self):
        need = int(self.o['levels'])
        got = sum(self.pair(j, s) for j in range(len(self.axes)) for s in (1, -1))
        got += sum(self.onset(j) for j in range(len(self.axes)))
        got += min(1.0, self.neutral_s / self.o['drift_s']) if self.o['drift_s'] > 0 else 1.0
        return got / (2 * need * len(self.axes) + len(self.axes) + 1)

    def missing(self):
        need, out = int(self.o['levels']), []
        for j, axis in enumerate(self.axes):
            for sign in (1, -1):
                have = self.pair(j, sign)
                if have < need:
                    levels = sorted(h['level'] for h in self.holds if h['j'] == j and h['sign'] == sign)
                    hint = f' (have e={",".join(f"{v:g}" for v in levels)}; differ by {self.o["level_gap"]:g})' \
                        if levels else ''
                    out.append(f'{axis}{"+" if sign > 0 else "-"} {need - have} more level(s){hint}')
            if not self.onset(j):
                out.append(f'{axis} one hold started from rest ({self.o["baseline_s"]:g} s still first)')
        if self.neutral_s < self.o['drift_s']:
            out.append(f'{self.o["drift_s"] - self.neutral_s:.0f} s more sitting still')
        return out

    def line(self, current, now):
        width = 20
        f = self.fraction()
        bar = '#' * int(round(f * width)) + '-' * (width - int(round(f * width)))
        need = int(self.o['levels'])
        parts = [f'{a}{"+" if s > 0 else "-"} {self.pair(j, s)}/{need}' for j, a in enumerate(self.axes)
                 for s in (1, -1)]
        parts.append(f'still {min(self.neutral_s, self.o["drift_s"]):.0f}/{self.o["drift_s"]:.0f}s')
        text = f'[{bar}] {100 * f:3.0f}%  ' + '  '.join(parts)
        if current and current['kind'] == 'hold':
            held = now - current['t0']
            total = self.o['settle_s'] + self.o['measure_s']
            text += (f'  | holding {self.axes[current["j"]]}{"+" if current["sign"] > 0 else "-"} '
                     f'e={abs(current["sum"][current["j"]] / current["n"]):.2f} {min(held, total):.1f}/{total:g}s')
        return text


def _status(ctx, text):
    import sys
    if ctx.out is print and sys.stdout.isatty():
        sys.stdout.write('\r\033[K' + text)
        sys.stdout.flush()
        return True
    return False


def _clear(ctx):
    import sys
    if ctx.out is print and sys.stdout.isatty():
        sys.stdout.write('\r\033[K')


def lane_at(runner, t):
    log = getattr(runner, 'lane_log', None) or []
    value = None
    for when, input_, _ in log:
        if when > t:
            break
        value = input_
    return value


def watch(ctx, runner, test, state, defaults, room, amplitude):
    """Passive: someone drives (RC, keyboard) while this listens to the applied command and odometry; steady
    single-axis holds count toward `levels` distinct efforts per axis and sign, one start from rest per axis
    (t95) and `drift_s` of sitting still (drift). Ends when all are covered, after `max_s`, or at Ctrl-C
    (which fits what there is). Writes the speed-curve keys of each axis with enough holds."""
    o = watch_options(test)
    axes, target = ctx.spec.axes, test.get('apply') or {}
    cover = Coverage(axes, o)
    start = runner.now()
    tty = _status(ctx, 'listening: drive (RC or keyboard); Ctrl-C to stop and fit what is there')
    if not tty:
        ctx.out('listening: drive (RC or keyboard); Ctrl-C to stop and fit what is there')
    last_status, ended, finished = start, 'complete', set()
    try:
        while True:
            runner.hold(None, 0.5)
            now = runner.now()
            done, current = runs(runner.efforts, o)
            previous = None
            for run in done:
                hold = cover.add(run, previous, runner.window)
                previous = run
                if hold:
                    ctx.progress = f'{100 * cover.fraction():.0f}%'
                    _clear(ctx)
                    hold['lane'] = lane_at(runner, hold['t0'])
                    m0 = hold['t1'] - o['measure_s']
                    during = runner.window(m0, hold['t1'])
                    v = during and sum(r[1 + hold['j']] for r in during) / len(during)
                    unit = 'rad/s' if axes[hold['j']] == ctx.spec.rotational else 'm/s'
                    pair = f'{axes[hold["j"]]}{"+" if hold["sign"] > 0 else "-"}'
                    # a mark line (log.txt and the events topic, so in the bag too) for each hold that counts
                    ctx.log('mark', f'watch {pair} e={hold["level"]:g} v~{v or 0:.2f}{unit}'
                            + (' from rest' if hold['baseline'] else '') + (f' ({hold["lane"]})' if hold['lane'] else '')
                            + f' t={hold["t0"]:.1f}..{hold["t1"]:.1f}')
                    if cover.pair(hold['j'], hold['sign']) == int(o['levels']) and pair not in finished:
                        finished.add(pair)
                        ctx.log('mark', f'watch {pair} done ({int(o["levels"])} levels)')
            if cover.fraction() >= 1.0 - 1e-9:
                break
            if now - start > o['max_s']:
                ended = f'max_s {o["max_s"]:g} s'
                break
            if now - last_status >= STATUS_S:
                last_status = now
                if not _status(ctx, cover.line(current, now)) and int(now - start) % 20 < STATUS_S:
                    ctx.out(cover.line(current, now) + '  need: ' + '; '.join(cover.missing()[:3]))
    except KeyboardInterrupt:
        ended = 'ctrl-c'
    _clear(ctx)
    ctx.out(f'  watch ended ({ended}) at {100 * cover.fraction():.0f}%'
            + (': still missing ' + '; '.join(cover.missing()) if cover.fraction() < 1 else ''))
    return watch_fit(ctx, runner, cover, o, target, ended)


def watch_fit(ctx, runner, cover, o, target, ended):
    from . import fit
    axes, n = ctx.spec.axes, len(ctx.spec.axes)
    calm_rows = [r for t0, t1 in cover.calm for r in runner.window(t0, t1)]
    drift = fit.drift(calm_rows) if calm_rows and all(r[4] is not None for r in calm_rows) else None
    reference = calm_rows or None            # baseline of a hold not started from rest: the still windows
    prefix = target.get('key', 'characterization')
    changes, parts, points, cross, skipped, rises_all = [], [], {}, {}, [], {}
    for j, axis in enumerate(axes):
        unit = 'rad/s' if axis == ctx.spec.rotational else 'm/s'
        curves, rises = {}, []
        for sign in (1, -1):
            name = f'{axis}{"+" if sign > 0 else "-"}'
            raw = []
            for h in (h for h in cover.holds if h['j'] == j and h['sign'] == sign):
                m0 = h['t1'] - o['measure_s']
                during = runner.window(m0, h['t1'])
                if h['baseline']:
                    before = runner.window(*h['baseline'])
                elif reference:
                    before = reference
                else:     # no still moment at all: no drift to take out
                    before = [(m0, 0.0, 0.0, 0.0, during[0][4] if during else None)]
                if not during or not before:
                    continue
                delta = fit.response(before, during)
                raw.append((sign * delta[j], h['level']))
                cross.setdefault(axis, []).append([round(delta[k] / delta[j], 4) if abs(delta[j]) > 1e-3 else None
                                                   for k in range(n)])
                b0 = h['baseline'][0] if h['baseline'] else h['t0']
                record(ctx, (axis, sign, h['level']), h['efforts'], h['t0'], h['t1'], runner.window(b0, h['t1']),
                       h['baseline'], (m0, h['t1']), bool(h['baseline']))
                ctx.holds[-1]['lane'] = h.get('lane')
                if h['baseline']:
                    rows = runner.window(b0, h['t1'])
                    rise = fit.t95([r[0] for r in rows], [sign * r[1 + j] for r in rows], h['t0'], h['t1'])
                    if rise is not None:
                        rises.append(rise)
            # repeats of one level are averaged, then the usual monotone check
            groups = {}
            for v, level in raw:
                key = next(g[0] for g in clusters([p[1] for p in raw], o['level_gap']) if level in g)
                groups.setdefault(key, []).append((v, level))
            averaged = [(sum(v for v, _ in g) / len(g), sum(e for _, e in g) / len(g)) for g in groups.values()]
            kept, dropped = monotone(averaged)
            points[name] = [[round(v, 4), round(e, 4)] for v, e in averaged]
            curve = fit.speed_curve(kept) if len(kept) >= 2 else None
            if curve:
                curves[name] = curve
                parts.append(f'{name} d1={curve["d1"]:.3g} d2={curve["d2"]:.3g}')
            else:
                skipped.append(f'{name} ({len(kept)} level(s))')
        rise = round(statistics.median(rises), 3) if rises else None
        rises_all[axis] = rises
        if len(curves) == 2:
            axis_changes, velocity, _ = curve_changes(prefix, axis, curves, rise)
            changes.extend(axis_changes)
            parts.append(f'{axis} vmax={velocity:.3g}{unit}' + (f' t95={rise:.2f}s' if rise else ''))
        elif curves:
            changes.extend(dict(key=f'{prefix}.speed_curves.{k}', value=dict(d1=round(c['d1'], 5),
                                                                              d2=round(c['d2'], 5)))
                           for k, c in curves.items())
    if changes and target.get('version') is not None:
        changes.append(dict(key=f'{prefix}.version', value=target['version']))
    cross_median = {}
    for axis, ratios in cross.items():
        columns = list(zip(*ratios))
        cross_median[axis] = [round(statistics.median([v for v in c if v is not None]), 3)
                              if any(v is not None for v in c) else None for c in columns]
    summary = (' | '.join(parts) or 'no axis had two levels') + \
        (f' | not fitted: {", ".join(skipped)}' if skipped else '') + f' ({ended}, {len(cover.holds)} holds)'
    return dict(summary=summary, changes=changes, live=list(target.get('live') or []),
                measured=dict(points=points, t95=rises_all, drift=drift, still_s=round(cover.neutral_s, 1),
                              cross=cross_median, coverage=round(cover.fraction(), 3), ended=ended,
                              missing=cover.missing()))
