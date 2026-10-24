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
                     'nav: {frequency: true}', 'nav: {limits: &a [*a]}',
                     'nav: {limits: [{x: 1, x: 2}]}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse(text, 'fixture.yaml', 'operator', FIELDS)
        with self.assertRaisesRegex(ValueError, 'line=1.*line=2'):
            parse('timeout: .5\ncommand_mux: {timeout: .5}', 'x', 'operator', FIELDS)

    def test_empty_maps_alias_use_and_immutable_fields(self):
        for text in ('unknown: {}', 'nav: {frequency: {}}', 'nav: []'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse(text, 'shape.yaml', 'operator', FIELDS)
        self.assertEqual(parse('{}', 'x', 'operator', FIELDS), ())
        self.assertEqual(parse('nav: {}', 'x', 'operator', FIELDS), ())
        with self.assertRaisesRegex(ValueError, 'line=1.*line=2'):
            parse('timeout: &x 0.5\ncommand_mux: {timeout: *x}', 'x', 'operator', FIELDS)
        assignments = parse('timeout: &x 0.5\nreverse: *x', 'x', 'operator', FIELDS)
        self.assertEqual([a.origin.line for a in assignments], [1, 2])
        self.assertEqual([a.value for a in assignments], [.5, .5])
        aliases, layers = ['old'], ['operator']
        field = Field(float, aliases=aliases, layers=layers)
        aliases.append('new')
        layers.append('session')
        self.assertEqual(field.aliases, ('old',))
        self.assertEqual(field.layers, ('operator',))
        for bad in (dict(aliases=[1]), dict(layers=['unknown'])):
            with self.assertRaises(ValueError):
                Field(float, **bad)
        with self.assertRaises(ValueError):
            parse('new: 0.5', 'x', 'operator', {'value': field})
        with self.assertRaises(ValueError):
            parse('old: 0.5', 'x', 'session', {'value': field})
