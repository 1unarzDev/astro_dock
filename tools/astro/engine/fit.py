"""The pure maths of the tests: responses, the column search, speed curves, rise time, step metrics, Nav2 caps.

Planar vehicle, three body axes in a fixed order (two linear, one rotational); their names come from the
tests file. A velocity row is (t, vx, vy, wz, yaw) with yaw None when the odometry has none.
"""
import math

import numpy


# ---- response: one hold over the neutral before it -----------------------------------------------

def response(baseline, hold):
    """Mean body velocity of ``hold`` minus the ``baseline`` drift, per body axis.

    The baseline's mean velocity is the drift (current, wind) and lives in the world frame: it is
    subtracted there from every hold sample, which is then rotated back into the body at its own heading,
    so a boat that turns during the hold does not read a steady current as a response. Without headings
    it is the plain body-frame difference."""
    if not baseline or not hold:
        raise ValueError(f'no velocity samples (baseline {len(baseline)}, hold {len(hold)})')
    wz = numpy.mean([r[3] for r in hold]) - numpy.mean([r[3] for r in baseline])
    if all(r[4] is not None for r in (*baseline, *hold)):
        def world(r):
            c, s = math.cos(r[4]), math.sin(r[4])
            return c * r[1] - s * r[2], s * r[1] + c * r[2]
        de, dn = numpy.mean([world(r) for r in baseline], axis=0)
        body = []
        for r in hold:
            e, n = world(r)
            c, s = math.cos(r[4]), math.sin(r[4])
            body.append((c * (e - de) + s * (n - dn), -s * (e - de) + c * (n - dn)))
        vx, vy = numpy.mean(body, axis=0)
    else:
        vx = numpy.mean([r[1] for r in hold]) - numpy.mean([r[1] for r in baseline])
        vy = numpy.mean([r[2] for r in hold]) - numpy.mean([r[2] for r in baseline])
    out = [float(vx), float(vy), float(wz)]
    if not all(math.isfinite(v) for v in out):
        raise ValueError('nonfinite response')
    return out


def drift(rows):
    """World-frame mean velocity (east, north) and its speed and heading (deg) over rows with yaw."""
    pts = [(math.cos(r[4]) * r[1] - math.sin(r[4]) * r[2], math.sin(r[4]) * r[1] + math.cos(r[4]) * r[2])
           for r in rows if r[4] is not None]
    if not pts:
        return None
    e, n = numpy.mean(pts, axis=0)
    return dict(east=float(e), north=float(n), speed=float(math.hypot(e, n)),
                heading_deg=float(math.degrees(math.atan2(n, e)) % 360))


def cross_axes(axis, n=3):
    return [k for k in range(n) if k != axis]


def cross_response(delta, axis, sign):
    """(main response along the command, cross responses per unit of it)."""
    main = max(sign * delta[axis], 1e-6)
    return main, tuple(delta[k] / main for k in cross_axes(axis, len(delta)))


# ---- the column search (ported as-is from the archived analysis/column.py) ------------------------

def worst(response, tolerance):
    """The largest cross response in units of its tolerance; <= 1 means every entry passes."""
    return max(abs(y) / t for y, t in zip(response, tolerance))


def _within(column, limit):
    """``column`` scaled down so sum|c| <= limit (a(1 + sum|c|) must stay in range); None if no room."""
    total = float(numpy.abs(column).sum())
    if limit <= 0:
        return None
    scale = min(1.0, limit / total) if total else 1.0
    return tuple(float(value * scale) for value in column)


def _next(points, probe, limit):
    """The next column to try, or None when the holds so far cannot determine one."""
    first = numpy.array(points[0][0], dtype=float)
    if len(points) == 1:
        return _within((first[0] + probe, first[1]), limit)
    if len(points) == 2:
        return _within((first[0], first[1] + probe), limit)
    design = numpy.array([[1.0, *column] for column, _ in points])
    fit = numpy.linalg.lstsq(design, numpy.array([y for _, y in points], dtype=float), rcond=None)[0]
    jacobian = fit[1:].T
    if abs(numpy.linalg.det(jacobian)) < 1e-9:
        return None
    column = -numpy.linalg.solve(jacobian, fit[0])
    return _within(column, limit)


def refine(measure, start, tolerance, probe=0.15, limit=1.0, max_holds=4, points=()):
    """Search for the column that zeroes the cross response.

    ``measure(column) -> (y1, y2)`` runs one hold. ``points`` are holds already measured
    ((column, response) pairs). At most ``max_holds`` new holds run. Returns the best measured
    column, its response, and whether it passes.
    """
    points, column = list(points), tuple(start)
    if points:
        if worst(points[0][1], tolerance) <= 1.0:
            return dict(column=points[0][0], response=points[0][1], holds=1, converged=True)
        column = _next(points, probe, limit)
    for _ in range(max_holds):
        if column is None:
            break
        points.append((column, tuple(measure(column))))
        if worst(points[-1][1], tolerance) <= 1.0:
            break
        column = _next(points, probe, limit)
    best = min(points, key=lambda point: worst(point[1], tolerance))
    return dict(column=best[0], response=best[1], holds=len(points),
                converged=bool(worst(best[1], tolerance) <= 1.0))


class Search:
    """``refine`` one hold at a time, so two signs can alternate (out and back)."""

    def __init__(self, tolerance, probe=0.15, limit=1.0, max_holds=4, start=(0.0, 0.0)):
        self.tolerance, self.probe, self.limit, self.max_holds = tolerance, probe, limit, max_holds
        self.points, self.column, self.done = [], tuple(start), False

    def add(self, response):
        self.points.append((self.column, tuple(response)))
        if worst(response, self.tolerance) <= 1.0 or len(self.points) >= self.max_holds:
            self.done = True
            return
        self.column = _next(self.points, self.probe, self.limit)
        self.done = self.column is None

    def result(self):
        best = min(self.points, key=lambda point: worst(point[1], self.tolerance))
        return dict(column=best[0], response=best[1], holds=len(self.points),
                    converged=bool(worst(best[1], self.tolerance) <= 1.0))


def effective_column(plus, minus, command, axis, sign, amplitude):
    """The decoupling column one command actually drives (flat row-major n x n matrices, the plus one
    for a component >= 0, the minus one below), per unit of the main effort."""
    n = len(command)
    out = numpy.zeros(n)
    for k, value in enumerate(command):
        matrix = plus if value >= 0 else minus
        out += numpy.array([matrix[n * row + k] for row in range(n)]) * value
    return [float(v) for v in out / (sign * amplitude)]


def command_for(axis, sign, amplitude, column, n=3):
    """Efforts: the main axis at sign*amplitude plus the column's cross efforts (e_k = c * e_main)."""
    command = [0.0] * n
    command[axis] = sign * amplitude
    for k, value in zip(cross_axes(axis, n), column):
        command[k] = value * sign * amplitude
    return command


# ---- speed curves (archived analysis/thrust.py) --------------------------------------------------

def speed_curve(points):
    """Fit w = e^2 = d1*v + d2*v|v| to (speed, effort) points; both terms stay >= 0.

    Returns None without a usable point. A negative term (noise, or a single level) is dropped and
    the other refitted, so the curve is always monotone."""
    points = [(v, e) for v, e in points if math.isfinite(v) and math.isfinite(e) and v > 0 and e > 0]
    if not points:
        return None
    v, e = (numpy.array(column, dtype=float) for column in zip(*points))
    e = e * e
    basis = (v, v * v)
    d1, d2 = numpy.linalg.lstsq(numpy.column_stack(basis), e, rcond=None)[0] \
        if len(points) > 1 and len(set(v)) > 1 else (0.0, -1.0)
    if d2 < 0 or d1 < 0:
        index = 1 if d1 < 0 and d2 >= 0 else 0
        coefficient = float(basis[index] @ e / (basis[index] @ basis[index]))
        d1, d2 = (coefficient, 0.0) if index == 0 else (0.0, coefficient)
    return dict(d1=float(d1), d2=float(d2), max_speed=float(v.max()),
                points=[[float(a), float(b)] for a, b in sorted(points)])


def speed_at(d1, d2, effort):
    """The steady speed ``effort`` holds on a curve: the positive root of d2 v^2 + d1 v = e^2."""
    magnitude = effort * effort
    if magnitude == 0:
        return 0.0
    root = math.sqrt(max(0.0, d1 * d1 + 4 * d2 * magnitude))
    return 2 * magnitude / (d1 + root) if d1 + root > 0 else 0.0


# ---- rise time and step metrics ------------------------------------------------------------------

def t95(t, y, t_on, t_cut):
    """Seconds after ``t_on`` until ``y`` first reaches 95 % of its change from the mean before ``t_on``
    to the mean over the hold's last third; None if never (a first-order rise gives 3 tau)."""
    t, y = numpy.asarray(t, float), numpy.asarray(y, float)
    before = y[t < t_on]
    base = float(before.mean()) if before.size else 0.0
    hold = (t >= t_on) & (t <= t_cut)
    tail = hold & (t >= t_cut - (t_cut - t_on) / 3)
    if not tail.any():
        return None
    steady = float(y[tail].mean())
    if steady == base:
        return None
    reached = numpy.nonzero(hold & ((y - base) / (steady - base) >= 0.95))[0]
    return float(t[reached[0]] - t_on) if reached.size else None


RISE_LOW, RISE_HIGH = 0.1, 0.9
SETTLING_BAND, SETTLING_FLOOR, TAIL_FRACTION = 0.05, 0.02, 0.2


def step_metrics(samples, target, start, end):
    """(time, value) samples of a step to ``target`` at ``start``: rise, overshoot, settling, SSE, IAE
    (archived analysis/step_response.py)."""
    window = [(t - start, v) for t, v in samples if start <= t <= end and v is not None]
    result = dict(target=target, samples=len(window), verdict='INCONCLUSIVE')
    if len(window) < 5 or target == 0:
        return result
    times = numpy.array([t for t, _ in window])
    sign, reference = numpy.sign(target), abs(target)
    values = sign * numpy.array([v for _, v in window])

    def crossing(level):
        above = numpy.nonzero(values >= level)[0]
        return float(times[above[0]]) if above.size else None
    low, high = crossing(RISE_LOW * reference), crossing(RISE_HIGH * reference)
    tolerance = max(SETTLING_BAND * reference, SETTLING_FLOOR)
    outside = numpy.nonzero(numpy.abs(values - reference) > tolerance)[0]
    if not outside.size:
        settling = 0.0
    elif outside[-1] + 1 < len(times):
        settling = float(times[outside[-1] + 1])
    else:
        settling = None
    tail = values[int(len(values) * (1 - TAIL_FRACTION)):]
    error = numpy.abs(reference - values)
    result.update(
        rise_time=high - low if low is not None and high is not None else None,
        overshoot=max(0.0, float(values.max() - reference) / reference),
        settling_time=settling,
        steady_state_error=float(reference - tail.mean()),
        iae=float(numpy.sum((error[1:] + error[:-1]) / 2 * numpy.diff(times))),
        verdict='SETTLED' if settling is not None else 'NOT SETTLED')
    return result


# ---- Nav2 caps (archived analysis/characterization.py rule_limits) --------------------------------

def rule_limits(rules, tau, rotational):
    """The limits the boat's lag allows, per axis: (velocity, acceleration). With lag T = tau + delay and
    ramps of k lags (a = v / (k T)), stopping takes v^2 / 2a + v T = v T (k / 2 + 1), which must fit in
    the stop distance D: v = D / (T (k / 2 + 1)). A turn overshoots by about wz T, kept to half the yaw
    tolerance: wz = tol / (2 T). ``tau`` maps axis -> s; ``rotational`` names the rotational axis."""
    k, delay = float(rules['ramp_lags']), float(rules['delay_s'])
    lag = {axis: float(value) + delay for axis, value in tau.items()}
    velocity = {axis: (float(rules['yaw_tolerance']) / (2 * lag[axis]) if axis == rotational
                       else float(rules['stop_distance_m']) / (lag[axis] * (k / 2 + 1))) for axis in lag}
    return velocity, {axis: velocity[axis] / (k * lag[axis]) for axis in lag}


def nav2_limits(velocity, acceleration, t95s, rules, axes, rotational):
    """{velocity: [..], acceleration: [..]} in axis order: what the boat showed, capped by its lag."""
    tau = {axis: float(t95s[axis]) / 3.0 for axis in axes}
    cap_v, cap_a = rule_limits(rules, tau, rotational)
    return dict(velocity=[round(min(float(velocity[a]), cap_v[a]), 4) for a in axes],
                acceleration=[round(min(float(acceleration[a]), cap_a[a]), 4) for a in axes])
