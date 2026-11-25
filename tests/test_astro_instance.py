"""Parallel sim instances: ASTRO_SIM_INSTANCE=N maps a sim profile (one with astro.sim) to its own ROS domain,
containers, data dir and state file; instance 0 is the profile as written. A temporary profile set, no ROS."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from astro import config, remote  # noqa: E402


class Instance(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        sim = self.dir / 'profiles' / 'world'
        sim.mkdir(parents=True)
        (sim / 'network.env').write_text('LAN_SUBNET=127.0.0.0/8\nROS_DOMAIN_ID=101\n')
        (sim / 'devices.yaml').write_text('hosts:\n  localhost: {roles: [computer], address: 127.0.0.1, '
                                          'ssh: false, container: world_ros}\n')
        self.state = self.dir / 'cfg' / 'state.yaml'
        self.state.parent.mkdir()
        self.state.write_text('gain: 1\n')
        (sim / 'profile.yaml').write_text(
            'kind: vessel\nnetwork_env: network.env\nregistry: devices.yaml\nastro:\n'
            f'  state: {self.state}\n  state_env: WORLD_STATE\n  data: {self.dir / "data"}\n'
            '  sim: world-backend\n  instance_env: WORLD_INSTANCE\n')

    def vessel(self, n):
        env = {'ASTRO_PROFILES': str(self.dir / 'profiles'), 'ASTRO_SIM_INSTANCE': n}
        with mock.patch.dict(os.environ, env):
            os.environ.pop('ASTRO_PROFILE_DIR', None)
            vessel = config.Vessel('world')
            return vessel, vessel.env(), remote.local_env(vessel, False)

    def test_instance_0_is_the_profile_as_written(self):
        vessel, env, crossing = self.vessel('0')
        self.assertEqual(env['ROS_DOMAIN_ID'], '101')
        self.assertEqual(env['ASTRO_SIM_INSTANCE'], '0')
        self.assertEqual(env['WORLD_INSTANCE'], '0')
        self.assertEqual(vessel.hosts()['localhost']['container'], 'world_ros')
        self.assertEqual(vessel.data_root(), self.dir / 'data')
        self.assertEqual(vessel.state(), self.state)
        self.assertEqual(crossing['WORLD_STATE'], str(self.state))

    def test_instance_3_has_its_own_domain_container_data_and_state(self):
        vessel, env, crossing = self.vessel('3')
        self.assertEqual(env['ROS_DOMAIN_ID'], '104')
        self.assertEqual(env['ASTRO_SIM_INSTANCE'], '3')
        self.assertEqual(env['WORLD_INSTANCE'], '3')
        self.assertEqual(vessel.hosts()['localhost']['container'], 'world_ros_3')
        self.assertEqual(vessel.data_root(), self.dir / 'data' / 'i3')
        self.assertEqual(env['ASTRO_DATA'], str(self.dir / 'data' / 'i3'))
        own = self.state.with_name('state.i3.yaml')
        self.assertEqual(crossing['WORLD_STATE'], str(own))
        self.assertEqual(crossing['ASTRO_STATE'], str(own))
        self.assertEqual(vessel.state(), own)
        self.assertEqual(own.read_text(), 'gain: 1\n')          # copied from the base the first time
        own.write_text('gain: 2\n')
        self.assertEqual(vessel.state().read_text(), 'gain: 2\n')
        self.assertEqual(self.state.read_text(), 'gain: 1\n')   # the base stays

    def test_container_words_name_the_instance_container(self):
        vessel, _, _ = self.vessel('3')
        container = vessel.hosts()['localhost']['container']
        argv, _ = remote.local_words(vessel, container, 'true', False)
        self.assertIn('world_ros_3', argv)
        self.assertIn('ROS_DOMAIN_ID=104', argv)
        self.assertIn('WORLD_INSTANCE=3', argv)

    def test_a_non_sim_vessel_ignores_the_instance(self):
        profile = self.dir / 'profiles' / 'world' / 'profile.yaml'
        profile.write_text(profile.read_text().replace('  sim: world-backend\n', ''))
        vessel, env, _ = self.vessel('3')
        self.assertEqual(env['ROS_DOMAIN_ID'], '101')
        self.assertNotIn('ASTRO_SIM_INSTANCE', env)
        self.assertEqual(vessel.hosts()['localhost']['container'], 'world_ros')

    def test_a_bad_instance_is_an_error(self):
        with self.assertRaises(config.Error):
            self.vessel('two')

    def test_instance_path(self):
        self.assertEqual(config.instance_path('~/.config/x/hw_sim.yaml', 2), '~/.config/x/hw_sim.i2.yaml')
        self.assertEqual(config.instance_path('/a.b/state', 1), '/a.b/state.i1')
        self.assertEqual(config.instance_path('s.yaml', 0), 's.yaml')


if __name__ == '__main__':
    unittest.main()
