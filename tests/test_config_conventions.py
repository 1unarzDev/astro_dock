"""Lint of docs/config-conventions.md plus readback against its real consumers."""
import re
import unittest
from collections.abc import Mapping
from pathlib import Path

from astro_run_config import compile_config

from mhseals_config import catalog, resolver
from mhseals_config.catalog import DescriptorBundle

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / 'docs/config-conventions.md').read_text()

UNITS = ['s', 'Hz', 'm', 'm/s', 'rad', 'rad/s', 'm/s^2', 'count', 'bool', 'enum',
         'frame', 'topic', 'path', '1']
LAYERS = ['package', 'deployment', 'profile', 'operator', 'session', 'patch', 'trial',
          'derived']


def tier_table():
    """Return (tier, name) pairs from the R9 table."""
    return re.findall(r'^\| (\d|-) \| ([a-z-]+) \|', DOC, re.M)


def tier_meanings():
    """Return {name: meaning} from the R9 table."""
    return dict(re.findall(r'^\| (?:\d|-) \| ([a-z-]+) \| (.+) \|$', DOC, re.M))


def catalog_tiers():
    """Read the shipped catalog through the real validating loader."""
    resources = {'run-defaults': (ROOT / 'config/run-defaults.yaml').read_bytes()}
    bundle = DescriptorBundle.read_yaml(
        (ROOT / 'config/run-catalog.yaml').read_text(), resources,
        {'astro.static-linux.compiler': compile_config}, {'astro.static-linux': '0.1.0'})
    tiers = []

    def walk(x):
        if isinstance(x, Mapping):
            for k, v in x.items():
                if k == 'candidate_tier':
                    tiers.append(v)
                walk(v)
        elif isinstance(x, (tuple, list)):
            for v in x:
                walk(v)
    walk(bundle.descriptor)
    return tiers


class ConventionDocTest(unittest.TestCase):
    """The convention page agrees with the code that consumes it."""

    def test_one_page_with_rule_ids(self):
        """The page stays short and carries R1..R12 in order."""
        self.assertLess(len(DOC.splitlines()), 140)
        ids = re.findall(r'^## (R\d+) ', DOC, re.M)
        self.assertEqual(ids, ['R%d' % i for i in range(1, 13)])

    def test_closed_unit_list(self):
        """The closed unit list is exactly the agreed one."""
        m = re.search(r'closed list and no other spelling[^:]*:\n\n`([^`]+)`', DOC)
        self.assertEqual([u.strip() for u in m.group(1).split(',')], UNITS)
        self.assertIn('FollowPath.TwirlingCritic', DOC)
        self.assertIn("catalog row's `unit`", DOC)

    def test_layer_names_in_order(self):
        """Layer names follow the doc order and exist in the real consumers."""
        m = re.search(r'## R5 Layers\n(.*?)\n\n', DOC, re.S)
        found = re.findall(r'`([a-z]+)`', m.group(1))[:len(LAYERS)]
        self.assertEqual(found, LAYERS)
        self.assertLessEqual(set(LAYERS), catalog._LAYERS)
        self.assertEqual(resolver.ORDER, ('defaults',) + tuple(found[1:5]) + ('trial',))
        self.assertIn('`defaults`', m.group(1))

    def test_tier_vocabulary(self):
        """Tier names and the spec section 7 meanings are present."""
        self.assertEqual(tier_table(), [
            ('1', 'live'), ('2', 'reconfigure'), ('3', 'restart'), ('4', 'rebuild'),
            ('5', 'reboot'), ('-', 'new-session'), ('-', 'next-use')])
        meaning = tier_meanings()
        for name, words in [('reconfigure', ('lifecycle', 'RESET/configure/STARTUP')),
                            ('restart', ('process or launch group',)),
                            ('rebuild', ('C++/interface dependency closure', 'versioned isolated prefix')),
                            ('reboot', ('FCU', 'safe owner window'))]:
            for w in words:
                self.assertIn(w, meaning[name])
        self.assertIn('never `supported` without an adapter and consumer evidence',
                      DOC.replace('\n', ' '))

    def test_tier_dependency_stated(self):
        """The validator gap and the derived classes are stated."""
        flat = DOC.replace('\n', ' ')
        self.assertIn('integer 1-5 or `unsupported`', flat)
        self.assertIn('A6-CORE', flat)
        self.assertIn('`unverified` is a derived class', flat)

    def test_cosmetic_policy_keeps_last_good(self):
        """Refused live reload keeps last good under both policies."""
        flat = DOC.replace('\n', ' ')
        self.assertIn('Under both policies a refused live reload keeps the last good config', flat)
        self.assertIn('only at startup or first load', flat)

    def test_operator_file_rule(self):
        """Operator file path and its override variable are documented."""
        self.assertIn('${XDG_CONFIG_HOME:-~/.config}/mhseals/<surface>.yaml', DOC)
        self.assertIn('MHSEALS_<SURFACE>_CONFIG', DOC)

    def test_shipped_run_catalog_uses_only_supported_tiers(self):
        """The real loader accepts the shipped catalog; its tiers are in the vocabulary."""
        allowed = {int(n) for n, _ in tier_table() if n != '-'} | {'unsupported'}
        tiers = catalog_tiers()
        self.assertTrue(tiers)
        for t in tiers:
            self.assertIn(t, allowed)

    def test_loader_refuses_undocumented_new_session(self):
        """new-session is not yet accepted by the validator (A6-CORE dependency)."""
        text = (ROOT / 'config/run-catalog.yaml').read_text().replace(
            'candidate_tier: 1', 'candidate_tier: new-session', 1)
        resources = {'run-defaults': (ROOT / 'config/run-defaults.yaml').read_bytes()}
        with self.assertRaises(ValueError):
            DescriptorBundle.read_yaml(
                text, resources, {'astro.static-linux.compiler': compile_config},
                {'astro.static-linux': '0.1.0'})


if __name__ == '__main__':
    unittest.main()
