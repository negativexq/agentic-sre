# ITBench live-smoke pre-hardening audit

This is an offline source audit at reviewed HEAD `e4cdaa354a24b7efa24900c83dee20a38b09e41b`. Historical E7/E8/E9 artifacts remain immutable. No model, provider, judge, or network inference call was made.

| Finding | Status | Evidence | Repair/verification |
|---|---|---|---|
| Dead semantic capabilities | CONFIRMED | Coverage showed recent-change/replica zero useful and temporal all inconclusive | Resolver gates all three surfaces; coverage-v2 |
| Recent-change honesty | CONFIRMED | Executor reports `change_history_available=false` | Not provider-visible without history |
| Trace utility | CONFIRMED | Unknown→unknown records are not useful edges | Meaningful-edge availability rule |
| Replica utility | CONFIRMED | Peer existence did not prove comparison | Requires peer plus difference/equality dimension |
| Temporal utility | CONFIRMED | Frozen snapshots lack usable candidate timestamps | Unavailable before model selection |
| Dynamic discovery canary | CONFIRMED | Old canary inserted C002 directly | v3 uses semantic result → `ENTITY_DISCOVERED` → C002 → REVISE |
| Target constraints | CONFIRMED | V5 target fields were free strings | Provider strict target enums |
| Executor metric | CONFIRMED | Old audit checked callability, not dispatch | Representative actual operation matrix |
| Dead policy helper | CONFIRMED | `_operations_for_phase` was superseded | Removed |
| Submission association | PARTIALLY_CONFIRMED | `_submit_ready` already checks per-target evidence | Additional direct regression remains a risk |
| Future identity fields | PARTIALLY_CONFIRMED | Existing helper did not require new capability hashes | Preflight now requires them before provider construction |

The complete machine-readable record is in [`itbench-live-smoke-prehardening-audit.json`](itbench-live-smoke-prehardening-audit.json).

## Decision

The operation and target surfaces are now derived from the same observable capability/control path. A future live smoke remains prohibited in this task; the final classification is produced only after all offline checks and CI.
