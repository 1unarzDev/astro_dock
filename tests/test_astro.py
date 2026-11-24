"""astro's engine: the pure building blocks (fit, response, apply, log, status) and its verb/help coverage."""
import datetime
import math
from pathlib import Path
import re
import sys
import tempfile
import unittest

import numpy
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from astro import cli  # noqa: E402
from astro.engine import apply, fit, log, results  # noqa: E402

AXES = ['surge', 'sway', 'yaw']
IDENTITY = [1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0]


class Fit(unittest.TestCase):
    def test_speed_curve_recovers_d1_d2(self):
        d1, d2 = 0.4, 0.25
        points = [(v, math.sqrt(d1 * v + d2 * v * v)) for v in (0.3, 0.7, 1.1)]
        curve = fit.speed_curve(points)
        self.assertAlmostEqual(curve['d1'], d1, places=6)
        self.assertAlmostEqual(curve['d2'], d2, places=6)
        self.assertAlmostEqual(fit.speed_at(curve['d1'], curve['d2'], points[1][1]), 0.7, places=6)

    def test_negative_term_is_dropped_and_the_other_refit(self):
        # Pure quadratic drag plus a concave bend: lstsq gives d1 < 0, so d1 drops and d2 alone is refit.
        points = [(v, math.sqrt(0.5 * v * v - 0.05 * v)) for v in (0.5, 1.0, 1.5)]
        curve = fit.speed_curve(points)
        self.assertEqual(curve['d1'], 0.0)
        self.assertGreater(curve['d2'], 0.0)
        single = fit.speed_curve([(0.8, 0.6)])            # one level: linear only
        self.assertEqual(single['d2'], 0.0)
        self.assertAlmostEqual(single['d1'], 0.36 / 0.8)

    def test_refine_converges_on_linear_coupling(self):
        y0, jac = numpy.array([0.12, -0.08]), numpy.array([[0.9, 0.2], [-0.1, 1.1]])
        measure = lambda c: tuple(y0 + jac @ numpy.array(c))       # noqa: E731
        out = fit.refine(measure, (0.0, 0.0), (0.01, 0.01), max_holds=5)
        self.assertTrue(out['converged'])
        numpy.testing.assert_allclose(out['column'], -numpy.linalg.solve(jac, y0), atol=1e-6)
        search = fit.Search((0.01, 0.01), max_holds=5)              # the stepwise form agrees
        while not search.done:
            search.add(measure(search.column))
        numpy.testing.assert_allclose(search.result()['column'], out['column'], atol=1e-9)

    def test_t95_of_a_first_order_rise(self):
        tau, t_on = 0.6, 2.0
        t = numpy.arange(0, 12, 0.01)
        y = numpy.where(t < t_on, 0.0, 1.3 * (1 - numpy.exp(-(t - t_on) / tau)))
        self.assertAlmostEqual(fit.t95(t, y, t_on, 12.0), 3 * tau, delta=0.02)

    def test_nav2_caps(self):
        rules = {'delay_s': 0.15, 'stop_distance_m': 1.0, 'ramp_lags': 3.0, 'yaw_tolerance': 0.25}
        t95 = {'surge': 1.5, 'sway': 1.8, 'yaw': 0.45}
        out = fit.nav2_limits({'surge': 9, 'sway': 0.1, 'yaw': 9}, {'surge': 9, 'sway': 9, 'yaw': 0.05},
                              t95, rules, AXES, 'yaw')
        lag = 0.5 + 0.15
        self.assertAlmostEqual(out['velocity'][0], round(1.0 / (lag * 2.5), 4))
        self.assertEqual(out['velocity'][1], 0.1)                             # measured is lower
        self.assertAlmostEqual(out['velocity'][2], round(0.25 / (2 * 0.3), 4))
        self.assertAlmostEqual(out['acceleration'][0], round(1.0 / (lag * 2.5) / (3 * lag), 4))
        self.assertEqual(out['acceleration'][2], 0.05)


class Response(unittest.TestCase):
    def test_drift_removed_at_each_heading(self):
        current = (0.2, -0.1)                       # world-frame drift
        def rows(body, yaw, t0):
            c, s = math.cos(yaw), math.sin(yaw)
            # body velocity seen = boat's own + the current rotated into the body
            vx = body[0] + c * current[0] + s * current[1]
            vy = body[1] - s * current[0] + c * current[1]
            return [(t0 + i * 0.1, vx, vy, 0.0, yaw) for i in range(20)]
        baseline = rows((0.0, 0.0), 0.3, 0)
        hold = rows((0.5, 0.02), 1.4, 5)            # the boat turned between baseline and hold
        out = fit.response(baseline, hold)
        numpy.testing.assert_allclose(out, [0.5, 0.02, 0.0], atol=1e-9)
        main, cross = fit.cross_response(out, 0, 1)
        self.assertAlmostEqual(cross[0], 0.04)

    def test_coasted_allows_a_steady_current(self):
        from astro.engine.procedures import coasted
        steady = [(i * 0.1, 0.2, -0.1, 0.0, 0.0) for i in range(20)]               # drifting, not coasting
        slowing = [(i * 0.1, 1.0 - 0.05 * i, 0.0, 0.0, 0.0) for i in range(20)]
        self.assertTrue(coasted(steady))
        self.assertFalse(coasted(slowing))


class Apply(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.state = self.dir / 'state.yaml'
        self.state.write_text(yaml.safe_dump({'other': 1}))
        self.defaults = {'decoupling': IDENTITY, 'decoupling_negative': None}
        self.check = f'{sys.executable} -c "import sys,yaml; d=yaml.safe_load(open(sys.argv[1])); ' \
                     f'sys.exit(abs(d[\'decoupling\'][3]) > 1)"'

    def test_column_and_key_write_backup_check_undo(self):
        changes = [dict(key='decoupling', column=0, value=[1.0, 0.04, -0.07]),
                   dict(key='decoupling_negative', column=0, value=[1.0, 0.1, 0.0], fallback='decoupling'),
                   dict(key='characterization.speed_curves.surge+', value={'d1': 0.4, 'd2': 0.2})]
        backup, diffs = apply.apply(self.state, changes, self.defaults, self.check, AXES,
                                    live=[{'node': '/n', 'param': 'decoupling'}])
        self.assertEqual(diffs[0], ('decoupling[:,surge]', [1.0, 0, 0], [1.0, 0.04, -0.07]))
        data = yaml.safe_load(self.state.read_text())
        self.assertEqual(data['decoupling'], [1.0, 0, 0, 0.04, 1.0, 0, -0.07, 0, 1.0])
        self.assertEqual(data['decoupling_negative'][3], 0.1)          # from decoupling, the fallback
        self.assertEqual(data['characterization']['speed_curves']['surge+'], {'d1': 0.4, 'd2': 0.2})
        self.assertEqual(data['other'], 1)
        self.assertTrue(backup.exists())
        name, live = apply.undo(self.state, self.check)
        self.assertEqual(name, backup.name)
        self.assertEqual(live, [{'node': '/n', 'param': 'decoupling'}])
        self.assertEqual(yaml.safe_load(self.state.read_text()), {'other': 1})
        with self.assertRaises(apply.Refused):
            apply.undo(self.state)

    def test_unchanged_writes_nothing(self):
        before = self.state.read_text()
        self.assertEqual(apply.apply(self.state, [dict(key='other', value=1)], {}, self.check), (None, []))
        self.assertEqual(self.state.read_text(), before)
        self.assertEqual(apply.backups(self.state), [])

    def test_refused_check_rolls_back(self):
        before = self.state.read_text()
        with self.assertRaisesRegex(apply.Refused, 'check refused'):
            apply.apply(self.state, [dict(key='decoupling', column=0, value=[1.0, 1.5, 0.0])],
                        self.defaults, self.check, AXES)
        self.assertEqual(self.state.read_text(), before)
        self.assertEqual([p for p in self.dir.iterdir() if p.name != 'state.yaml'], [])

    def test_first_apply_without_a_state_file_undoes_to_none(self):
        self.state.unlink()
        apply.apply(self.state, [dict(key='a.b', value=2)], {}, None)
        self.assertEqual(yaml.safe_load(self.state.read_text()), {'a': {'b': 2}})
        apply.undo(self.state)
        self.assertFalse(self.state.exists())


class Log(unittest.TestCase):
    def test_line_round_trip(self):
        when = datetime.datetime(2026, 11, 24, 13, 7, 2)
        line = log.format_line('apply', 'couple-surge  decoupling[:,surge] [1, 0, 0] -> [1, 0.04, -0.07]', when)
        self.assertTrue(line.startswith('13:07:02  apply   couple-surge'))
        self.assertEqual(log.parse_line(line),
                         ('13:07:02', 'apply', 'couple-surge  decoupling[:,surge] [1, 0, 0] -> [1, 0.04, -0.07]'))
        self.assertEqual(log.parse_line(log.format_line('mark', 'wind', when))[1:], ('mark', 'wind'))
        self.assertIsNone(log.parse_line('not a log line'))
        with tempfile.TemporaryDirectory() as tmp:
            log.write(tmp, 'mark', 'x', topic=None, echo=False)
            self.assertEqual(log.parse_line(log.read(tmp)[0])[1:], ('mark', 'x'))


    def test_epoch_follows_the_log_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = datetime.date(2026, 11, 24)
            log.day_dir(tmp, day).mkdir(parents=True)
            (log.day_dir(tmp, day) / '.tz').write_text('+0000\n')
            utc = datetime.datetime(2026, 11, 24, 17, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
            self.assertEqual(log.epoch(tmp, day, '17:12:00'), utc)
            (log.day_dir(tmp, day) / '.tz').write_text('-0600\n')
            self.assertEqual(log.epoch(tmp, day, '11:12:00'), utc)


class Status(unittest.TestCase):
    def test_classification_and_next(self):
        defaults = {'decoupling': IDENTITY}
        state = {'decoupling': [1.0, 0, 0, 0.04, 1.0, 0, 0, 0, 1.0]}
        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp, '2026-11-24')
            def save(test, at, **fields):
                results.save(day, dict(test=test, **fields), datetime.datetime(2026, 11, 24, *at))
            save('still', (13, 0, 0), status='done', summary='')
            save('couple-surge', (13, 2, 0), status='done', changes=[{'key': 'decoupling'}], applied='x')
            save('couple-sway', (13, 4, 0), status='done', changes=[{'key': 'decoupling'}])
            save('couple-sway', (13, 5, 0), status='aborted')
            save('speed-surge', (13, 6, 0), status='done', changes=[{'key': 'c'}],
                 upstream={'decoupling': IDENTITY})                    # ran before couple-surge's apply
            save('speed-sway', (13, 8, 0), status='done', changes=[{'key': 'c'}],
                 upstream={'decoupling': state['decoupling']})
            order = ['still', 'couple-surge', 'couple-sway', 'speed-surge', 'speed-sway', 'nav2']
            rows = results.statuses(order, tmp, state, defaults)
            self.assertEqual([r[1] for r in rows],
                             ['measured', 'applied', 'aborted', 'stale', 'measured', 'pending'])
            self.assertEqual(results.next_step(rows), 'astro test couple-sway')
            self.assertEqual(results.next_step(rows[3:]), 'astro test speed-surge')
            self.assertEqual(results.next_step(rows[4:]), 'astro apply speed-sway')


class FakeRunner:
    """A first-order boat on a fake clock: speed = level (per axis), tau 0.5 s, odometry at 50 Hz."""
    battery = None

    def __init__(self, *args):
        self.t, self.v, self.rows, self.command = 0.0, [0.0, 0.0, 0.0], [], [0.0, 0.0, 0.0]

    def now(self):
        return self.t

    def wait_ready(self):
        return True

    def gate_state(self, gate):
        return True, None

    def hold(self, command, seconds):
        t0 = self.t
        while self.t - t0 < seconds:
            self.t = round(self.t + 0.02, 6)
            self.v = [v + (c - v) * 0.02 / 0.5 for v, c in zip(self.v, command)]
            self.rows.append((self.t, *self.v, 0.0, 0.0, 0.0, 1000 + self.t))
        return t0, self.t

    def window(self, t0, t1):
        return [r for r in self.rows if t0 <= r[0] <= t1]

    def neutral(self):
        pass

    def close(self):
        pass


class Results(unittest.TestCase):
    def test_speed_result_has_software_and_holds(self):
        from astro.engine import procedures
        with tempfile.TemporaryDirectory() as tmp:
            tests = Path(tmp, 'tests.yaml')
            tests.write_text(yaml.safe_dump({
                'topics': {'effort': '/e', 'odom': '/o'}, 'axes': AXES,
                'spaces': {'large': {'amp': 0.5, 'levels': [0.3, 0.6]}},
                'tests': {'speed-surge': {'type': 'speed', 'axis': 'surge', 'baseline_s': 1, 'settle_s': 2,
                                          'measure_s': 1, 'coast_s': 1, 'apply': {'key': 'c'}}}}))
            ctx = procedures.Context(procedures.Spec(tests), Path(tmp, 'state.yaml'), tmp, make_runner=FakeRunner,
                                     out=lambda *a: None)
            ctx.software = {'repo': 'abc123', 'workspace': 'def456'}
            data = yaml.safe_load(procedures.run(ctx, 'speed-surge').read_text())
        self.assertEqual(data['software'], {'repo': 'abc123', 'workspace': 'def456'})
        self.assertEqual([data[k] is not None for k in ('test', 'date', 'time')], [True] * 3)
        self.assertEqual(list(data)[-1], 'holds')
        self.assertEqual(len(data['holds']), 4)
        hold = data['holds'][0]
        self.assertEqual(hold['command'], {'axis': 'surge', 'sign': 1, 'level': 0.3, 'efforts': [0.3, 0.0, 0.0]})
        lengths = {len(v) for v in hold['samples'].values()}
        self.assertEqual(len(lengths), 1)
        self.assertEqual(set(hold['samples']), {'t', 'vx', 'vy', 'wz', 'yaw', 't_sensor'})
        self.assertAlmostEqual(len(hold['samples']['t']) / (hold['end'] - hold['settle']), 30, delta=6)


def mixed_wrench(command, plus, minus):
    """The body wrench a command makes through a thruster stack's law: each axis decoupled alone through its
    signed column, mixed per thruster, thrust e|e| summed per thruster, desaturated; wrench = M^T T / 4."""
    mixer = ((-1.0, 1.0, 1.0), (-1.0, -1.0, -1.0), (1.0, -1.0, 1.0), (1.0, 1.0, -1.0))
    thrust = [0.0] * 4
    for k, value in enumerate(command):
        matrix = plus if value >= 0 else minus
        part = [matrix[3 * row + k] * value for row in range(3)]
        for i, row in enumerate(mixer):
            e = sum(g * p for g, p in zip(row, part))
            thrust[i] += e * abs(e)
    peak = max(1.0, *(math.sqrt(abs(f)) for f in thrust))      # uniform desaturation of the efforts
    efforts = [math.copysign(math.sqrt(abs(f)), f) / peak for f in thrust]
    thrust = [e * abs(e) for e in efforts]
    return [sum(mixer[i][k] * thrust[i] for i in range(4)) / 4 for k in range(3)]


class PlantRunner(FakeRunner):
    """FakeRunner whose steady velocity is a hull's response to the mixed wrench of the live matrices."""
    HULL = numpy.array([[1.0, 0.0, 0.0], [0.15, 1.0, 0.0], [-0.12, 0.0, 1.0]])   # cross coupling to fix

    def __init__(self, *args):
        super().__init__()
        self.live, self.calls = {'decoupling': list(IDENTITY), 'decoupling_negative': list(IDENTITY)}, []

    def set_params(self, node, values):
        self.calls.append(dict(values))
        self.live.update({k: list(v) for k, v in values.items()})

    def hold(self, command, seconds):
        wrench = mixed_wrench(command, self.live['decoupling'], self.live['decoupling_negative'])
        return super().hold(list(self.HULL @ numpy.array(wrench)), seconds)


class Couple(unittest.TestCase):
    def test_the_measured_column_applied_zeroes_the_cross_response(self):
        from astro.engine import procedures
        live = [{'node': '/driver', 'param': 'decoupling', 'key': 'decoupling'},
                {'node': '/driver', 'param': 'decoupling_negative', 'key': 'decoupling_negative'}]
        with tempfile.TemporaryDirectory() as tmp:
            tests = Path(tmp, 'tests.yaml')
            tests.write_text(yaml.safe_dump({
                'topics': {'effort': '/e', 'odom': '/o'}, 'axes': AXES, 'spaces': {'large': {'amp': 0.5}},
                'tests': {'couple-surge': {'type': 'couple', 'axis': 'surge', 'holds': 8, 'tolerance': [0.01, 0.01],
                                           'baseline_s': 1, 'settle_s': 4, 'measure_s': 2, 'min_response': 0.01,
                                           'apply': {'key': 'decoupling', 'signed': 'decoupling_negative',
                                                     'live': live}}}}))
            state = Path(tmp, 'state.yaml')
            state.write_text(yaml.safe_dump({'decoupling': IDENTITY, 'decoupling_negative': IDENTITY}))
            runner = PlantRunner()
            ctx = procedures.Context(procedures.Spec(tests), state, tmp, make_runner=lambda *a: runner,
                                     out=lambda *a: None)
            data = yaml.safe_load(procedures.run(ctx, 'couple-surge').read_text())
        self.assertEqual(runner.live, {'decoupling': IDENTITY, 'decoupling_negative': IDENTITY})   # restored
        self.assertEqual(set(runner.calls[0]), {'decoupling', 'decoupling_negative'})               # one request
        plus, minus = list(IDENTITY), list(IDENTITY)
        for change in data['changes']:
            matrix = plus if change['key'] == 'decoupling' else minus
            for row, value in enumerate(change['value']):
                matrix[3 * row] = value
        for sign in (1, -1):
            v = PlantRunner.HULL @ numpy.array(mixed_wrench([0.5 * sign, 0, 0], plus, minus))
            self.assertLess(abs(v[1] / v[0]), 0.012)
            self.assertLess(abs(v[2] / v[0]), 0.012)


class Lane(unittest.TestCase):
    def test_a_hold_taken_by_another_input_aborts(self):
        from astro.engine import procedures
        log = [(0.0, 'idle', 'idle'), (1.0, 'auto', ''), (5.0, 'rc', 'switch MANUAL'), (5.3, 'auto', '')]
        off, bad = procedures.off_lane(log, 2.0, 8.0, 'auto')
        self.assertAlmostEqual(off, 0.3)
        off, bad = procedures.off_lane(log[:3], 2.0, 8.0, 'auto')
        self.assertAlmostEqual(off, 3.0)
        self.assertEqual(bad, ('rc', 'switch MANUAL'))

        class Taken(FakeRunner):
            def hold(self, command, seconds):
                out = super().hold(command, seconds)
                if any(command):
                    self.lane_log.append((self.t - seconds, 'rc', 'switch MANUAL'))
                return out
        runner = Taken()
        runner.lane_log = []
        ctx = type('C', (), {'spec': type('S', (), {'lane': {'state': '/m', 'want': 'auto', 'fix': 'GUIDED'}})()})()
        runner.hold([0.3, 0, 0], 2)
        with self.assertRaisesRegex(procedures.Abort, 'input is rc .switch MANUAL.*GUIDED'):
            procedures.check_lane(ctx, runner, 0.0, 2.0)


class Verbs(unittest.TestCase):
    def test_help_on_a_dispatched_command_never_runs_it(self):
        import contextlib, io
        from unittest import mock
        with mock.patch('subprocess.call') as call, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['hot', 'REPO', '--help']), 0)
        call.assert_not_called()
        self.assertIn('hw.sh hot', out.getvalue())

    def test_astro_starts_without_numpy(self):
        import subprocess
        code = ("import sys; sys.modules['numpy'] = None; sys.path.insert(0, sys.argv[1]); "
                "from astro import cli; sys.exit(cli.main(['help', '--list']))")
        run = subprocess.run([sys.executable, '-c', code, str(ROOT / 'tools')], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('astro', run.stdout)

    def test_every_verb_has_a_help_entry_and_every_astro_entry_is_a_verb(self):
        packages, errors = cli.help_module().discover([cli.HELP.with_name('astro-help.yaml')])
        self.assertEqual(errors, [])
        runs = [c['run'] for c in packages[0]['commands'] if c['run'].startswith('astro ')]
        named = {re.match(r'astro (\S+)', run).group(1) for run in runs}
        self.assertEqual(named, set(cli.VERBS))
        rec = {run.split()[2] for run in runs if run.startswith('astro rec ')}
        self.assertEqual(rec, {'start', 'stop', 'ls'})


if __name__ == '__main__':
    unittest.main()
