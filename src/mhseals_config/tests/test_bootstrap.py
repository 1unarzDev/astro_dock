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

    def test_captured_readers_immutable_and_single_read(self):
        from mhseals_config._bootstrap import read_bootstrap_inputs, read_selected_inputs
        from mhseals_config._store import Store
        ticket = self.begin()
        artifact = Path(self.root) / 's/initializing/nav.json'
        original_read, reads = Store.read, []

        def replace_after_capture(store, name):
            result = original_read(store, name)
            if name == 'initializing/nav.json':
                reads.append(name)
                artifact.write_text('{"frequency": 999}')
            return result

        for operation in (lambda: read_bootstrap_inputs(self.root, ticket),
                          lambda: finish_bootstrap(self.root, ticket, self.observation(ticket),
                                                   evaluators=EVALUATORS),
                          lambda: read_selected_inputs(self.root, 's', evaluators=EVALUATORS)):
            artifact.write_text('{"frequency": 20}')
            reads.clear()
            with patch.object(Store, 'read', replace_after_capture):
                result = operation()
            self.assertEqual(len(reads), 1)
            if hasattr(result, 'documents'):
                self.assertEqual(result.documents['nav']['frequency'], 20)
                self.assertEqual(result.ticket, ticket)
                with self.assertRaises(TypeError):
                    result.documents['nav']['frequency'] = 1
                with self.assertRaises(TypeError):
                    result.candidate['participants']['nav'] = 'other'
        self.assertEqual(result.manifest.session.session, 's')

    def test_reader_absence_and_required_input_faults(self):
        from dataclasses import replace
        from mhseals_config._bootstrap import (read_bootstrap_inputs, read_selected_inputs,
                                               NoActiveSelection)
        with self.assertRaises(NoActiveSelection):
            read_selected_inputs(self.root, 's')
        ticket = self.begin()
        self.assertIsNone(read_bootstrap_inputs(self.root, ticket).manifest)
        with self.assertRaises(ValueError):
            read_bootstrap_inputs(self.root, replace(ticket, candidate='wrong'))
        with self.assertRaises(FileNotFoundError):
            read_bootstrap_inputs(self.root, replace(ticket, scope='wrong'))
        finish_bootstrap(self.root, ticket, self.observation(ticket), evaluators=EVALUATORS)
        directory = Path(self.root) / 's/initializing'
        for name in ('nav.json', 'ticket.json', 'receipt.json'):
            path = directory / name
            content = path.read_bytes()
            path.unlink()
            try:
                read_selected_inputs(self.root, 's', evaluators=EVALUATORS)
                self.fail('missing required input accepted')
            except FileNotFoundError as error:
                self.assertNotIsInstance(error, NoActiveSelection)
            path.write_bytes(content)
        (directory / 'nav.json').write_text('{}')
        with self.assertRaises(ValueError):
            read_bootstrap_inputs(self.root, ticket)
        active = Path(self.root) / 's/active.json'
        active.unlink()
        active.symlink_to(Path(self.root) / 'missing')
        with self.assertRaises(OSError) as error:
            read_selected_inputs(self.root, 's', evaluators=EVALUATORS)
        self.assertNotIsInstance(error.exception, NoActiveSelection)

    def test_reader_nested_documents_and_invented_trial(self):
        from mhseals_config._bootstrap import read_bootstrap_inputs, read_selected_inputs
        documents = {'hardware': {'matrix': [[1, 0]]}, 'nav': {'frequency': 20}}
        ticket = begin_bootstrap(self.root, 'pair', BaselineRevision('b', ()), documents,
                                 dict.fromkeys(documents, 'fixture.nav/1'), source_identity='src',
                                 participant_identities=dict.fromkeys(documents, 'id'),
                                 evaluators=EVALUATORS)
        inputs = read_bootstrap_inputs(self.root, ticket)
        self.assertEqual(set(inputs.documents), set(documents))
        with self.assertRaises(TypeError):
            inputs.documents['hardware']['matrix'][0][0] = 2
        ticket = self.begin()
        finish_bootstrap(self.root, ticket, self.observation(ticket), evaluators=EVALUATORS)
        active = Path(self.root) / 's/active.json'
        data = json.loads(active.read_text())
        data['manifest']['trial'] = {'trial': 'invented'}
        active.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            read_selected_inputs(self.root, 's', evaluators=EVALUATORS)
