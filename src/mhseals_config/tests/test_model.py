import unittest
from mhseals_config import Artifact, BaselineRevision, Manifest, SessionSelection, digest


class ModelTests(unittest.TestCase):
    def test_scope(self):
        b = BaselineRevision('b', (Artifact('input.json', digest({'a': 1})),))
        with self.assertRaises(ValueError):
            Manifest(b, effective=b.artifacts)
        with self.assertRaises(ValueError):
            Manifest(b, SessionSelection('s', 'other', 'overlay'))
        with self.assertRaises(ValueError):
            Manifest.read({'schema': 2})
        self.assertEqual(Manifest(b).baseline, b)
