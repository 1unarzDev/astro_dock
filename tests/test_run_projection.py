import json
from pathlib import Path
import tempfile
import unittest
import yaml
from astro_run_config import render, verify
from test_run_config import ROOT, WS


class ProjectionTest(unittest.TestCase):
    def test_frozen_projection_and_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            selection = tmp / 'selected.yaml'
            selection.write_text('ros: {domain_id: 73, discovery_range: LOCALHOST}\n'
                                 'data: {root: "' + WS + '/bags/experiment $x \'one\'"}')
            adapter = tmp / 'adapter.yaml'
            adapter.write_text('services: {dev: {environment: {DISPLAY: "${DISPLAY:-}"}}}')
            args = (ROOT / 'config/run-defaults.yaml', selection, {}, adapter,
                    ROOT / 'config/fastdds.xml', tmp / 'override.yml', 'linux')
            generation = render(*args)
            env = verify(generation)
            self.assertEqual(env['ROS_DOMAIN_ID'], '73')
            self.assertEqual(env['ROS_AUTOMATIC_DISCOVERY_RANGE'], 'LOCALHOST')
            self.assertEqual(env['MHSEALS_DATA'], WS + "/bags/experiment $x 'one'")
            composed = yaml.safe_load((tmp / 'override.yml').read_text())
            self.assertIn('$$x', composed['services']['dev']['environment']['MHSEALS_DATA'])
            old = (tmp / 'override.yml').read_bytes()
            selection.write_text('ros: {domain_id: true}')
            with self.assertRaises(ValueError):
                render(*args)
            self.assertEqual((tmp / 'override.yml').read_bytes(), old)
            self.assertEqual(verify(generation), env)
            with self.assertRaises(ValueError):
                verify(generation, {'ROS_DOMAIN_ID': '42'})
            (generation / 'source-selected.yaml').chmod(0o644)
            (generation / 'source-selected.yaml').write_text('{}')
            with self.assertRaises(ValueError):
                verify(generation)
