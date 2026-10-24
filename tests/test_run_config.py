import importlib.metadata
from pathlib import Path
import tempfile
import unittest

from astro_run_config import WS, compile_config

ROOT = Path(__file__).resolve().parents[1]


class CompilerTest(unittest.TestCase):
    def compile(self, text='', env=None):
        with tempfile.TemporaryDirectory() as tmp:
            selected = Path(tmp) / 'run.yaml'
            selected.write_text(text or '{}')
            return compile_config(ROOT / 'config/run-defaults.yaml', selected, env or {})

    def test_installed_and_session_provenance(self):
        self.assertEqual(importlib.metadata.version('astro-run-config'), '0.1.0')
        result = self.compile('ros: {domain_id: 73}', {'ROS_DOMAIN_ID': '81'})
        self.assertEqual(result['baseline']['ros.domain_id'], 73)
        self.assertEqual(result['effective']['ros.domain_id'], 81)
        self.assertEqual(result['origins']['ros.domain_id'][-1]['source'], 'ROS_DOMAIN_ID')

    def test_strict_inputs(self):
        for text in ['ros: {domain_id: true}', 'ros: {domain_id: null}',
                     'ros: {domain_id: 233}', 'ros: {domain_id: .inf}', 'bad: 1',
                     'ros: {domain_id: 3}\nros.domain_id: 4', 'ros: &r {domain_id: *r}', 'ros: {domain_id: 1, domain_id: 2}',
                     'ros: {<<: {domain_id: 3}}', 'data: {root: /tmp/bags}',
                     'data: {root: ' + WS + '/bags/../escape}', 'transport: {mode: cyclone}']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.compile(text)
        for env in [{'ROS_DOMAIN_ID': ''}, {'ROS_DOMAIN_ID': '3', 'ASTRO_ROS_DOMAIN_ID': '4'},
                    {'CYCLONEDDS_URI': ''}, {'ROS_DOMAIN_ID': 'true'}]:
            with self.subTest(env=env), self.assertRaises(ValueError):
                self.compile(env=env)

    def test_session_cannot_mask_invalid_baseline(self):
        for text, env in [
            ('ros: {domain_id: 999}', {'ROS_DOMAIN_ID': '73'}),
            ('ros: {discovery_range: INVALID}', {'ROS_AUTOMATIC_DISCOVERY_RANGE': 'SUBNET'}),
            ('data: {root: /tmp/bad}', {'MHSEALS_DATA': WS + '/bags/valid'}),
            ('data: {root: "' + WS + '/bags/$x"}', {'MHSEALS_DATA': WS + '/bags/valid'})]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.compile(text, env)
