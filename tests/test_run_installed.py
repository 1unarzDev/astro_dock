"""Normal wheel installation, unrelated cwd, and strict catalog default pointers."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import yaml
from mhseals_config.catalog import DescriptorBundle
from test_run_config import ROOT


class InstalledTest(unittest.TestCase):
    def test_catalog(self):
        catalog = (ROOT / 'config/run-catalog.yaml').read_text()
        bundle = DescriptorBundle.read_yaml(catalog,
            {'run-defaults': (ROOT / 'config/run-defaults.yaml').read_bytes()}, {},
            {'astro.static-linux': '0.1.0'})
        self.assertEqual([row.default for row in bundle.rows],
                         [42, 'SUBNET', '/home/roboboat/roboboat_ws/bags', 'static-fastdds'])
        self.assertTrue(all(not row.writable for row in bundle.rows))

    def test_wheels_outside_checkout_and_missing_core(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            wheels = tmp / 'wheels'
            wheels.mkdir()
            subprocess.run([os.sys.executable, '-m', 'pip', 'wheel', '--no-deps',
                            '--no-build-isolation', '--wheel-dir', str(wheels),
                            str(ROOT / 'src/mhseals_config'), str(ROOT / 'tools')],
                           cwd=tmp, check=True, capture_output=True)
            venv = tmp / 'venv'
            subprocess.run(['/usr/bin/python3', '-m', 'venv', '--system-site-packages', str(venv)],
                           check=True, capture_output=True)
            pip = str(venv / 'bin/pip')
            tool = next(wheels.glob('astro_run_config-*.whl'))
            core = next(wheels.glob('mhseals_config-*.whl'))
            subprocess.run([pip, 'install', '--no-deps', str(tool)], cwd=tmp,
                           check=True, capture_output=True)
            missing = subprocess.run([str(venv / 'bin/astro-run-config'), '--help'],
                                     cwd=tmp, capture_output=True, text=True)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn('Install mhseals_config', missing.stderr)
            subprocess.run([pip, 'install', '--no-deps', str(core)], cwd=tmp,
                           check=True, capture_output=True)
            result = subprocess.run([str(venv / 'bin/python'), '-c',
                'import astro_run_config as a; import mhseals_config.resolver as r; '
                'assert "site-packages" in a.__file__; assert "site-packages" in r.__file__; assert len(a.catalog_bundle().rows) == 4'],
                cwd=tmp, check=True, capture_output=True)
            selection = tmp / 'run.yaml'
            selection.write_text('ros: {domain_id: 73}')
            adapter = tmp / 'adapter.yaml'
            adapter.write_text('services: {dev: {}}')
            generated = subprocess.run([str(venv / 'bin/astro-run-config'), '--config', str(selection),
                '--defaults', str(ROOT / 'config/run-defaults.yaml'), '--adapter', str(adapter),
                '--transport', str(ROOT / 'config/fastdds.xml'), '--output', str(tmp / 'override.yml')],
                cwd=tmp, env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True, check=True)
            verified = subprocess.run([str(venv / 'bin/astro-run-config'), '--verify', generated.stdout.strip()],
                                     cwd=tmp, env={'PATH': '/usr/bin:/bin'}, check=True,
                                     capture_output=True, text=True)
            self.assertIn('ROS_DOMAIN_ID\t73\n', verified.stdout)
