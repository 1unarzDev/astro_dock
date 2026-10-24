"""Pure package descriptor inventory. Metadata never grants mutation authority."""
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import math
import yaml
from pathlib import PurePosixPath

from .model import Artifact
from .resolver import Field, freeze, parse

# Nested contracts are deliberately finite; executable references are registry IDs.
_CONTRACTS = {
    'value': {'type', 'dimensions', 'units', 'constraint_ref', 'validator_ref', 'inputs'},
    'authoring': {'destination', 'pointer', 'layers', 'absence', 'null', 'reset',
                  'scopes', 'controlling_keys'},
    'consumer': {'id', 'version', 'parameters', 'compiler_ref', 'inputs', 'outputs',
                 'selection_ref', 'dependencies', 'candidate_tier', 'owner_group'},
    'safety': {'policy_ref', 'armed_ref', 'goal_ref', 'procedure_ref', 'hold_ref',
               'confirmation_ref', 'compensation_ref', 'state_ref'},
    'evidence': {'recipe_ref', 'consumer', 'version', 'required_type'},
    'coverage': {'source', 'consumer', 'status', 'revision', 'path', 'line', 'consume_site'},
}
_TYPES = {'boolean': bool, 'integer': int, 'number': (int, float), 'string': str,
          'sequence': tuple, 'mapping': Mapping}
_LAYERS = {'package', 'deployment', 'profile', 'operator', 'session', 'patch', 'trial', 'derived'}
_STATUS = {'discovered', 'traced', 'startup-supported', 'live-supported',
           'behavior-verified', 'unsupported', 'fixed-invariant'}


def _shape(value, allowed, required=()):
    if not isinstance(value, Mapping) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError('invalid catalog fields')


def _name(value):
    if not isinstance(value, str) or not value:
        raise ValueError('expected nonempty catalog name')


def _names(values):
    if not isinstance(values, (tuple, list)):
        raise ValueError('expected name sequence')
    for value in values:
        _name(value)
    if len(set(values)) != len(values):
        raise ValueError('duplicate catalog name')


def _hash(value):
    _name(value)
    Artifact('source', value)


def _pointer(root, pointer):
    if not isinstance(pointer, str) or not pointer.startswith('/'):
        raise ValueError('expected absolute YAML pointer')
    value = root
    for part in pointer[1:].split('/'):
        # JSON Pointer escapes only; reject malformed escapes.
        if '~' in part.replace('~0', '').replace('~1', ''):
            raise ValueError('invalid pointer escape')
        part = part.replace('~1', '/').replace('~0', '~')
        try:
            value = value[int(part)] if isinstance(value, tuple) else value[part]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError('dangling default pointer') from exc
    return value


def _yaml(text):
    # Reuse located strict YAML validation, including recursive aliases and duplicates.
    node = yaml.compose(text, Loader=yaml.SafeLoader)
    if not isinstance(node, yaml.nodes.MappingNode):
        raise ValueError('catalog resource must be a mapping')
    text = yaml.serialize(node)
    wrapped = 'document:\n' + ''.join('  ' + line + '\n' for line in text.splitlines())
    return parse(wrapped, '<catalog>', 'defaults', {'document': Field(dict)})[0].value


def _typed(value, contract):
    kind = contract['type']
    if kind not in _TYPES:
        raise ValueError('unknown catalog type')
    if value is not None and (not isinstance(value, _TYPES[kind]) or
                              kind in ('integer', 'number') and isinstance(value, bool)):
        raise ValueError('default type mismatch')
    def finite(item):
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError('nonfinite default')
        if isinstance(item, (Mapping, tuple)):
            for child in item.values() if isinstance(item, Mapping) else item:
                finite(child)
    finite(value)
    dims = contract.get('dimensions', ())
    if not isinstance(dims, (tuple, list)) or any(type(n) is not int or n < 0 for n in dims):
        raise ValueError('invalid dimensions')
    def dimensions(item, remaining):
        if remaining:
            if not isinstance(item, tuple) or len(item) != remaining[0]:
                raise ValueError('default dimensions mismatch')
            for child in item:
                dimensions(child, remaining[1:])
    if value is not None:
        dimensions(value, dims)


@dataclass(frozen=True)
class CatalogRow:
    descriptor: object
    default: object
    blockers: tuple

    @property
    def writable(self):
        return False


@dataclass(frozen=True)
class DescriptorBundle:
    """Validated immutable metadata; resources are supplied bytes, never opened here."""
    descriptor: object
    rows: tuple

    @classmethod
    def read_yaml(cls, text, resources, registry, consumer_versions):
        return cls.read(_yaml(text), resources, registry, consumer_versions)

    @classmethod
    def read(cls, data, resources, registry, consumer_versions):
        _shape(data, {'schema_version', 'owner_package', 'revision', 'consumer_versions',
                      'default_sources', 'leaves'},
               {'schema_version', 'owner_package', 'revision', 'consumer_versions', 'default_sources', 'leaves'})
        if type(data['schema_version']) is not int or data['schema_version'] != 1:
            raise ValueError('unsupported catalog schema')
        _name(data['owner_package'])
        _name(data['revision'])
        if not isinstance(data['consumer_versions'], Mapping):
            raise ValueError('expected pinned consumer versions')
        for consumer, version in data['consumer_versions'].items():
            _name(consumer)
            _name(version)
            if consumer in consumer_versions and consumer_versions[consumer] != version:
                raise ValueError('incompatible consumer version')
        for name in ('default_sources', 'leaves'):
            if not isinstance(data[name], (list, tuple)):
                raise ValueError('expected descriptor sequence')
        sources = {}
        for source in data['default_sources']:
            _shape(source, {'id', 'resource', 'sha256'}, {'id', 'resource', 'sha256'})
            sid, path = source['id'], source['resource']
            _name(sid)
            _name(path)
            _hash(source['sha256'])
            if sid in sources or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts:
                raise ValueError('duplicate source or non-package resource')
            raw = resources.get(sid)
            if type(raw) is not bytes or hashlib.sha256(raw).hexdigest() != source['sha256']:
                raise ValueError('missing immutable resource or digest mismatch')
            sources[sid] = (source, _yaml(raw.decode('utf-8')))
        identities, rows = set(), []
        for leaf in data['leaves']:
            _shape(leaf, {'canonical_key', 'aliases', 'default_ref', 'invariant_ref',
                          'value', 'authoring', 'consumers', 'safety', 'evidence', 'coverage'},
                   {'canonical_key', 'aliases'})
            _names(leaf['aliases'])
            _names([leaf['canonical_key'], *leaf['aliases']])
            for identity in (leaf['canonical_key'], *leaf['aliases']):
                if identity in identities:
                    raise ValueError('catalog alias collision')
                identities.add(identity)
            blockers = ['runtime-authority-not-provided']
            for section in ('value', 'authoring', 'consumers', 'safety', 'evidence', 'coverage'):
                if not leaf.get(section):
                    blockers.append('missing-' + section)
            for section, entries in (('value', [leaf.get('value', {})]),
                                     ('authoring', [leaf.get('authoring', {})]),
                                     ('safety', [leaf.get('safety', {})]),
                                     ('consumer', leaf.get('consumers', ())),
                                     ('evidence', leaf.get('evidence', ())),
                                     ('coverage', leaf.get('coverage', ()))):
                if not isinstance(entries, (list, tuple)):
                    raise ValueError('expected contract sequence')
                for entry in entries:
                    _shape(entry, _CONTRACTS[section])
                    blockers.extend('missing-' + section + '.' + key for key in sorted(_CONTRACTS[section] - set(entry)))
                    for key, value in entry.items():
                        if key in {'inputs', 'outputs', 'parameters', 'dependencies', 'layers',
                                   'scopes', 'controlling_keys'}:
                            _names(value)
                        elif key == 'dimensions':
                            if not isinstance(value, (list, tuple)) or any(
                                    type(n) is not int or n < 0 for n in value):
                                raise ValueError('invalid dimensions')
                        elif key == 'line':
                            if type(value) is not int or value < 1:
                                raise ValueError('invalid trace line')
                        elif key != 'candidate_tier':
                            _name(value)
                        if value in ('unknown', 'unsupported'):
                            blockers.append('unresolved-' + section + '.' + key)
                        if key.endswith('_ref') and value not in ('unsupported', 'unknown'):
                            _name(value)
                            if value not in registry or not callable(registry[value]):
                                blockers.append('unbound-reference:' + value)
                    if section == 'consumer':
                        tier = entry.get('candidate_tier', 'unsupported')
                        if tier != 'unsupported' and (type(tier) is not int or not 1 <= tier <= 5):
                            raise ValueError('invalid candidate tier')
                        cid, version = entry.get('id'), entry.get('version')
                        if cid not in data['consumer_versions'] or version != data['consumer_versions'][cid]:
                            blockers.append('missing-consumer-pin:' + str(cid))
                        if cid not in consumer_versions or version != consumer_versions[cid]:
                            blockers.append('unresolved-consumer-version:' + str(cid))
                    if section == 'evidence':
                        cid = entry.get('consumer')
                        if entry.get('version') != data['consumer_versions'].get(cid):
                            blockers.append('unresolved-evidence-version:' + str(cid))
                    if section == 'coverage':
                        if entry.get('status') not in _STATUS:
                            raise ValueError('invalid coverage status')
                        if entry.get('status') in ('discovered', 'unsupported'):
                            blockers.append('inventory-coverage:' + entry['status'])
            if 'type' in leaf.get('value', {}) and leaf['value']['type'] not in _TYPES:
                raise ValueError('unknown catalog type')
            authoring = leaf.get('authoring', {})
            for key, choices in {'absence': {'next-allowed-layer', 'required', 'domain-defined'},
                                 'null': {'reject', 'allowed', 'follows-controlling-key'},
                                 'reset': {'pinned-default', 'next-allowed-layer', 'unsupported'}}.items():
                if key in authoring and authoring[key] not in choices | {'unknown', 'unsupported'}:
                    raise ValueError('unknown authoring semantics')
            if set(authoring.get('layers', ())) - _LAYERS:
                raise ValueError('unknown authoring layer')
            if set(authoring.get('scopes', ())) - {'baseline', 'session', 'trial'}:
                raise ValueError('unknown persistence scope')
            if authoring.get('controlling_keys') and set(authoring.get('scopes', ())) & {'baseline'}:
                raise ValueError('derived leaf cannot authorize durable writing')
            if 'default_ref' in leaf and 'invariant_ref' in leaf:
                raise ValueError('ambiguous default authority')
            default = None
            if 'default_ref' in leaf:
                ref = leaf['default_ref']
                _shape(ref, {'source_id', 'yaml_pointer', 'path', 'line', 'source_hash'},
                       {'source_id', 'yaml_pointer', 'path', 'line', 'source_hash'})
                if ref['source_id'] not in sources:
                    raise ValueError('dangling source reference')
                source, root = sources[ref['source_id']]
                if ref['source_hash'] != source['sha256'] or ref['path'] != source['resource']:
                    raise ValueError('default provenance mismatch')
                if type(ref['line']) is not int or ref['line'] < 1:
                    raise ValueError('invalid source line')
                default = _pointer(root, ref['yaml_pointer'])
                if 'value' in leaf and 'type' in leaf['value']:
                    _typed(default, leaf['value'])
                if default is None and authoring.get('null') not in ('allowed', 'follows-controlling-key'):
                    blockers.append('unresolved-null-semantics')
            elif 'invariant_ref' in leaf:
                _name(leaf['invariant_ref'])
                if not callable(registry.get(leaf['invariant_ref'])):
                    blockers.append('unbound-invariant')
            else:
                blockers.append('missing-default-authority')
            rows.append(CatalogRow(freeze(leaf), default, tuple(sorted(set(blockers)))))
        return cls(freeze(data), tuple(rows))

    def field(self, canonical_key):
        """Exact leaf lookup: unknown family descendants never gain wildcard support."""
        for row in self.rows:
            if canonical_key == row.descriptor['canonical_key']:
                return row
        raise ValueError('unknown catalog leaf: ' + canonical_key)
