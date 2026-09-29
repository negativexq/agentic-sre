"""Explicit incident endpoints for synthetic v2 causal-claim fixtures.

Test fixtures historically supplied an opaque 'latency' string. These fixtures
now declare their path terminal (or directly alerting actor) as the incident
symptom. Production legacy records are never upgraded this way.
"""

from typing import Any

from packages.rca.model import Hypothesis


def incident_claim(**kwargs: Any) -> Hypothesis:
    actor = kwargs["causal_actor"]
    paths = kwargs.get("causal_paths", ())
    direct = kwargs.get("causal_explanation") == "DIRECT"
    symptoms = tuple(dict.fromkeys(path[-1].target for path in paths if path))
    if direct:
        symptoms = (actor,)
    local = tuple(f for f in kwargs.get("findings", ()) if f.entity == actor)
    instances = {f.entity_instance for f in local}
    if len(instances) == 1:
        kwargs.setdefault("actor_instance", next(iter(instances)))
    kwargs.setdefault("claim_version", "m21.v2")
    kwargs.setdefault("symptom_entities", symptoms)
    return Hypothesis(**kwargs)
