# Final offline interface review

Candidate: `LIVE_SMOKE_CANDIDATE_V1`. Historical E9 remains
`ITB_E9_COMPLETE_REGRESSED`; no historical artifact was rewritten.

## Result

The final offline interface is `READY_FOR_SINGLE_LIVE_SMOKE_REVIEW`. This is
not authorization to run a smoke and is not an RCA score claim.

The initial model surface is now exactly `HYPOTHESIZE(target=visible seed)` or
`STOP`, with no semantic operation selection. VERIFY constrains
`INVESTIGATE` to the current hypothesis, `REVISE` to alternatives, and
exposes only observable candidate-scoped operations. `EVENT_ANALYSIS` is
target-scoped; `METRIC_ANOMALIES` is exposed only for the one measured useful
snapshot; trace remains conditional; dead operations remain hidden.

The provider schema uses action-specific strict branches. Offline proof covers
C999 rejection, wrong investigation target, invalid action/operation pairs,
and no-evidence submission. Dynamic discovery passes end-to-end:
`C001 → ENTITY_CONTEXT → ENTITY_DISCOVERED C002 → REVISE C002 →
INVESTIGATE C002 → SUBMIT`, with exact replay equality and complete traces.

## Offline evidence

- 35-snapshot initial-policy qualification: 0 failures.
- Final interface canary: PASS; stale capabilities 0, replay mismatches 0,
  trace completeness 100%.
- Simulated smoke v2: normal and adversarial paths PASS, both terminally
  reach `SUBMIT`.
- Semantic coverage v3: core `ENTITY_CONTEXT`, `EVENT_ANALYSIS` and
  `SPEC_ANALYSIS` are useful on 35/35; metrics 1/35; traces 1/35;
  recent-change, replica and temporal capabilities are not provider-exposed.
- Full tests: `431 passed`; Ruff, mypy, `make check`, `make a1-eval-check`
  and pre-commit PASS.

See the machine-readable artifacts for exact surfaces and measurements:
[audit](itbench-final-interface-audit.json),
[coverage](itbench-semantic-operation-coverage-v3.json),
[capability surface](itbench-live-smoke-capability-surface-v2.json),
[canary](itbench-final-interface-canary.json), and
[simulated smoke](itbench-control-plane-simulated-smoke-v2.json).

## Network accounting

Live agent calls: `0`; OpenAI outbound attempts: `0`; Luna calls: `0`;
judge calls: `0`; official benchmark scenarios: `0`; live smoke scenarios: `0`.

The next action requires separate human review. No live smoke was run.
