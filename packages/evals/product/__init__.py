"""Product-resolution harness: fresh-cluster scenarios, separate from the legacy suite."""

from packages.evals.product.actions import ProductAction
from packages.evals.product.spec import Expectation, Phase, ProductScenario, ProofId

__all__ = ["Expectation", "Phase", "ProductAction", "ProductScenario", "ProofId"]
