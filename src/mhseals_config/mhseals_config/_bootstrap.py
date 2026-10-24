"""Private first-start filesystem writer; never opens outputs or starts processes.

Callers supply domain-validated JSON documents and explicit observation recipes.
Reservations survive failure: recovery requires an owner, never silent fallback.
"""
from dataclasses import dataclass, asdict
import uuid
from .model import Artifact, BaselineRevision, Manifest, SessionSelection, digest, _identity
from ._evidence import Recipe, verified
from ._store import Store
from .resolver import freeze


@dataclass(frozen=True)
class Ticket:
    scope: str
    token: str
    candidate: str


class NoActiveSelection(FileNotFoundError):
    """Only the initial active selection object is absent."""


@dataclass(frozen=True)
class StartupInputs:
    ticket: Ticket
    manifest: Manifest | None
    candidate: object
    documents: object


def read_bootstrap_inputs(root, ticket):
    with Store(root, ticket.scope) as store:
        candidate, documents = _candidate(store, ticket)
        return StartupInputs(ticket, None, freeze(candidate), freeze(documents))


def read_selected_inputs(root, scope, *, evaluators=None):
    with Store(root, scope) as store:
        return _active(store, scope, evaluators or {})


def read_selected_subset(root, scope, participants, *, evaluators, expected_candidate=None):
    """Validate the complete owner selection, rechecking only named own recipes.

    Foreign evidence receives structural validation, not a behavior claim.
    """
    if isinstance(participants, str):
        raise ValueError('explicit participant collection required')
    requested = tuple(participants)
    if not requested or len(set(requested)) != len(requested):
        raise ValueError('nonempty unique participant subset required')
    with Store(root, scope) as store:
        inputs, observations = _selection(store, scope)
        candidate, documents = inputs.candidate, inputs.documents
        if expected_candidate is not None and inputs.ticket.candidate != expected_candidate:
            raise ValueError('managed candidate mismatch')
        if not set(requested) <= set(candidate['recipes']):
            raise ValueError('unknown selected participant')
        # Evaluators receive captured ordinary JSON, as in the strict full reader.
        own_candidate = dict(candidate, recipes={p: candidate['recipes'][p] for p in requested})
        accepted = verified(own_candidate, inputs.ticket,
                            {p: observations[p] for p in requested}, evaluators, documents)
        if set(accepted) != set(requested):
            raise ValueError('subset behavior evidence unavailable or invalid')
        return StartupInputs(inputs.ticket, inputs.manifest, freeze(candidate), freeze(documents))


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
        return _candidate(store, ticket)[0]


def _candidate(store, ticket):
    if store.read('initializing/ticket.json') != asdict(ticket):
        raise ValueError('ticket identity mismatch')
    candidate = store.read('initializing/candidate.json')
    if digest(candidate) != ticket.candidate or candidate['token'] != ticket.token or candidate['scope'] != ticket.scope:
        raise ValueError('candidate identity mismatch')
    documents = {}
    for participant, artifact in candidate['artifacts'].items():
        reference = Artifact(**artifact)
        document = store.read('initializing/' + reference.name)
        if digest(document) != reference.sha256:
            raise ValueError('candidate artifact corrupted')
        documents[participant] = document
    return candidate, documents


def finish_bootstrap(root, ticket, observations, *, evaluators=None):
    """Record supplied observations; only complete identity-bound recipes promote."""
    with Store(root, ticket.scope) as store:
        candidate, documents = _candidate(store, ticket)
        if store.exists('owner.json') or store.exists('active.json'):
            raise FileExistsError('scope became owned/active')
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
    return read_selected_inputs(root, scope, evaluators=evaluators).manifest


def _selection(store, scope):
    try:
        active = store.read('active.json')
    except FileNotFoundError as error:
        raise NoActiveSelection(scope) from error
    if set(active) != {'manifest', 'ticket', 'receipt'}:
        raise ValueError('invalid active selection')
    ticket = Ticket(**active['ticket'])
    if ticket.scope != scope:
        raise ValueError('wrong active scope')
    candidate, documents = _candidate(store, ticket)
    if active['manifest'] != _manifest(candidate, ticket):
        raise ValueError('active selection mismatch')
    manifest = Manifest.read(active['manifest'])
    receipt = store.read('initializing/receipt.json')
    closure = set(candidate['artifacts'])
    if (not closure or closure != set(candidate['recipes'])
            or closure != set(candidate['participants'])
            or set(receipt) != {'status', 'ticket', 'observations', 'verified'}
            or set(receipt['observations']) != closure
            or not isinstance(receipt['verified'], list)
            or len(receipt['verified']) != len(closure)
            or set(receipt['verified']) != closure
            or digest(receipt) != active['receipt']
            or receipt['status'] != 'verified_startup'
            or receipt['ticket'] != asdict(ticket)):
        raise ValueError('invalid complete startup receipt')
    for participant in closure:
        recipe = candidate['recipes'][participant]
        observation = receipt['observations'][participant]
        expected = dict(scope=scope, candidate=ticket.candidate, source=candidate['source'],
                        identity=candidate['participants'][participant], recipe=recipe)
        if (not isinstance(recipe, str) or '/' not in recipe
                or not isinstance(observation, dict)
                or set(observation) != {*expected, 'evidence'}
                or any(observation.get(k) != v for k, v in expected.items())):
            raise ValueError('invalid bound participant evidence')
    references = {a['name']: a['sha256'] for a in candidate['artifacts'].values()}
    if len(references) != len(closure):
        raise ValueError('duplicate candidate artifact reference')
    for artifact in manifest.baseline.artifacts:
        if references.get(artifact.name) != artifact.sha256:
            raise ValueError('baseline reference outside candidate')
    return StartupInputs(ticket, manifest, candidate, documents), receipt['observations']


def _active(store, scope, evaluators):
    inputs, observations = _selection(store, scope)
    accepted = verified(inputs.candidate, inputs.ticket, observations, evaluators, inputs.documents)
    if set(accepted) != set(inputs.candidate['recipes']):
        raise ValueError('invalid verified startup evidence')
    return StartupInputs(inputs.ticket, inputs.manifest, freeze(inputs.candidate), freeze(inputs.documents))
