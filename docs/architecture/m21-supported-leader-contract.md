# M21 Supported-Leader Contract

Contract version: `m21-leader.v2`, engine `2.0.0`, 2026-09-28.
Supersedes the frozen, unimplemented `m21-leader.v1` proposal with the user's
explicit authorization. The previous independent `SUPPORTED_LEADER` layer was
not implemented in the inspected baseline and is not added on top of invalid
D1 semantics.

The authoritative definitions are in [M21 v2](m21-causal-semantics-contract.md).
A leader is assessed only after actor-scoped formation, positive incident
admission, D1 v2 witnesses and positive contradiction/effect rules.

- `SUPPORTED_CAUSE`: exactly one eligible supported admitted claim and no
  unresolved admitted rival. The claim level is `POSSIBLE_INITIATING_CAUSE`.
- `COMPETING_CAUSES`: support exists but independent supported or unresolved
  admitted rivals remain. No finding-volume dominance selects a unique root.
- `INSUFFICIENT_EVIDENCE`: no eligible admitted initiating support.

Material frontier IDs and claim bindings accompany the diagnosis. An unobserved
upstream mechanism limits initiating certainty even if its query was attempted.
Context observations are retained separately; they are not residual competitors
that must each be eliminated. Context promotion is possible on new positive
incident linkage.

D1 v2 is deliberately a possible-cause rule. Even a unique supported cause stays
under legacy `AMBIGUOUS`; it does not satisfy `RESOLVED` authority. A supported
possible cause is a useful product result and is displayed explicitly, together
with open mechanisms. Incident recovery is independently `NOT_ASSESSED` by RCA.
This preserves context invariance without granting false certainty.

The full inventory, admission reasons, rule witnesses, diagnosis scope and
material frontier bindings are deterministic and enter the epistemic digest.
There is no eight-record measurement limit. Old documents retain their recorded
semantics; old-engine replay is explicitly unsupported under engine 2.0.0.

The 35 previously seen ITBench snapshots are a development/regression set.
Report true actor retention/admission, false and correct possible support,
incorrect certainty, real-rival ambiguity, material boundaries, context blockage
and replay separately. Active provider costs require a separate live/read-tape
measurement; snapshot runs cannot establish them.
