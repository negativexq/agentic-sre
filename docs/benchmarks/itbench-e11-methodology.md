# ITB-E11 methodology and runbook

E11 is a new experiment, distinct from the immutable E10 result. It is a frozen post-engineering benchmark run on the known ITBench scenario set, not an untouched or unseen generalization test. Before any future provider execution:

1. Freeze and review one source SHA, prompt hash, dataset revision, canonical 35-scenario order, model policy, and runtime limits.
2. Build candidate/catalog outputs without ground truth and freeze their hashes where the offline qualification requires it.
3. Run one trial per scenario in canonical order with checkpoint persistence, no retries, no fallback, and no judge.
4. Seal all predictions and verify every artifact hash.
5. Only after seal verification, run deterministic local grading. Keep any model-based judge separately deferred.

The only future model configuration is `provider=openai`, `model=gpt-5.6-luna`, `reasoning_effort=none`, `provider_retries=0`. The runtime remains read-only and runtime-owned: no shell, arbitrary filesystem, SQL, PromQL, LogQL, remediation, ground-truth access, or cross-scenario memory.

## Development status

```text
E11 LIVE RUN = NOT RUN
provider_invocations_during_development = 0
```

No E11 manifest or live ledger is created by this offline qualification iteration. A future authorized run must use the repository’s frozen E11 runner and a new execution identity.
