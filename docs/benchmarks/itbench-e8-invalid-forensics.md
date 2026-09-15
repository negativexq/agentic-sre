# ITBench E8 invalid-decision forensic

The audit uses only frozen E8 native artifacts. No provider, judge, or
ground-truth access was used during this audit.

| Mechanism | Count | E9 classification |
|---|---:|---|
| DECISION_SCHEMA | 8 | Recoverable protocol |
| DECISION_SEMANTICS | 11 | Recoverable state transition |
| TOOL_ARGUMENTS | 2 | Recoverable tool argument |
| EVIDENCE_REFERENCE | 0 | — |
| CANONICAL_IDENTITY | 0 | — |
| TOO_MANY_ROOT_CAUSES | 0 | — |
| OTHER | 0 | — |

All 21 were safe provider responses followed by runtime rejection. Ten
candidate updates referenced entities the observable backend did not contain;
one exceeded the active-candidate bound; five STOP reasons exceeded the schema
bound; three candidate-entity values failed entity validation; and two tool
requests used invalid limits. None was a hard safety or infrastructure
failure. E9 converts these classes into bounded rejection feedback and a
continued investigation.

Per-scenario details are in
[itbench-e8-invalid-forensics.json](itbench-e8-invalid-forensics.json).
