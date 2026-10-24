"""Pure first-closure fixtures; these are not package-owned descriptors."""
import copy
import hashlib
import unittest

import yaml

from mhseals_config.catalog import DescriptorBundle


RAW = b'command_timeout: 0.5\ncontroller_frequency: 20.0\ndecoupling_negative: null\n'
SHA = hashlib.sha256(RAW).hexdigest()


def fixture():
    leaves = []
    for key, alias in [('hardware.command_timeout', 'timeout'),
                       ('nav.controller_frequency', 'frequency')]:
        pointer = '/' + key.split('.')[-1]
        leaves.append({
            'canonical_key': key, 'aliases': [alias],
            'default_ref': {'source_id': 'defaults', 'yaml_pointer': pointer,
                            'path': 'config/default.yaml', 'line': 1, 'source_hash': SHA},
            'value': {'type': 'number', 'dimensions': [], 'units': 'seconds' if alias == 'timeout' else 'Hz',
                      'constraint_ref': 'domain.constraints', 'validator_ref': 'domain.validate',
                      'inputs': [key]},
            'authoring': {'destination': 'config/operator.yaml', 'pointer': pointer,
                          'layers': ['package', 'operator', 'session'], 'absence': 'next-allowed-layer',
                          'null': 'reject', 'reset': 'pinned-default', 'scopes': ['baseline', 'session'],
                          'controlling_keys': []},
            'consumers': [{'id': 'fixture', 'version': 'build-1', 'parameters': [alias],
                           'compiler_ref': 'domain.compile', 'inputs': [key], 'outputs': [alias],
                           'selection_ref': 'domain.selected', 'dependencies': ['fixture'],
                           'candidate_tier': 1, 'owner_group': 'fixture'}],
            'safety': {name: 'domain.policy' for name in (
                'policy_ref', 'armed_ref', 'goal_ref', 'procedure_ref', 'hold_ref',
                'confirmation_ref', 'compensation_ref', 'state_ref')},
            'evidence': [{'recipe_ref': 'domain.verify', 'consumer': 'fixture',
                          'version': 'build-1', 'required_type': 'observation'}],
            'coverage': [{'source': 'defaults', 'consumer': 'fixture', 'status': 'traced',
                          'revision': 'source-sha', 'path': 'fixture.py', 'line': 1,
                          'consume_site': 'fixture.consume'}],
        })
    return {'schema_version': 1, 'owner_package': 'fixture', 'revision': 'fixture-1',
            'consumer_versions': {'fixture': 'build-1'},
            'default_sources': [{'id': 'defaults', 'resource': 'config/default.yaml', 'sha256': SHA}],
            'leaves': leaves}


REGISTRY = {name: lambda: None for name in ('domain.constraints', 'domain.validate',
            'domain.compile', 'domain.selected', 'domain.policy', 'domain.verify')}


def read(data=None, raw=RAW, versions=None):
    return DescriptorBundle.read(data if data is not None else fixture(),
                                 {'defaults': raw}, REGISTRY,
                                 versions if versions is not None else {'fixture': 'build-1'})


class CatalogTests(unittest.TestCase):
    def test_roundtrip_defaults_and_no_authority(self):
        data = fixture()
        bundle = DescriptorBundle.read_yaml(yaml.safe_dump(data), {'defaults': RAW}, REGISTRY,
                                            {'fixture': 'build-1'})
        self.assertEqual([row.default for row in bundle.rows], [.5, 20.0])
        self.assertEqual(dict(bundle.descriptor), dict(read().descriptor))
        self.assertEqual(bundle.rows[0].blockers, ('runtime-authority-not-provided',))
        self.assertFalse(bundle.rows[0].writable)
        with self.assertRaises(TypeError):
            bundle.rows[0].descriptor['canonical_key'] = 'other'
        with self.assertRaises(ValueError):
            bundle.field('nav.controller_frequency.unknown_descendant')
        self.assertNotIn('resolved_default', yaml.safe_dump(data))

    def test_incomplete_inventory_is_explicit(self):
        data = fixture()
        data['leaves'] = [{'canonical_key': 'hardware.unknown', 'aliases': []}]
        row = read(data).rows[0]
        self.assertIn('missing-default-authority', row.blockers)
        self.assertIn('missing-consumers', row.blockers)
        self.assertFalse(row.writable)
        data = fixture()
        data['leaves'][0]['evidence'][0]['version'] = 'old-build'
        self.assertIn('unresolved-evidence-version:fixture', read(data).rows[0].blockers)
        data['leaves'][0]['coverage'][0]['status'] = 'discovered'
        self.assertIn('inventory-coverage:discovered', read(data).rows[0].blockers)
        data = fixture()
        data['leaves'][0]['safety']['state_ref'] = 'unknown'
        self.assertIn('unresolved-safety.state_ref', read(data).rows[0].blockers)
        self.assertTrue(any('unresolved-consumer-version' in b for b in read(versions={}).rows[0].blockers))

    def test_structural_rejections(self):
        changes = [
            lambda d: d.update(extra=True),
            lambda d: d.update(schema_version=True),
            lambda d: d['leaves'][0].update(extra=True),
            lambda d: d['leaves'][0]['value'].update(default=.5),
            lambda d: d['leaves'][0]['value'].update(type='imaginary'),
            lambda d: d['leaves'][1].update(aliases=['timeout']),
            lambda d: d['leaves'][0].update(aliases='timeout'),
            lambda d: d['leaves'][0]['default_ref'].update(source_id='absent'),
            lambda d: d['leaves'][0]['default_ref'].update(yaml_pointer='/absent'),
            lambda d: d['leaves'][0]['default_ref'].update(source_hash='0' * 64),
            lambda d: d['leaves'][0]['default_ref'].update(line=True),
            lambda d: d['leaves'][0]['consumers'][0].update(candidate_tier=True),
            lambda d: d['leaves'][0]['authoring'].update(layers=['invented']),
            lambda d: d['leaves'][0]['authoring'].update(controlling_keys=['nav.frequency']),
            lambda d: d['leaves'][0]['value'].update(dimensions=[True]),
            lambda d: d['leaves'][0]['value'].update(dimensions=[2]),
            lambda d: d['leaves'][0]['authoring'].update(null='invented'),
            lambda d: d['leaves'][0]['consumers'][0].update(inputs='arbitrary'),
            lambda d: d['default_sources'][0].update(resource='../default.yaml'),
        ]
        for change in changes:
            with self.subTest(change=change):
                data = copy.deepcopy(fixture())
                change(data)
                with self.assertRaises(ValueError):
                    read(data)
        with self.assertRaises(ValueError):
            read(versions={'fixture': 'build-2'})
        with self.assertRaises(ValueError):
            read(raw=RAW + b'changed: true\n')

    def test_strict_yaml_and_type_bool(self):
        for text in ('schema_version: 1\nschema_version: 1',
                     'schema_version: 1\n<<: {owner_package: fixture}'):
            with self.assertRaises(ValueError):
                DescriptorBundle.read_yaml(text, {}, {}, {})
        for raw in (b'command_timeout: true\n', b'command_timeout: .inf\n',
                    b'command_timeout: 1\ncommand_timeout: 1\n'):
            data = fixture()
            data['leaves'] = data['leaves'][:1]
            sha = hashlib.sha256(raw).hexdigest()
            data['default_sources'][0]['sha256'] = sha
            data['leaves'][0]['default_ref']['source_hash'] = sha
            with self.assertRaises(ValueError):
                read(data, raw)


if __name__ == '__main__':
    unittest.main()
