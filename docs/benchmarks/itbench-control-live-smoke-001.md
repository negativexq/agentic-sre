# ITB control live smoke 001

## Classification

`LIVE_SMOKE_PREFLIGHT_FAILED`

The smoke was fail-closed before provider construction. The future-live
preflight rejected the preregistration because it did not contain the required
`semantic_availability_policy_hash` capability identity field.

No schema request or model transport was attempted. The preregistration and
fresh smoke ledger are preserved as evidence; this run was not repaired or
rerun.

| Check | Result |
|---|---|
| Approved runtime HEAD | `505019000f54ad396bfcce5ec6129f6aced7ab88` |
| Provider constructed | No |
| OpenAI outbound attempts | 0 |
| Luna calls | 0 |
| Judge calls | 0 |
| Official benchmark scenarios | 0 |
| Live smoke scenarios | 0 |
| Fresh ledger | 8 capacity, 0 used, 8 remaining |
| Rerun | Forbidden |

The next action requires a separate human decision. No code or model policy
was changed after this preflight failure.
