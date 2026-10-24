"""Pure domain-owned evaluators explicitly supplied by trusted startup adapters.

The registry is code, never loaded from input YAML or persisted receipt data.
Evaluators consume raw observations and candidate documents. Core supplies no
hardware evaluator and cannot infer behavior from an asserted checks dictionary.
"""
from dataclasses import dataclass
from collections.abc import Callable


@dataclass(frozen=True)
class Recipe:
    identity: str
    evaluate: Callable

    def __post_init__(self):
        if not isinstance(self.identity, str) or '/' not in self.identity or not callable(self.evaluate):
            raise ValueError('recipe requires versioned identity and pure evaluator')


def verified(candidate, ticket, observations, evaluators, documents):
    if not isinstance(observations, dict) or set(observations) - set(candidate['recipes']):
        raise ValueError('unknown observation participant')
    accepted = []
    for participant, recipe_id in candidate['recipes'].items():
        recipe = evaluators.get(recipe_id)
        observation = observations.get(participant, {})
        expected = dict(scope=ticket.scope, candidate=ticket.candidate,
                        source=candidate['source'], identity=candidate['participants'][participant],
                        recipe=recipe_id)
        if (not isinstance(recipe, Recipe) or recipe.identity != recipe_id
                or not isinstance(observation, dict)
                or set(observation) != {*expected, 'evidence'}
                or any(observation.get(k) != v for k, v in expected.items())):
            continue
        # Unsupported/partial raw observations must be refused by domain evaluator.
        try:
            valid = recipe.evaluate(documents[participant], observation['evidence']) is True
        except (ValueError, TypeError, KeyError):
            valid = False
        if valid:
            accepted.append(participant)
    return accepted
