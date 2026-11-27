"""astro and crane: a help.yaml command run through the sim backend gets the sim vessel's env (as astro sim does);
a missing crane is one line naming its install."""
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from astro import cli, config, remote  # noqa: E402


class FakeVessel:
    def __init__(self, name, domain, sim=None):
        self.name, self.domain, self.astro = name, domain, {'sim': sim} if sim else {}

    def env(self):
        return {'ASTRO_VESSEL': self.name, 'ROS_DOMAIN_ID': str(self.domain)}


class CraneDispatch(unittest.TestCase):
    SIM = FakeVessel('sim', 101, '${CRANE_DIR:-~/crane_ml}/bin/crane sim')
    BOAT = FakeVessel('boat-a', 42)

    def env_for(self, run):
        with mock.patch.object(remote, 'sim_vessel', return_value=self.SIM), \
                mock.patch.object(config, 'Vessel', return_value=self.BOAT), \
                mock.patch.dict('os.environ', {'CRANE_DIR': ''}):
            return cli.dispatch_env({'name': 'x', 'run': run})

    def test_sim_backend_commands_get_the_sim_vessels_env_and_others_this_machines(self):
        for run in ('${CRANE_DIR:-$HOME/crane_ml}/bin/crane sim up', '~/crane_ml/bin/crane sim list'):
            self.assertEqual(self.env_for(run)['ROS_DOMAIN_ID'], '101', run)
        for run in ('${CRANE_DIR:-$HOME/crane_ml}/bin/crane doctor', '~/crane_ml/bin/crane simulate', 'ros2 topic list'):
            self.assertEqual(self.env_for(run)['ROS_DOMAIN_ID'], '42', run)

    def test_a_missing_crane_names_its_install(self):
        err = io.StringIO()
        with mock.patch.object(cli, 'help_module') as module, contextlib.redirect_stderr(err):
            module.return_value.discover.return_value = ([], [])
            self.assertEqual(cli.dispatch(['crane', 'sim', 'up']), 2)
        self.assertIn('crane is not installed here', err.getvalue())
        self.assertIn('install.sh', err.getvalue())
        with mock.patch.object(remote, 'sim', side_effect=FileNotFoundError(2, 'No such file', '/x/bin/crane')), \
                mock.patch.object(config, 'Vessel', return_value=self.BOAT), contextlib.redirect_stderr(err):
            code = cli.main(['sim', 'up'])
        self.assertEqual(code, 2)
        self.assertIn('the sim backend is not installed (/x/bin/crane)', err.getvalue())


if __name__ == '__main__':
    unittest.main()
