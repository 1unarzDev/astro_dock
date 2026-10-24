"""Located YAML leaves and explicit registered precedence; no domain defaults."""
from dataclasses import dataclass
from types import MappingProxyType
import math
import yaml
from yaml.nodes import MappingNode, SequenceNode, ScalarNode

ORDER = ('defaults', 'deployment', 'profile', 'operator', 'session', 'trial')
RESET = object()


@dataclass(frozen=True)
class Origin:
    source: str
    line: int
    column: int
    layer: str


@dataclass(frozen=True)
class Field:
    kind: type
    layers: tuple = ORDER
    nullable: bool = False
    aliases: tuple = ()


@dataclass(frozen=True)
class Assignment:
    key: str
    value: object
    origin: Origin


def parse(text, source, layer, fields):
    if layer not in ORDER:
        raise ValueError('unknown layer')
    aliases = {}
    for key, field in fields.items():
        for spelling in (key, *field.aliases):
            if spelling in aliases:
                raise ValueError('ambiguous schema alias: ' + spelling)
            aliases[spelling] = key
    loader = yaml.SafeLoader(text)
    result, seen = [], {}

    def location(node):
        return Origin(source, node.start_mark.line + 1, node.start_mark.column + 1, layer)

    def fail(message, first, second):
        raise ValueError(f'{message}: {location(first)}; {location(second)}')

    def check(node, ancestors):
        if id(node) in ancestors:
            raise ValueError(f'recursive alias at {location(node)}')
        ancestors = ancestors | {id(node)}
        if isinstance(node, MappingNode):
            keys = {}
            for k, v in node.value:
                if k.tag != 'tag:yaml.org,2002:str' or k.value == '<<':
                    raise ValueError(f'non-string/merge key at {location(k)}')
                if k.value in keys:
                    fail('duplicate mapping key', keys[k.value], k)
                keys[k.value] = k
                check(v, ancestors)
        elif isinstance(node, SequenceNode):
            for child in node.value:
                check(child, ancestors)

    def walk(node, path, ancestors):
        if id(node) in ancestors:
            raise ValueError(f'recursive alias at {location(node)}')
        ancestors = ancestors | {id(node)}
        if isinstance(node, MappingNode):
            keys = {}
            for k, v in node.value:
                if not isinstance(k, ScalarNode) or k.tag != 'tag:yaml.org,2002:str':
                    raise ValueError(f'non-string/merge key at {location(k)}')
                if k.value == '<<':
                    raise ValueError(f'merge key at {location(k)}')
                if k.value in keys:
                    fail('duplicate mapping key', keys[k.value], k)
                keys[k.value] = k
                walk(v, (*path, k.value), ancestors)
            return
        spelling = '.'.join(path)
        if spelling not in aliases:
            raise ValueError(f'unknown key {spelling} at {location(node)}')
        key = aliases[spelling]
        if key in seen:
            fail('duplicate canonical assignment', seen[key], node)
        seen[key] = node
        field = fields[key]
        if layer not in field.layers:
            raise ValueError(f'layer {layer} forbidden for {key}')
        value = loader.construct_object(node, deep=True)
        if value is None:
            if not field.nullable:
                raise ValueError(f'null forbidden for {key}')
        elif type(value) is not field.kind:
            raise ValueError(f'invalid type for {key}')
        def finite(item):
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError('nonfinite value')
            if isinstance(item, list):
                for child in item:
                    finite(child)
        finite(value)
        # Sequences are immutable whole-value replacements.
        if isinstance(value, list):
            value = freeze(value)
        result.append(Assignment(key, value, location(node)))

    try:
        root = loader.get_single_node()
        if not isinstance(root, MappingNode):
            raise ValueError('configuration must be a mapping')
        check(root, set())
        walk(root, (), set())
        return tuple(result)
    finally:
        loader.dispose()


def freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(freeze(v) for v in value)
    return value


@dataclass(frozen=True)
class Resolution:
    baseline: object
    effective: object
    origins: object


def resolve(layers):
    """Resolve parsed layers; removing an assignment reveals the lower layer.

    RESET explicitly selects the pinned default, without copying a session value
    into baseline. Transaction adapters construct reset assignments themselves.
    """
    baseline, effective, origins, defaults = {}, {}, {}, {}
    previous = -1
    for assignments in layers:
        for assignment in assignments:
            index = ORDER.index(assignment.origin.layer)
            if index < previous:
                raise ValueError('layers out of precedence order')
            previous = index
            key, value = assignment.key, assignment.value
            if index == 0:
                defaults[key] = value
            if value is RESET:
                if key not in defaults:
                    raise ValueError('reset requires pinned default')
                value = defaults[key]
            effective[key] = value
            if index < ORDER.index('session'):
                baseline[key] = value
            origins.setdefault(key, []).append(assignment.origin)
    return Resolution(freeze(baseline), freeze(effective), freeze(origins))
