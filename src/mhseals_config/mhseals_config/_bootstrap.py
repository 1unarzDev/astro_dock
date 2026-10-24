"""Private first-start filesystem writer; never opens outputs or starts processes.

Callers supply domain-validated JSON documents and explicit observation recipes.
Reservations survive failure: recovery requires an owner, never silent fallback.
"""
from dataclasses import dataclass, asdict
from pathlib import Path
import json
import os
import uuid
from .model import Artifact, BaselineRevision, Manifest, SessionSelection, digest, _identity
from ._evidence import Recipe, verified


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path, data):
    # New immutable objects only. No check-then-replace publication claim.
    with open(path, 'x') as stream:
        json.dump(data, stream, sort_keys=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    _sync_dir(path.parent)


def _read(path):
    with open(path) as stream:
        return json.load(stream)


@dataclass(frozen=True)
class Ticket:
    scope: str
    token: str
    candidate: str


def _scope(root, scope):
    _identity(scope)
    return Path(root) / scope


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
    path = _scope(root, scope)
    path.mkdir(parents=True, exist_ok=True)
    _sync_dir(path.parent)
    active = path / 'active.json'
    if active.exists():
        read_active(root, scope, evaluators=evaluators)  # Corruption is an error, never first-run fallback.
        raise FileExistsError('scope already has active selection')
    if (path / 'owner.json').exists():
        raise FileExistsError('scope owned/running')
    reservation = path / 'initializing'
    reservation.mkdir()  # Atomic exclusive reservation, retained on any failure.
    _sync_dir(path)
    token = uuid.uuid4().hex
    artifacts = {}
    for participant, document in validated_inputs.items():
        name = participant + '.json'
        _write(reservation / name, document)
        artifacts[participant] = asdict(Artifact(name, digest(document)))
    candidate = dict(schema=1, scope=scope, token=token, baseline=asdict(baseline),
                     artifacts=artifacts, recipes=recipes, source=source_identity,
                     participants=participant_identities, closure=closure)
    _write(reservation / 'candidate.json', candidate)
    ticket = Ticket(scope, token, digest(candidate))
    _write(reservation / 'ticket.json', asdict(ticket))
    _write(reservation / 'pending.json', dict(status='bootstrap_pending', ticket=asdict(ticket)))
    return ticket


def read_candidate(root, ticket):
    path = _scope(root, ticket.scope) / 'initializing'
    if _read(path / 'ticket.json') != asdict(ticket):
        raise ValueError('ticket identity mismatch')
    candidate = _read(path / 'candidate.json')
    if digest(candidate) != ticket.candidate or candidate['token'] != ticket.token or candidate['scope'] != ticket.scope:
        raise ValueError('candidate identity mismatch')
    for artifact in candidate['artifacts'].values():
        reference = Artifact(**artifact)
        if digest(_read(path / reference.name)) != reference.sha256:
            raise ValueError('candidate artifact corrupted')
    return candidate


def finish_bootstrap(root, ticket, observations, *, evaluators=None):
    """Record supplied observations; only complete identity-bound recipes promote."""
    candidate = read_candidate(root, ticket)
    path = _scope(root, ticket.scope)
    if (path / 'owner.json').exists() or (path / 'active.json').exists():
        raise FileExistsError('scope became owned/active')
    documents = {p: _read(path / 'initializing' / a['name'])
                 for p, a in candidate['artifacts'].items()}
    accepted = verified(candidate, ticket, observations, evaluators or {}, documents)
    complete = len(accepted) == len(candidate['recipes'])
    receipt = dict(status='verified_startup' if complete else 'initialized_unverified',
                   ticket=asdict(ticket), observations=observations, verified=accepted)
    _write(path / 'initializing' / 'receipt.json', receipt)
    if complete:
        manifest = _manifest(candidate, ticket)
        Manifest.read(manifest)
        _write(path / 'active.json', dict(manifest=manifest, ticket=asdict(ticket),
                                         receipt=digest(receipt)))
    return receipt


def _manifest(candidate, ticket):
    return dict(schema=1, baseline=candidate['baseline'],
                session=asdict(SessionSelection(ticket.scope, candidate['baseline']['revision'],
                                               ticket.candidate)), trial=None,
                effective=list(candidate['artifacts'].values()))


def read_active(root, scope, *, evaluators=None):
    path = _scope(root, scope)
    active = _read(path / 'active.json')  # Missing/corrupt is never defaults.
    if set(active) != {'manifest', 'ticket', 'receipt'}:
        raise ValueError('invalid active selection')
    ticket = Ticket(**active['ticket'])
    if ticket.scope != scope:
        raise ValueError('wrong active scope')
    candidate = read_candidate(root, ticket)
    if active['manifest'] != _manifest(candidate, ticket):
        raise ValueError('active selection mismatch')
    manifest = Manifest.read(active['manifest'])
    receipt = _read(path / 'initializing' / 'receipt.json')
    documents = {p: _read(path / 'initializing' / a['name'])
                 for p, a in candidate['artifacts'].items()}
    accepted = verified(candidate, ticket, receipt['observations'], evaluators or {}, documents)
    if (digest(receipt) != active['receipt'] or receipt['status'] != 'verified_startup'
            or receipt['ticket'] != asdict(ticket) or receipt['verified'] != accepted
            or set(accepted) != set(candidate['recipes'])):
        raise ValueError('invalid verified startup evidence')
    return manifest
