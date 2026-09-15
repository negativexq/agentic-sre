# ITBench E7 invalid-decision forensic audit

This is a post-hoc, ground-truth-free audit of immutable E7 artifacts.
No provider or judge calls were made.

Official invalid decisions: **13**.

| mechanism | count |
|---|---:|
| `empty_CALL_TOOLS_request_collection` | 0 |
| `empty_SUBMIT_root_cause_collection` | 0 |
| `mixed_decision_payload` | 0 |
| `invalid_evidence_refs` | 0 |
| `entity_syntax` | 0 |
| `tool_argument_schema` | 1 |
| `root_cause_cardinality` | 0 |
| `other` | 0 |
| `unknown` | 12 |

All official records durably contain `DECISION_SCHEMA`, path `$`, type `value_error`, and `provider_response_received=true`. The malformed structured payload, function name, and exact cardinalities were not persisted by E7.

Therefore the dominant mechanism is recorded as `UNKNOWN`, not attributed to a model mistake or a protocol mismatch without evidence.

The E7 smoke has the same bounded diagnostic. Its exact root cause is also `NOT_DURABLY_RECORDED`.
