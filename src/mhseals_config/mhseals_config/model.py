"""Immutable identities; persistent baseline and ephemeral selections stay separate."""
from dataclasses import dataclass
import hashlib
import json


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def _identity(value):
    if not isinstance(value, str) or not value or '/' in value or value in ('.', '..'):
        raise ValueError('invalid identity')


@dataclass(frozen=True)
class Artifact:
    name: str
    sha256: str

    def __post_init__(self):
        _identity(self.name)
        if len(self.sha256) != 64 or any(c not in '0123456789abcdef' for c in self.sha256):
            raise ValueError('invalid artifact digest')


@dataclass(frozen=True)
class BaselineRevision:
    revision: str
    artifacts: tuple[Artifact, ...]

    def __post_init__(self):
        _identity(self.revision)
        if not isinstance(self.artifacts, tuple) or not all(isinstance(a, Artifact) for a in self.artifacts):
            raise ValueError('baseline artifacts must be immutable references')


@dataclass(frozen=True)
class SessionSelection:
    session: str
    baseline: str
    overlay: str
    profile: str = ''
    role: str = ''

    def __post_init__(self):
        for value in (self.session, self.baseline, self.overlay):
            _identity(value)


@dataclass(frozen=True)
class TrialSelection:
    trial: str
    session: str
    claim: str
    expires: float

    def __post_init__(self):
        for value in (self.trial, self.session, self.claim):
            _identity(value)
        if not isinstance(self.expires, (int, float)) or not 0 < self.expires < float('inf'):
            raise ValueError('invalid trial expiry')


@dataclass(frozen=True)
class Manifest:
    baseline: BaselineRevision
    session: SessionSelection | None = None
    trial: TrialSelection | None = None
    effective: tuple[Artifact, ...] = ()

    def __post_init__(self):
        if self.session and self.session.baseline != self.baseline.revision:
            raise ValueError('session baseline mismatch')
        if self.trial and (not self.session or self.trial.session != self.session.session):
            raise ValueError('trial session mismatch')
        if self.effective and not self.session:
            raise ValueError('effective artifacts require session scope')
        if not isinstance(self.effective, tuple) or not all(isinstance(a, Artifact) for a in self.effective):
            raise ValueError('invalid effective references')

    @classmethod
    def read(cls, data):
        if set(data) != {'schema', 'baseline', 'session', 'trial', 'effective'} or data['schema'] != 1:
            raise ValueError('invalid manifest schema')
        b = data['baseline']
        if set(b) != {'revision', 'artifacts'}:
            raise ValueError('invalid baseline')
        return cls(BaselineRevision(b['revision'], tuple(Artifact(**a) for a in b['artifacts'])),
                   SessionSelection(**data['session']) if data['session'] is not None else None,
                   TrialSelection(**data['trial']) if data['trial'] is not None else None,
                   tuple(Artifact(**a) for a in data['effective']))
