import unittest
from mhseals_config.resolver import Field, parse, resolve, Assignment, RESET

FIELDS = {'command_mux.timeout': Field(float, aliases=('timeout',)),
          'thrusters.matrix': Field(list), 'nav.frequency': Field(int),
          'nav.limits': Field(list), 'reverse': Field(float, nullable=True)}


class ResolverTests(unittest.TestCase):
    def test_two_domain_shapes_and_scopes(self):
        def layer(text, name):
            return parse(text, name + '.yaml', name, FIELDS)
        defaults = layer('command_mux: {timeout: 0.5}\nthrusters: {matrix: [[1, 0]]}\n'
                         'nav: {frequency: 20, limits: [1, 2]}\nreverse: null', 'defaults')
        operator = layer('nav: {limits: [3]}', 'operator')
        session = layer('timeout: 0.8', 'session')
        r = resolve((defaults, operator, session))
        self.assertEqual(r.baseline['command_mux.timeout'], .5)
        self.assertEqual(r.effective['command_mux.timeout'], .8)
        self.assertEqual(r.effective['nav.limits'], (3,))
        self.assertEqual(len(r.origins['command_mux.timeout']), 2)
        reset = Assignment('command_mux.timeout', RESET, session[0].origin)
        self.assertEqual(resolve((defaults, (reset,))).effective['command_mux.timeout'], .5)
        with self.assertRaises(TypeError):
            r.baseline['nav.frequency'] = 1

    def test_rejections(self):
        for text in ('nav: {frequency: 1, frequency: 1}',
                     'timeout: .5\ncommand_mux: {timeout: .5}',
                     'timeout: .5\ncommand_mux: {timeout: .8}',
                     'nav: {<<: {frequency: 20}}', 'nav: &a {x: *a}',
                     'unknown: 1', 'timeout: null', 'timeout: .nan',
                     'nav: {frequency: true}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse(text, 'fixture.yaml', 'operator', FIELDS)
        with self.assertRaisesRegex(ValueError, 'line=1.*line=2'):
            parse('timeout: .5\ncommand_mux: {timeout: .5}', 'x', 'operator', FIELDS)
