import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from mhseals_config import BaselineRevision
from mhseals_config._evidence import Recipe


def evaluate_nav(document, evidence):
    return (set(evidence) == {"period_ns"}
            and type(evidence["period_ns"]) is int
            and evidence["period_ns"] * document["frequency"] == 1_000_000_000)


EVALUATORS = {"fixture.nav/1": Recipe("fixture.nav/1", evaluate_nav)}
from mhseals_config._bootstrap import begin_bootstrap, finish_bootstrap, read_active, read_candidate


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name

    def begin(self, scope='s', **kwargs):
        return begin_bootstrap(self.root, scope, BaselineRevision('b', ()),
                               {'nav': {'frequency': 20}}, {'nav': 'fixture.nav/1'},
                               source_identity='sourcehash', participant_identities={'nav': 'pid/boot'}, evaluators=EVALUATORS, **kwargs)

    def test_empty_root_then_verified_passive(self):
        with self.assertRaises(FileNotFoundError):
            read_active(self.root, 's', evaluators=EVALUATORS)
        ticket = self.begin()
        self.assertFalse((Path(self.root) / 's/active.json').exists())
        self.assertEqual(read_candidate(self.root, ticket)['closure'], 'passive')
        receipt = finish_bootstrap(self.root, ticket, self.observation(ticket), evaluators=EVALUATORS)
        self.assertEqual(receipt['status'], 'verified_startup')
        self.assertEqual(read_active(self.root, 's', evaluators=EVALUATORS).session.session, 's')
        with self.assertRaises(FileExistsError):
            self.begin()

    def test_unknown_corrupt_owned_and_output_refuse(self):
        ticket = self.begin()
        self.assertEqual(finish_bootstrap(self.root, ticket, {})['status'], 'initialized_unverified')
        with self.assertRaises(FileNotFoundError):
            read_active(self.root, 's', evaluators=EVALUATORS)
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
        with patch('mhseals_config._store.os.fsync', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                self.begin()
        ticket = self.begin('other')
        (Path(self.root) / 'other/initializing/nav.json').write_text('{}')
        with self.assertRaises(ValueError):
            read_candidate(self.root, ticket)

    def test_write_fault_and_wrong_identity_never_promote(self):
        with patch('mhseals_config._store.Store.write', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                self.begin()
        with self.assertRaises(FileExistsError):
            self.begin()
        ticket = self.begin('wrong')
        observations = self.observation(ticket)
        observations['nav']['identity'] = 'stale-boot'
        self.assertEqual(finish_bootstrap(self.root, ticket, observations,
                                         evaluators=EVALUATORS)['verified'], [])
        self.assertFalse((Path(self.root) / 'wrong/active.json').exists())

    def test_exclusive_reservation_unknown_participant_and_wrong_scope(self):
        from dataclasses import replace
        ticket = self.begin()
        with self.assertRaises(FileExistsError):
            self.begin()
        with self.assertRaises(ValueError):
            finish_bootstrap(self.root, ticket, {'unknown': {}})
        with self.assertRaises(FileNotFoundError):
            read_candidate(self.root, replace(ticket, scope='wrong-scope'))
        with self.assertRaises(ValueError):
            read_candidate(self.root, replace(ticket, candidate='bad-checksum'))

    def observation(self, ticket):
        return {'nav': dict(identity='pid/boot', source='sourcehash', scope=ticket.scope,
                            candidate=ticket.candidate, recipe='fixture.nav/1',
                            evidence={'period_ns': 50_000_000})}

    def test_fabricated_partial_stale_or_wrong_scope_evidence(self):
        for index, change in enumerate(({'evidence': {'checks': {'invented': True}}},
                                        {'evidence': {}}, {'scope': 'wrong'},
                                        {'source': 'old'}, {'candidate': 'old'},
                                        {'recipe': 'invented/1'}, {'identity': 'old-boot'})):
            ticket = self.begin('case' + str(index))
            observation = self.observation(ticket)
            observation['nav'].update(change)
            receipt = finish_bootstrap(self.root, ticket, observation, evaluators=EVALUATORS)
            self.assertEqual(receipt['status'], 'initialized_unverified')
        with self.assertRaises(ValueError):
            begin_bootstrap(self.root, 'invented', BaselineRevision('b', ()),
                            {'nav': {}}, {'nav': 'invented/1'}, source_identity='s',
                            participant_identities={'nav': 'i'}, evaluators=EVALUATORS)

    def test_active_manifest_and_evidence_tampering(self):
        for index, target in enumerate(('receipt', 'trial', 'profile', 'role', 'evidence')):
            ticket = self.begin('tamper' + str(index))
            finish_bootstrap(self.root, ticket, self.observation(ticket), evaluators=EVALUATORS)
            directory = Path(self.root) / ticket.scope
            active_path = directory / 'active.json'
            active = json.loads(active_path.read_text())
            if target in ('receipt', 'evidence'):
                receipt_path = directory / 'initializing/receipt.json'
                receipt = json.loads(receipt_path.read_text())
                receipt['observations'] = {}
                receipt['verified'] = []
                receipt_path.write_text(json.dumps(receipt))
                if target == 'evidence':
                    from mhseals_config import digest
                    active['receipt'] = digest(receipt)
            elif target == 'trial':
                active['manifest']['trial'] = dict(trial='invented', session=ticket.scope,
                                                  claim='c', expires=9999999999)
            else:
                active['manifest']['session'][target] = 'invented'
            active_path.write_text(json.dumps(active))
            with self.assertRaises(ValueError):
                read_active(self.root, ticket.scope, evaluators=EVALUATORS)

    def test_no_follow_scope_reservation_artifacts_and_broken_markers(self):
        with tempfile.TemporaryDirectory() as outside:
            root = Path(self.root)
            (root / 'redirect').symlink_to(outside)
            with self.assertRaises(OSError):
                self.begin('redirect')
            self.assertEqual(list(Path(outside).iterdir()), [])
            for marker in ('active.json', 'owner.json'):
                scope = marker.split('.')[0]
                directory = root / scope
                directory.mkdir()
                (directory / marker).symlink_to(Path(outside) / 'missing')
                with self.assertRaises(ValueError):
                    self.begin(scope)
            ticket = self.begin()
            artifact = root / 's/initializing/nav.json'
            copied = Path(outside) / 'nav.json'
            copied.write_bytes(artifact.read_bytes())
            artifact.unlink()
            artifact.symlink_to(copied)
            with self.assertRaises(OSError):
                read_candidate(self.root, ticket)
            (root / 'reservation').mkdir()
            (root / 'reservation/initializing').symlink_to(outside)
            with self.assertRaises(FileExistsError):
                self.begin('reservation')
            self.assertEqual([p.name for p in Path(outside).iterdir()], ['nav.json'])

    def test_concurrent_bootstrap_reservation(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        barrier = Barrier(2)

        def attempt():
            barrier.wait()
            try:
                return self.begin()
            except FileExistsError:
                return 'refused'

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(results.count('refused'), 1)
        ticket = next(result for result in results if result != 'refused')
        self.assertEqual(read_candidate(self.root, ticket)['scope'], 's')
