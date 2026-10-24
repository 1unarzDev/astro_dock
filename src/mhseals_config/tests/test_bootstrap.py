import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from mhseals_config import BaselineRevision
from mhseals_config._bootstrap import begin_bootstrap, finish_bootstrap, read_active, read_candidate


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name

    def begin(self, scope='s', **kwargs):
        return begin_bootstrap(self.root, scope, BaselineRevision('b', ()),
                               {'nav': {'frequency': 20}}, {'nav': ['behavior']},
                               source_identity='sourcehash', participant_identities={'nav': 'pid/boot'}, **kwargs)

    def test_empty_root_then_verified_passive(self):
        with self.assertRaises(FileNotFoundError):
            read_active(self.root, 's')
        ticket = self.begin()
        self.assertFalse((Path(self.root) / 's/active.json').exists())
        self.assertEqual(read_candidate(self.root, ticket)['closure'], 'passive')
        receipt = finish_bootstrap(self.root, ticket, {'nav': dict(identity='pid/boot',
                                  source='sourcehash', candidate=ticket.candidate,
                                  checks={'behavior': True})})
        self.assertEqual(receipt['status'], 'verified_startup')
        self.assertEqual(read_active(self.root, 's').session.session, 's')
        with self.assertRaises(FileExistsError):
            self.begin()

    def test_unknown_corrupt_owned_and_output_refuse(self):
        ticket = self.begin()
        self.assertEqual(finish_bootstrap(self.root, ticket, {})['status'], 'initialized_unverified')
        with self.assertRaises(FileNotFoundError):
            read_active(self.root, 's')
        with self.assertRaises(ValueError):
            self.begin('other', closure='output_open')
        path = Path(self.root) / 'owned'
        path.mkdir()
        (path / 'owner.json').write_text('{}')
        with self.assertRaises(FileExistsError):
            self.begin('owned')
        path = Path(self.root) / 'corrupt'
        path.mkdir()
        (path / 'active.json').write_text('{')
        with self.assertRaises(json.JSONDecodeError):
            self.begin('corrupt')

    def test_fsync_failure_retains_reservation_and_artifact_corruption(self):
        with patch('mhseals_config._bootstrap.os.fsync', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                self.begin()
        ticket = self.begin('other')
        (Path(self.root) / 'other/initializing/nav.json').write_text('{}')
        with self.assertRaises(ValueError):
            read_candidate(self.root, ticket)
