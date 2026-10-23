#!/usr/bin/env python3
"""Check the zenoh bridge configs allow only odometry and match the README."""

from pathlib import Path
import json
import re
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
CONFIGS = ROOT / 'config' / 'ros-transport'
HOSTS = {'blastoise': 'squirtle', 'squirtle': 'blastoise'}


def load(host):
    """Parse a host's JSON5 config with the stdlib: strip comments, quote bare keys, drop trailing commas."""
    text = (CONFIGS / f'{host}.json5').read_text()
    text = re.sub(r'^\s*//.*$', '', text, flags=re.M)
    text = re.sub(r'([{,]\s*|^\s*)(\w+):', r'\1"\2":', text, flags=re.M)
    text = re.sub(r',(\s*[}\]])', r'\1', text)
    return json.loads(text)


def allowed(host, key):
    """Return the regexes in one allow list of a host's bridge config."""
    return load(host)['plugins']['ros2dds']['allow'][key]


class RosTransportTest(unittest.TestCase):
    """Keep vision_msgs off the Foxy to Jazzy bridge."""

    def test_only_odometry_is_allowed(self):
        for host, peer in HOSTS.items():
            self.assertEqual(allowed(host, 'publishers'),
                             [f'^/transport/{host}/odom$'])
            self.assertEqual(allowed(host, 'subscribers'),
                             [f'^/transport/{peer}/odom$'])

    def test_readme_table_matches(self):
        readme = (ROOT / 'README.md').read_text()
        listed = set(re.findall(r'^\| `(/transport/[^`]+)` \|', readme, re.M))
        self.assertEqual(listed, {f'/transport/{h}/odom' for h in HOSTS})

    def test_fastdds_requests_the_raised_receive_buffer(self):
        # rmem_max only caps a request; without receiveBufferSize sockets stay at rmem_default.
        sysctl = re.search(r'rmem_max=(\d+)', (ROOT / 'setup.linux.sh').read_text())
        udp = ET.parse(ROOT / 'config' / 'fastdds.xml').getroot().iter()
        sizes = [e.text for d in udp if d.tag.endswith('transport_descriptor')
                 and d.findtext('{*}transport_id', '') == 'mtu_udp'
                 for e in d if e.tag.endswith('receiveBufferSize')]
        self.assertEqual(sizes, [sysctl.group(1)])


if __name__ == '__main__':
    unittest.main()
