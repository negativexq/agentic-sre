# ITB-E8 Smoke-002 forensic audit

Smoke-002 is preserved as a pre-official readiness failure. The durable
artifact audit found no native artifact, ITBench output, turn trace, or usage
record for the four successful provider turns. The runner wrote those files
only after the runtime returned; the next model-call reservation was blocked
by the exhausted smoke ledger before the runtime could return.

Therefore the following are explicitly **NOT_DURABLY_RECORDED**:

- turn decisions and tool requests;
- candidate updates and evidence handles;
- input/output token counts and context sizes;
- exact tool executions or terminal semantics.

This report uses no ground truth and made zero network calls. Smoke-002 is
classified as `SMOKE_HARNESS_BUDGET_DEFECT`, not as an RCA-quality result.
