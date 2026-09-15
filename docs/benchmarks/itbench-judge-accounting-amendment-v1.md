# ITBench Luna judge accounting amendment v1

The original judge schedule was capped at 141 calls: one smoke plus four
35-scenario batches (E4, E5, E6, E7). The first Luna smoke transmitted one
request before the pinned evaluator returned HTTP 400 because it sent the
Luna-incompatible `temperature=0` parameter. That attempt is permanently
charged and is not refunded.

The remaining planned schedule is one replacement smoke plus four 35-scenario
batches, requiring 141 additional calls. The persistent judge ledger is
therefore amended from a cap of 141 to 142 while preserving `consumed=1`.
This is accounting only; it does not change agent behavior or any frozen E7
runtime component.

The evaluator launch wrapper now uses an ephemeral, auditable compatibility
copy of the pinned evaluator with `temperature=1`, one provider attempt, and
one calculation attempt. It records the compatibility patch in each result.
