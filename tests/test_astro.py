"""astro's engine: the pure building blocks (fit, response, apply, log, status) and its verb/help coverage."""
import datetime
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
import unittest.mock

import numpy
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
os.environ['ASTRO_FIELD'] = 'off'          # never file field issues from tests

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

    def test_search_converges_when_cross_velocity_goes_as_sqrt_force(self):
        # quadratic drag: the cross velocity is sgn(F) sqrt|F| of a force linear in the column (sim couple-surge)
        f0, jac = numpy.array([0.0035, -0.0074]), numpy.array([[0.96, -0.70], [-0.25, 1.09]])
        measure = lambda c: tuple(math.copysign(math.sqrt(abs(f)), f) for f in f0 + jac @ numpy.array(c))  # noqa
        search = fit.Search((0.05, 0.05), max_holds=4, square=True)
        while not search.done:
            search.add(measure(search.column))
        self.assertTrue(search.result()['converged'])
        self.assertLessEqual(search.result()['holds'], 4)

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
        y[(t > 2.3) & (t < 2.32)] = 1.3                       # one noisy sample over the line early on
        self.assertAlmostEqual(fit.t95(t, y, t_on, 12.0), 3 * tau, delta=0.02)

    def test_nav2_caps(self):
        rules = {'delay_s': 0.15, 'stop_distance_m': 1.0, 'ramp_lags': 3.0, 'yaw_tolerance': 0.25}
        t95 = {'surge': 1.5, 'sway': 1.8, 'yaw': 0.45}
        out, why = fit.nav2_limits({'surge': 9, 'sway': 0.1, 'yaw': 9}, {'surge': 9, 'sway': 9, 'yaw': 0.05},
                                   t95, rules, AXES, 'yaw')
        self.assertEqual(why['sway']['velocity'], 'measured')
        self.assertIn('stop_distance_m', why['surge']['velocity'])
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
        now = datetime.datetime(2026, 11, 24, 13, 7, 2)            # two applies in the same instant
        first, _ = apply.apply(self.state, [dict(key='x', value=1)], {}, None, now=now)
        second, _ = apply.apply(self.state, [dict(key='x', value=2)], {}, None, now=now)
        self.assertNotEqual(first, second)
        with self.assertRaisesRegex(apply.Refused, 'check refused'):    # an undo the check refuses rolls back
            apply.revert(self.state, apply.history(self.state)[-1], self.defaults, self.check)
        self.assertEqual(yaml.safe_load(self.state.read_text())['x'], 2)

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
        apply.revert(self.state, apply.history(self.state)[0])
        self.assertEqual(yaml.safe_load(self.state.read_text()), {})

    def test_apply_records_its_keys_in_the_history(self):
        now = datetime.datetime(2026, 11, 24, 13, 7, 2)
        backup, _ = apply.apply(self.state, [dict(key='decoupling', column=0, value=[1.0, 0.04, -0.07]),
                                             dict(key='other', value=3)], self.defaults, None, AXES,
                                live=[{'node': '/n', 'param': 'decoupling'}], now=now, test='couple-surge',
                                result='/r/couple-surge-130700.yaml')
        [entry] = apply.history(self.state)
        self.assertEqual((entry['id'], entry['time'], entry['test']), (backup.name, '2026-11-24 13:07:02', 'couple-surge'))
        self.assertEqual(entry['live'], [{'node': '/n', 'param': 'decoupling'}])
        self.assertEqual([(k['label'], k['old'], k['new']) for k in entry['keys']],
                         [('decoupling[:,surge]', [1.0, 0, 0], [1.0, 0.04, -0.07]), ('other', 1, 3)])
        self.assertIsNone(entry['undone'])

    def test_undo_one_apply_leaves_the_others(self):
        apply.apply(self.state, [dict(key='a', value=1), dict(key='b', value=1)], {}, None, test='one')
        apply.apply(self.state, [dict(key='c', value=2)], {}, None, test='two')
        apply.apply(self.state, [dict(key='decoupling', column=1, value=[0.1, 1.0, 0.0])],
                    self.defaults, None, AXES, test='three')
        one = next(e for e in apply.history(self.state) if e['test'] == 'one')
        self.assertTrue(all(now == item['new'] and not later
                            for item, now, later in apply.plan(self.state, one, self.defaults)))
        done = apply.revert(self.state, one, self.defaults)
        self.assertEqual(sorted(d[0] for d in done), ['a', 'b'])
        data = yaml.safe_load(self.state.read_text())
        self.assertEqual({k: data.get(k) for k in ('a', 'b', 'c', 'other')}, {'a': None, 'b': None, 'c': 2, 'other': 1})
        self.assertEqual(data['decoupling'][1], 0.1)
        three = apply.history(self.state)[0]
        self.assertEqual(three['test'], 'three')
        self.assertTrue(apply.history(self.state)[2]['undone'])
        apply.revert(self.state, three, self.defaults)        # back to the defaults: the key goes again
        self.assertEqual(yaml.safe_load(self.state.read_text()), {'other': 1, 'c': 2})

    def test_a_key_changed_since_is_a_conflict_to_restore_or_skip(self):
        apply.apply(self.state, [dict(key='x', value=1), dict(key='y', value=1)], {}, None, test='first')
        apply.apply(self.state, [dict(key='x', value=2)], {}, None, test='second')
        first = apply.history(self.state)[1]
        rows = {item['label']: (now, [o['test'] for o in later]) for item, now, later in apply.plan(self.state, first)}
        self.assertEqual(rows, {'x': (2, ['second']), 'y': (1, [])})
        apply.revert(self.state, first, skip={'x'})              # skip: the later value stays
        data = yaml.safe_load(self.state.read_text())
        self.assertEqual((data.get('x'), data.get('y')), (2, None))
        apply.apply(self.state, [dict(key='x', value=1), dict(key='y', value=1)], {}, None, test='first')
        apply.apply(self.state, [dict(key='x', value=2)], {}, None, test='second')
        apply.revert(self.state, apply.history(self.state)[1])     # restore anyway
        self.assertEqual(yaml.safe_load(self.state.read_text()), {'other': 1, 'x': 2})  # x was 2 before 'first'

    def test_undo_last_and_list_from_the_cli(self):
        import contextlib
        import io
        from types import SimpleNamespace
        apply.apply(self.state, [dict(key='x', value=1)], {}, None, test='first')
        apply.apply(self.state, [dict(key='y', value=5)], {}, None, test='second')
        vessel = SimpleNamespace(state=lambda: self.state, astro={})
        session = SimpleNamespace(vessel=vessel, spec=SimpleNamespace(defaults=lambda: {}), root=self.dir / 'data',
                                  events=None)
        args = SimpleNamespace(last=True, listing=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), unittest.mock.patch.object(cli.config, 'have_ros', lambda: False):
            self.assertEqual(cli.cmd_undo(session, args), 0)
        self.assertRegex(out.getvalue(), r'^Undo: second, applied \d\d:\d\d today\n')
        self.assertIn('  y   now 5  \u2192  back to unset', out.getvalue())
        self.assertRegex(out.getvalue(), r'\nundone: second \(applied \d\d:\d\d\), restored in ')
        self.assertEqual(yaml.safe_load(self.state.read_text()), {'other': 1, 'x': 1})
        self.assertRegex((self.dir / 'data').glob('*/log.txt').__next__().read_text(),
                         r'apply +undone: second \(applied \d\d:\d\d:\d\d\): y \u2192 unset')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.cmd_undo(session, SimpleNamespace(last=False, listing=True)), 0)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].lstrip().startswith('second  '))
        self.assertIn('[undone', lines[0])
        self.assertIn('x unset \u2192 1  (1 key)', lines[1])


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
        self.assertNotIn('settle', hold)
        # the samples start with the neutral baseline; the fake stamps are 1000 s off the node clock: no t_sensor
        self.assertEqual(set(hold['samples']), {'t', 'vx', 'vy', 'wz', 'yaw'})
        self.assertAlmostEqual(hold['samples']['t'][0], hold['baseline'][0], delta=0.05)
        self.assertEqual((hold['baseline'][1] <= hold['start'] <= hold['measure'][0], hold['measure'][1]),
                         (True, hold['end']))
        self.assertAlmostEqual(len(hold['samples']['t']) / (hold['end'] - hold['baseline'][0]), 30, delta=6)

    def test_t_sensor_only_on_the_node_clock(self):
        from astro.engine import procedures
        ctx = type('C', (), {'holds': []})()
        rows = [(t / 30, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, t / 30 + 0.01) for t in range(30)]
        procedures.record(ctx, None, [0.1, 0, 0], 0.0, 1.0, rows)
        self.assertIn('t_sensor', ctx.holds[0]['samples'])

    def test_still_conditions(self):
        from astro.engine import procedures
        calm = [(t * 0.05, 0.1, 0.0, 0.001 * (-1) ** t, 0.0) for t in range(400)]   # a steady 0.1 m/s current
        found = procedures.conditions(calm, [0.004, 0.003, 0.001], fit.drift(calm), {}, 'abc123')
        self.assertEqual(found, {'verdict': 'ok', 'ceiling': 'L7', 'drift_trust': True, 'state': 'abc123'})
        gusty = [(t * 0.05, 0.05 if t < 200 else 0.25, 0.0, 0.0, 0.0) for t in range(400)]   # current picks up
        found = procedures.conditions(gusty, [0.1, 0.01, 0.01], fit.drift(gusty), {}, 'abc123')
        self.assertEqual((found['verdict'], found['ceiling'], found['drift_trust']), ('rough', 'L4', False))


class DriverRunner(FakeRunner):
    """FakeRunner a person drives: the script's (efforts, seconds) are what the mux applies, at 25 Hz."""
    SCRIPT = [([0, 0, 0], 12)] + [step for j in (0, 1, 2) for sign in (1, -1) for level in (0.2, 0.35, 0.5)
                                  for step in (([sign * level if k == j else 0 for k in range(3)], 4.5),
                                               ([0, 0, 0], 3))]

    def __init__(self, *args):
        super().__init__(*args)
        self.efforts, self.script = [], list(self.SCRIPT)

    def listen(self, topic):
        pass

    def hold(self, command, seconds):
        t0 = self.t
        while self.t - t0 < seconds:
            effort, left = self.script[0] if self.script else ([0, 0, 0], 1)
            self.efforts.append((self.t, *effort))     # the mux applies it, then the boat responds
            super().hold(effort, 0.04)
            if self.script:
                self.script[0] = (effort, left - 0.04)
                if self.script[0][1] <= 1e-9:
                    self.script.pop(0)
        return t0, self.t


class Watch(unittest.TestCase):
    def spec(self, tmp, axes):
        tests = Path(tmp, 'tests.yaml')
        tests.write_text(yaml.safe_dump({
            'topics': {'applied': '/a', 'odom': '/o'}, 'axes': axes,
            'tests': {'watch': {'type': 'watch', 'max_s': 300, 'apply': {'key': 'c', 'version': 1}}}}))
        return procedures_module().Spec(tests)

    def test_driving_covers_and_fits_the_speed_curves(self):
        procedures = procedures_module()
        with tempfile.TemporaryDirectory() as tmp:
            spec = self.spec(tmp, AXES)
            seen = []
            ctx = procedures.Context(spec, Path(tmp, 'state.yaml'), tmp, make_runner=DriverRunner, out=seen.append)
            data = yaml.safe_load(procedures.run(ctx, 'watch').read_text())
        self.assertEqual(data['measured']['coverage'], 1.0, seen)
        self.assertEqual(data['measured']['ended'], 'complete')
        changes = {c['key']: c['value'] for c in data['changes']}
        # the fake boat's steady speed equals its effort: e^2 = v^2, so d1 ~ 0 and d2 ~ 1
        self.assertAlmostEqual(changes['c.speed_curves.surge+']['d2'], 1.0, delta=0.05)
        self.assertLess(changes['c.speed_curves.surge+']['d1'], 0.05)
        self.assertAlmostEqual(changes['c.t95.surge'], 1.5, delta=0.15)     # the fake's tau 0.5 s: t95 = 3 tau
        self.assertEqual(len(data['holds']), 18)

    def test_cross_term_probes_against_a_turn_then_takes_the_secant(self):
        procedures = procedures_module()

        class Turning(DriverRunner):
            """Surging turns the boat: yaw rate -0.2 per unit surge effort, less 50 per unit of the decoupling's
            yaw-from-surge entry (what that entry does through the mixer), so the entry that zeroes it is 0.004."""
            entry = 0.0

            def hold(self, command, seconds):
                t0 = self.t
                while self.t - t0 < seconds:
                    effort, left = self.script[0] if self.script else ([0, 0, 0], 1)
                    self.efforts.append((self.t, *effort))
                    plant = list(effort)
                    plant[2] += effort[0] * (-0.2 + 50 * self.entry)
                    FakeRunner.hold(self, plant, 0.04)
                    if self.script:
                        self.script[0] = (effort, left - 0.04)
                        if self.script[0][1] <= 1e-9:
                            self.script.pop(0)
                return t0, self.t
        with tempfile.TemporaryDirectory() as tmp:
            tests = Path(tmp, 'tests.yaml')
            tests.write_text(yaml.safe_dump({
                'topics': {'applied': '/a', 'odom': '/o'}, 'axes': AXES, 'rotational': 'yaw',
                'tests': {'watch': {'type': 'watch', 'max_s': 300, 'max_turn': 1.0, 'upstream': ['decoupling'],
                                    'cross': {'key': 'decoupling', 'rows': ['yaw'], 'probe': 0.004},
                                    'apply': {'key': 'c'}}}}))
            state = Path(tmp, 'state.yaml')
            state.write_text(yaml.safe_dump({'decoupling': [1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0]}))
            ctx = procedures.Context(procedures.Spec(tests), state, tmp, make_runner=Turning, out=lambda *a: None)
            first = yaml.safe_load(procedures.run(ctx, 'watch').read_text())
            self.assertEqual(first['measured']['cross_terms']['yaw<-surge']['how'], 'probe')
            self.assertEqual([c['value'] for c in first['changes'] if c['key'] == 'decoupling'], [[1.0, 0.0, 0.004]])
            self.assertIn('score', first['quality'])
            state.write_text(yaml.safe_dump({'decoupling': [1.0, 0, 0, 0, 1.0, 0, 0.002, 0, 1.0]}))
            Turning.entry = 0.002                # a different entry in force: two points, so a secant
            second = yaml.safe_load(procedures.run(ctx, 'watch').read_text())
        term = second['measured']['cross_terms']['yaw<-surge']
        self.assertTrue(term['how'].startswith('secant'), term)
        self.assertAlmostEqual(term['next'], 0.004, delta=0.0005)

    def test_mixed_commands_are_not_holds(self):
        procedures = procedures_module()
        o = procedures.watch_options({})
        steady = [(t * 0.04, 0.3, 0.0, 0.0) for t in range(120)]
        mixed = [(t * 0.04, 0.3, 0.2, 0.0) for t in range(120)]
        self.assertEqual([r[0] for r in procedures.runs(steady, o)[0]], [])     # still running: not closed
        self.assertEqual(procedures.runs(steady + [(4.9, 0, 0, 0)], o)[0][0][:4], ('hold', 0, 1, 0.3))
        self.assertEqual(procedures.runs(mixed + [(4.9, 0, 0, 0)], o)[0], [])


def procedures_module():
    from astro.engine import procedures
    return procedures


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
                                           'baseline_s': 1, 'settle_s': 4, 'measure_s': 2, 'min_response': 0.01, 'drag': 'linear',
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
        self.assertEqual(rec, {'start', 'stop', 'ls', 'fetch'})


class Stacks(unittest.TestCase):
    SECTION = {'stacks': {
        'sensors': {'host': 'computer', 'run': 'ros2 launch p s.launch.py', 'ready': ['/odom']},
        'camera': {'host': 'camera', 'run': 'ros2 launch p c.launch.py', 'ready': ['/points']},
        'drive': {'host': 'computer', 'run': 'ros2 launch p d.launch.py', 'needs': ['sensors'],
                  'with': {'controller': 'controller:=true'}},
        'nav': {'host': 'computer', 'run': None, 'needs': ['drive'], 'default': False}}}

    def test_defaults_in_profile_order_and_names_pull_their_needs_first(self):
        from astro import stacks
        found = stacks.parse(self.SECTION)
        self.assertEqual([s.name for s in stacks.select(found)], ['sensors', 'camera', 'drive'])
        self.assertEqual([s.name for s in stacks.select(found, ['nav'])], ['sensors', 'drive', 'nav'])
        self.assertEqual(found['nav'].run, '')                         # run: null = started elsewhere
        self.assertEqual(stacks.command(found['drive'], ['controller']), 'ros2 launch p d.launch.py controller:=true')
        with self.assertRaisesRegex(Exception, "no stack 'navv'"):
            stacks.select(found, ['navv'])

    def test_a_commands_needs_start_needs_first_and_conflicts_stop(self):
        from astro import stacks
        found = stacks.parse(self.SECTION)
        down = {}
        stop, start, restart, options = stacks.plan(found, ['drive+controller', 'nope'],
                                                    [{'camera': 'it holds the pins'}, 'sensors'], down)
        self.assertEqual((stop, start, restart, options), ([], ['sensors', 'drive'], [], ['controller']))
        up = {'sensors': (True, {}), 'camera': (True, {}), 'drive': (True, {})}
        stop, start, restart, _ = stacks.plan(found, ['drive+controller'], [{'camera': 'it holds the pins'}], up)
        self.assertEqual((stop, start, restart), ([('camera', 'it holds the pins')], [], ['drive']))
        up['drive'] = (True, {'controller': True})
        self.assertEqual(stacks.plan(found, ['drive+controller', 'nav'], ['camera'], up),
                         ([('camera', '')], [], [], ['controller']))     # nav: run null, up only waits
        self.assertEqual(stacks.split_need('drive+controller'), ('drive', ['controller']))

    def test_ensure_says_why_then_stops_and_starts(self):
        import contextlib
        import io
        from unittest import mock
        from astro import stacks
        vessel = mock.Mock(astro=self.SECTION)
        states = {'sensors': (False, {}), 'camera': (True, {}), 'drive': (False, {})}
        out = io.StringIO()
        with mock.patch.object(stacks, 'states_of', return_value=states), \
                mock.patch.object(stacks, 'down') as down, mock.patch.object(stacks, 'up', return_value=0) as up, \
                contextlib.redirect_stdout(out):
            self.assertEqual(stacks.ensure(vessel, '/d', 'trial', ['drive+controller'], [{'camera': 'it holds X'}]), 0)
        self.assertEqual(out.getvalue().splitlines(), ['stopping camera (it holds X)',
                                                       'starting sensors (needed by trial)',
                                                       'starting drive (needed by trial)'])
        down.assert_called_once_with(vessel, '/d', ['camera'])
        up.assert_called_once_with(vessel, '/d', ['sensors', 'drive'], ['controller'])

    def test_inside_a_container_on_the_stacks_host_it_runs_here_and_unreached_hosts_are_skipped(self):
        import contextlib
        import io
        import subprocess as sp
        from unittest import mock
        from astro import config, remote, stacks
        vessel = mock.Mock(astro=self.SECTION)
        vessel.env.return_value = {}
        specs = {'computer': ('boat', {'ssh': True, 'container': ''}), 'camera': ('cam', {'ssh': True, 'container': ''})}
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if argv[0] == 'bash':
                return sp.CompletedProcess(argv, 0, 'astro-ps sensors down\nastro-ps drive up\n', '')
            raise FileNotFoundError('docker')
        out = io.StringIO()
        with mock.patch.object(remote, 'in_container', return_value=True), \
                mock.patch.object(config, 'this_host', return_value='boat'), \
                mock.patch.object(remote, 'place', side_effect=lambda v, h: specs[h]), \
                mock.patch.object(remote, 'words', return_value=(['ssh', 'cam', 'docker', 'exec'], None)), \
                mock.patch.object(stacks.subprocess, 'run', side_effect=run), contextlib.redirect_stdout(out):
            found = stacks.parse(self.SECTION)
            states = stacks.states_of(vessel, [found['sensors'], found['camera'], found['drive']], [])
        self.assertEqual(calls[0][:2], ['bash', '-c'])                  # this host: no docker exec
        self.assertEqual(states, {'sensors': (False, {}), 'drive': (True, {}), 'camera': None})
        self.assertEqual(out.getvalue(), 'cam not reached: skipping camera\n')
        self.assertEqual(stacks.plan(found, ['camera', 'drive'], ['camera'], states)[:2], ([], ['sensors']))

    def test_profile_errors_name_the_stack(self):
        from astro import config, stacks
        for bad, word in (({'a': {'run': 'x'}}, 'needs host'), ({'a': {'host': 'h', 'needs': ['b']}}, "needs 'b'"),
                          ({'a': {'host': 'h', 'rn': 'x'}}, 'unknown rn'),
                          ({'a': {'host': 'h', 'needs': ['b']}, 'b': {'host': 'h', 'needs': ['a']}}, 'needs itself')):
            with self.assertRaisesRegex(config.Error, word):
                stacks.select(stacks.parse({'stacks': bad}))

    def test_a_stack_starts_detached_in_its_hosts_container_over_ssh(self):
        import shlex
        from astro import remote, stacks
        vessel = type('V', (), {'name': 'boat', 'hosts_kind': {'container': {'name': 'dev'}},
                                'hosts': lambda self: HOSTS})()
        HOSTS = {'boat-computer': {'user': 'u', 'address': '10.0.0.5', 'ssh': True, 'container': '', 'via': '',
                                   'workspace': '', 'roles': ['computer']}}
        host, spec = remote.place(vessel, 'computer')                   # by role
        script = stacks.start_script('drive', 'ros2 launch p d.launch.py', stacks.data_expr({}))
        argv, env = remote.words(vessel, host, spec, script)
        self.assertEqual((host, env, argv[:4]), ('boat-computer', None, ['ssh', '-o', 'ConnectTimeout=8', 'u@10.0.0.5']))
        inner = shlex.split(argv[-1].split('docker exec -i ', 1)[1])
        self.assertEqual(inner[-4:-1], ['${c:-dev}', 'bash', '-c'])            # the whole script is one bash -c word
        self.assertIn('tmux -L astro new-session -d -s "$n"', inner[-1])  # never the default tmux server
        self.assertIn('setsid -f bash', inner[-1])
        self.assertIn('${ASTRO_DATA:-', inner[-1])
        self.assertEqual(stacks.data_expr({'data': 'bags/sim'}), '"${ROS_WS:-$PWD}"/bags/sim')

    def test_start_ps_stop_round_trip_in_a_shell(self):
        import shutil, subprocess
        from astro import stacks
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp, 'bin')                     # the tools the scripts use, but no tmux: setsid
            bin_dir.mkdir()
            for tool in ('bash', 'mkdir', 'date', 'cat', 'ps', 'tr', 'setsid', 'sleep', 'tail', 'sed', 'rm', 'tee'):
                (bin_dir / tool).symlink_to(shutil.which(tool))
            data, env = repr(tmp), {'PATH': str(bin_dir), 'HOME': tmp}
            def sh(script):
                return subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, timeout=30).stdout
            self.assertIn('astro-stack started', sh(stacks.start_script('s', 'echo hi; sleep 30', data)))
            self.assertIn('astro-stack running', sh(stacks.start_script('s', 'echo hi; sleep 30', data)))
            self.assertIn('astro-ps s up', sh(stacks.ps_script(['s'], data)))
            self.assertIn('astro-stack stopped', sh(stacks.stop_script('s', data)))  # SIGINT reaches it
            self.assertIn('astro-ps s down', sh(stacks.ps_script(['s'], data)))
            out = sh(stacks.start_script('bad', 'echo boom; exit 3', data))
            self.assertIn('astro-stack exited', out)
            self.assertIn('astro-log boom', out)


if __name__ == '__main__':
    unittest.main()
