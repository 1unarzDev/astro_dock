"""Private first-start filesystem writer; never opens outputs or starts processes.

Callers supply domain-validated JSON documents and explicit observation recipes.
Reservations survive failure: recovery requires an owner, never silent fallback.
"""
from dataclasses import dataclass, asdict
import uuid
from .model import Artifact, BaselineRevision, Manifest, SessionSelection, digest, _identity
from ._evidence import Recipe, verified
from ._store import Store


@dataclass(frozen=True)
class Ticket:
    scope: str
    token: str
    candidate: str


def begin_bootstrap(root, scope, baseline, validated_inputs, recipes, *,
                    source_identity, participant_identities, closure='passive', evaluators=None):
    """Reserve an unowned scope and materialize domain-validated inputs.

    Recipes name versioned, trusted domain-owned pure evaluators.
    Unknown evidence cannot verify. Explicit
    output activation is outside this helper's permitted closure.
    """
    if closure not in ('passive', 'inert', 'disarmed'):
        raise ValueError('bootstrap closure excludes output opening')
    if not isinstance(baseline, BaselineRevision):
        raise ValueError('persistent baseline required')
    if not validated_inputs or set(validated_inputs) != set(recipes) or set(recipes) != set(participant_identities):
        raise ValueError('participant closure mismatch')
    evaluators = evaluators or {}
    for participant, recipe_id in recipes.items():
        _identity(participant)
        if not isinstance(recipe_id, str) or not isinstance(evaluators.get(recipe_id), Recipe):
            raise ValueError('unregistered observation recipe')
    with Store(root, scope) as store:
        if store.exists('active.json'):
            _active(store, scope, evaluators)  # Corruption never triggers fallback.
            raise FileExistsError('scope already has active selection')
        if store.exists('owner.json'):
            raise FileExistsError('scope owned/running')
        store.mkdir('initializing')  # Exclusive reservation, retained on failure.
        token = uuid.uuid4().hex
        artifacts = {}
        for participant, document in validated_inputs.items():
            name = participant + '.json'
            store.write('initializing/' + name, document)
            artifacts[participant] = asdict(Artifact(name, digest(document)))
        candidate = dict(schema=1, scope=scope, token=token, baseline=asdict(baseline),
                         artifacts=artifacts, recipes=recipes, source=source_identity,
                         participants=participant_identities, closure=closure)
        store.write('initializing/candidate.json', candidate)
        ticket = Ticket(scope, token, digest(candidate))
        store.write('initializing/ticket.json', asdict(ticket))
        store.write('initializing/pending.json', dict(status='bootstrap_pending', ticket=asdict(ticket)))
        return ticket


def read_candidate(root, ticket):
    with Store(root, ticket.scope) as store:
        return _candidate(store, ticket)


def _candidate(store, ticket):
    if store.read('initializing/ticket.json') != asdict(ticket):
        raise ValueError('ticket identity mismatch')
    candidate = store.read('initializing/candidate.json')
    if digest(candidate) != ticket.candidate or candidate['token'] != ticket.token or candidate['scope'] != ticket.scope:
        raise ValueError('candidate identity mismatch')
    for artifact in candidate['artifacts'].values():
        reference = Artifact(**artifact)
        if digest(store.read('initializing/' + reference.name)) != reference.sha256:
            raise ValueError('candidate artifact corrupted')
    return candidate


def finish_bootstrap(root, ticket, observations, *, evaluators=None):
    """Record supplied observations; only complete identity-bound recipes promote."""
    with Store(root, ticket.scope) as store:
        candidate = _candidate(store, ticket)
        if store.exists('owner.json') or store.exists('active.json'):
            raise FileExistsError('scope became owned/active')
        documents = {p: store.read('initializing/' + a['name'])
                     for p, a in candidate['artifacts'].items()}
        accepted = verified(candidate, ticket, observations, evaluators or {}, documents)
        complete = len(accepted) == len(candidate['recipes'])
        receipt = dict(status='verified_startup' if complete else 'initialized_unverified',
                       ticket=asdict(ticket), observations=observations, verified=accepted)
        store.write('initializing/receipt.json', receipt)
        if complete:
            manifest = _manifest(candidate, ticket)
            Manifest.read(manifest)
            store.write('active.json', dict(manifest=manifest, ticket=asdict(ticket),
                                             receipt=digest(receipt)))
        return receipt


def _manifest(candidate, ticket):
    return dict(schema=1, baseline=candidate['baseline'],
                session=asdict(SessionSelection(ticket.scope, candidate['baseline']['revision'],
                                               ticket.candidate)), trial=None,
                effective=list(candidate['artifacts'].values()))


def read_active(root, scope, *, evaluators=None):
    with Store(root, scope) as store:
        return _active(store, scope, evaluators or {})


def _active(store, scope, evaluators):
    active = store.read('active.json')  # Missing/corrupt is never defaults.
    if set(active) != {'manifest', 'ticket', 'receipt'}:
        raise ValueError('invalid active selection')
    ticket = Ticket(**active['ticket'])
    if ticket.scope != scope:
        raise ValueError('wrong active scope')
    candidate = _candidate(store, ticket)
    if active['manifest'] != _manifest(candidate, ticket):
        raise ValueError('active selection mismatch')
    manifest = Manifest.read(active['manifest'])
    receipt = store.read('initializing/receipt.json')
    documents = {p: store.read('initializing/' + a['name'])
                 for p, a in candidate['artifacts'].items()}
    accepted = verified(candidate, ticket, receipt['observations'], evaluators or {}, documents)
    if (digest(receipt) != active['receipt'] or receipt['status'] != 'verified_startup'
            or receipt['ticket'] != asdict(ticket) or receipt['verified'] != accepted
            or set(accepted) != set(candidate['recipes'])):
        raise ValueError('invalid verified startup evidence')
    return manifest
