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

    def test_artifact_digest_is_an_immutable_string(self):
        valid = 'a' * 64
        for invalid in (list(valid), tuple(valid), valid.encode(), bytearray(valid.encode()),
                        None, 64, True, '', 'a' * 63, 'A' * 64, 'g' * 64):
            with self.subTest(digest_type=type(invalid).__name__):
                with self.assertRaises(ValueError):
                    Artifact('input.json', invalid)
        artifact = Artifact('input.json', valid)
        self.assertEqual(artifact.sha256, valid)
        self.assertIsInstance(artifact.sha256, str)
