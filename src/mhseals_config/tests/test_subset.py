"""Cross-domain fixtures prove selection structure, not production behavior."""
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from mhseals_config import Artifact, BaselineRevision, digest
from mhseals_config._bootstrap import (begin_bootstrap, finish_bootstrap, read_selected_inputs,
                                      read_selected_subset, NoActiveSelection)
from mhseals_config._evidence import Recipe


def evaluate(document, evidence):
    return evidence == {'observed': document['value']}


class SubsetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self.path = Path(self.root) / 'session'
        self.docs = {p: {'value': i, 'nested': [[i]]} for i, p in enumerate(
            ('hardware-baseline', 'hardware-pure', 'nav2-baseline', 'nav2-compiler'))}
        self.recipes = {p: ('hardware/1' if p.startswith('hardware') else 'nav/1') for p in self.docs}
        self.evaluators = {r: Recipe(r, evaluate) for r in self.recipes.values()}
        baseline = BaselineRevision('b', tuple(Artifact(p + '.json', digest(d))
                                   for p, d in self.docs.items() if p.endswith('baseline')))
        self.ticket = begin_bootstrap(self.root, 'session', baseline, self.docs, self.recipes,
                         source_identity='aggregate', participant_identities=dict.fromkeys(self.docs, 'boot'),
                         evaluators=self.evaluators)
        self.observations = {p: dict(scope='session', candidate=self.ticket.candidate,
                             source='aggregate', identity='boot', recipe=self.recipes[p],
                             evidence={'observed': d['value']}) for p, d in self.docs.items()}
        finish_bootstrap(self.root, self.ticket, self.observations, evaluators=self.evaluators)

    def subset(self, domain='hardware', **kwargs):
        return read_selected_subset(self.root, 'session',
                    [p for p in self.docs if p.startswith(domain)],
                    evaluators={domain + '/1': self.evaluators[domain + '/1']}, **kwargs)

    def rewrite(self, relative, transform):
        path = self.path / relative
        data = json.loads(path.read_text())
        transform(data)
        path.write_text(json.dumps(data))

    def receipt(self, transform):
        self.rewrite('initializing/receipt.json', transform)
        receipt = json.loads((self.path / 'initializing/receipt.json').read_text())
        self.rewrite('active.json', lambda a: a.update(receipt=digest(receipt)))

    def test_union_strict_and_subsets(self):
        full = read_selected_inputs(self.root, 'session', evaluators=self.evaluators)
        hardware, nav = self.subset(expected_candidate=self.ticket.candidate), self.subset('nav')
        self.assertEqual(full.manifest, hardware.manifest)
        self.assertEqual(hardware.ticket, nav.ticket)
        self.assertEqual(set(nav.documents), set(self.docs))
        with self.assertRaises(TypeError):
            hardware.documents['nav2-compiler']['nested'][0][0] = 3
        with self.assertRaises(ValueError):
            read_selected_inputs(self.root, 'session', evaluators={'hardware/1': self.evaluators['hardware/1']})
        with self.assertRaises(ValueError):
            self.subset(expected_candidate='wrong')
        for participants in ([], ['unknown'], ['hardware-pure', 'hardware-pure']):
            with self.assertRaises(ValueError):
                read_selected_subset(self.root, 'session', participants, evaluators=self.evaluators)
        with self.assertRaises(NoActiveSelection):
            read_selected_subset(self.root, 'wrong', ['hardware-pure'], evaluators=self.evaluators)

    def test_foreign_corruption_and_partial_receipts(self):
        artifact = self.path / 'initializing/nav2-compiler.json'
        content = artifact.read_text()
        artifact.write_text('{}')
        with self.assertRaises(ValueError):
            self.subset()
        artifact.write_text(content)
        receipt_path = self.path / 'initializing/receipt.json'
        active_path = self.path / 'active.json'
        original_receipt, original_active = receipt_path.read_text(), active_path.read_text()
        changes = [lambda r: r['verified'].append('hardware-pure'),
                   lambda r: r['observations'].pop('nav2-compiler'),
                   lambda r: r.update(status='initialized_unverified')]
        for key in ('scope', 'candidate', 'source', 'identity', 'recipe'):
            changes.append(lambda r, key=key: r['observations']['nav2-compiler'].update({key: 'wrong'}))
        for change in changes:
            self.receipt(change)
            with self.assertRaises(ValueError):
                self.subset()
            receipt_path.write_text(original_receipt)
            active_path.write_text(original_active)
        self.rewrite('initializing/receipt.json', lambda r: r.update(verified=[]))
        with self.assertRaises(ValueError):
            self.subset()

    def test_own_evaluator_and_baseline_closure(self):
        self.receipt(lambda r: r['observations']['hardware-pure'].update(evidence={'observed': 999}))
        with self.assertRaises(ValueError):
            self.subset()
        # Foreign evidence is structurally checked, not independently reevaluated.
        self.subset('nav')
        with self.assertRaises(ValueError):
            read_selected_subset(self.root, 'session', ['hardware-pure'], evaluators={})
        candidate_path = self.path / 'initializing/candidate.json'
        candidate = json.loads(candidate_path.read_text())
        candidate['baseline']['artifacts'][0]['sha256'] = '0' * 64
        candidate_path.write_text(json.dumps(candidate))
        with self.assertRaises(ValueError):
            self.subset('nav')

    def test_rehashed_invalid_candidate_closure_and_baseline(self):
        from mhseals_config._bootstrap import _manifest, Ticket
        originals = {p: p.read_text() for p in self.path.rglob('*.json')}
        for change in (lambda c: c['recipes'].pop('nav2-compiler'),
                       lambda c: c['participants'].pop('nav2-compiler'),
                       lambda c: c['baseline']['artifacts'][0].update(sha256='0' * 64)):
            for path, text in originals.items():
                path.write_text(text)
            candidate_path = self.path / 'initializing/candidate.json'
            candidate = json.loads(candidate_path.read_text())
            change(candidate)
            candidate_path.write_text(json.dumps(candidate))
            ticket = Ticket('session', self.ticket.token, digest(candidate))
            (self.path / 'initializing/ticket.json').write_text(json.dumps(asdict(ticket)))
            receipt_path = self.path / 'initializing/receipt.json'
            receipt = json.loads(receipt_path.read_text())
            receipt['ticket'] = asdict(ticket)
            for observation in receipt['observations'].values():
                observation['candidate'] = ticket.candidate
            receipt_path.write_text(json.dumps(receipt))
            (self.path / 'active.json').write_text(json.dumps(dict(ticket=asdict(ticket),
                manifest=_manifest(candidate, ticket), receipt=digest(receipt))))
            with self.assertRaises(ValueError):
                self.subset()
