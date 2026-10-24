"""Check docs/config-conventions.md against the code that consumes it."""
from pathlib import Path
import re
import unittest

from astro_run_config import compile_config

from mhseals_config import catalog, resolver
from mhseals_config.catalog import DescriptorBundle

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / 'docs/config-conventions.md').read_text()
TIERS = ['live', 'reconfigure', 'restart', 'rebuild', 'reboot', 'new-session', 'next-use']
LAYERS = ['package', 'deployment', 'profile', 'operator', 'session', 'patch', 'trial',
          'derived']


def read_catalog(text):
    """Load a run catalog through the real validating loader."""
    resources = {'run-defaults': (ROOT / 'config/run-defaults.yaml').read_bytes()}
    return DescriptorBundle.read_yaml(
        text, resources, {'astro.static-linux.compiler': compile_config},
        {'astro.static-linux': '0.1.0'})


class ConventionDocTest(unittest.TestCase):
    """The convention page agrees with the code that consumes it."""

    def test_short_and_numbered(self):
        """The page stays one page with R1..R4."""
        self.assertLess(len(DOC.splitlines()), 80)
        self.assertEqual(re.findall(r'^## (R\d+) ', DOC, re.M), ['R1', 'R2', 'R3', 'R4'])

    def test_layers_match_consumers(self):
        """Layer names exist in the catalog and the resolver order."""
        body = re.search(r'## R3 Layers and precedence\n(.*?)\n\n', DOC, re.S).group(1)
        found = re.findall(r'`([a-z]+)`', body)[:len(LAYERS)]
        self.assertEqual(found, LAYERS)
        self.assertLessEqual(set(LAYERS), catalog._LAYERS)
        self.assertEqual(resolver.ORDER, ('defaults',) + tuple(found[1:5]) + ('trial',))

    def test_tier_names(self):
        """The tier table lists the agreed names in order."""
        self.assertEqual(re.findall(r'^\| (?:\d|-) \| ([a-z-]+) \|', DOC, re.M), TIERS)

    def test_shipped_catalog_tiers_valid_and_new_session_refused(self):
        """The real loader accepts the shipped catalog but not new-session yet."""
        text = (ROOT / 'config/run-catalog.yaml').read_text()
        read_catalog(text)
        with self.assertRaises(ValueError):
            read_catalog(text.replace('candidate_tier: 1', 'candidate_tier: new-session', 1))


if __name__ == '__main__':
    unittest.main()
