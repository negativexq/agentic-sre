# M13 Investigation Audit Result

## Run metadata

| Field | Value |
| --- | --- |
| Milestone | M13 — Investigation Audit & Explainability |
| Date | 2026-09-23 |
| Commit SHA | `5b9f0cb92503063ecd4e89797b054115c6a6ae6b` |
| Commit range | `e4065f8..5b9f0cb` |
| Environment | macOS, Python 3.12.13, SQLite integration tests, disposable Kind cluster |
| Architecture/configuration | deterministic RCA; append-only investigation run artifact; report v2.0; run-bound console audit; bounded read-only action execution |
| Model configuration | no model selected; LLM disabled for this work |
| API usage class | CLASS 0 — offline / no provider call |
| Model-call budget | 0 |
| Actual model calls | 0 |
| Provider calls | 0 |
| Evaluation set | no benchmark; deterministic unit/integration/release checks only |

## Commands and checks

| Command | Result |
| --- | --- |
| `.venv/bin/python -m pytest tests/unit/report tests/integration/test_console_api.py -q` | PASS — 28 tests |
| `.venv/bin/python -m pytest tests/integration/test_console_api.py tests/unit/console -q` | PASS — 20 tests |
| `.venv/bin/python -m pytest tests/unit/rca/test_investigation.py::test_ambiguous_investigation_adds_evidence_and_resolves_deterministically -q` | PASS |
| `npm run build` in `apps/web` | PASS |
| `make check` | PASS — Ruff, format, mypy, 788 tests |
| `make e2e-kind` | PASS — fresh cluster, baseline snapshot, live bad-rollout diagnosis, event persistence, rollback A→B→A journal, stable resolved replay, disposable cluster cleanup |
| `make release-check` | FAIL — check/precommit/offline-demo pass, then `verify-release-provenance` rejects the existing latest-tag result directory before the target reaches its Kind step |

The release provenance failure is in the pre-existing release baseline: latest tag `v1.1.2` has only a public summary under `evals/results/v1.1.2/README.md` and `benchmark-summary.json`; it has no sealed `evals/results/v1.1.2/test` artifacts and its README does not declare the verifier's required `# v1.1.2 results` header or tag-bound commit metadata. The summary explicitly describes itself as not being a prediction file or replacement for sealed release artifacts. No historical result, label, seal, verifier, or threshold was changed to make this check pass.

The separate Kind E2E target completed its assertions and deleted its disposable cluster. Its scenario uses the test harness to inject and roll back a fault; no agent-directed write, Secret access, provider call, or autonomous remediation occurred.

## M13 hard gates

| Gate | Result | Evidence |
| --- | --- | --- |
| G13.1 — Persisted reconstruction | PASS | `tests/integration/test_live_diagnosis.py::test_bounded_investigation_result_survives_session_restart`; persisted artifact is fetched by diagnosis run ID and projected by report/console APIs |
| G13.2 — No report causal inference | PASS | report builder projects stored initial/final states and per-action audit only; `tests/unit/report/test_builder.py::test_builder_projects_recorded_investigation_without_recomputing_it` |
| G13.3 — Tool accounting | PASS | graph finalization asserts executed tool calls equal executed-action audit count; investigation tests assert the equality |
| G13.4 — Evidence accounting | PASS | investigation tests cover returned = new ∪ already-known and disjoint partitions; persisted console integration test checks the same partition |
| G13.5 — Authorization audit | PASS | rejected and authorized actions are visible in the persisted console DTO; rejected action is `NOT_EXECUTED` in `tests/integration/test_console_api.py` |
| G13.6 — Decision transition audit | PASS | `tests/unit/rca/test_investigation.py::test_ambiguous_investigation_adds_evidence_and_resolves_deterministically` verifies resolution and non-empty, changed before/after hypothesis and gap states |
| G13.7 — Stop reason | PASS | exact enum stop reason round-trips through persisted result and report summary in restart/report integration coverage |
| G13.8 — Immutable report | PASS | `tests/integration/test_console_api.py::test_report_projects_persisted_investigation_artifact` creates a later diagnosis and confirms the earlier report remains identical |
| G13.9 — Backward compatibility | PASS | `tests/unit/report/test_builder.py::test_legacy_v1_report_loads_without_investigation_sections` validates and renders a v1.0 report as Markdown and PDF |
| G13.10 — Relevant regression gates | FAIL | Python suite and Kind E2E pass, but `make release-check` fails at immutable v1.1.2 provenance validation described above |

## Metrics and limitations

- Full offline suite: 788 passed; one existing Starlette deprecation warning.
- Investigation efficiency, recovery, harm, and contribution benchmark metrics: not evaluated in M13; remain TBD for M14+ definitions.
- No benchmark labels, thresholds, denominators, or historical evaluation files were changed.
- M13 cannot be marked COMPLETE while G13.10 is FAIL.

## Safety

| Property | Required | Observed evidence |
| --- | --- | --- |
| Kubernetes writes by agent | 0 | No autonomous remediation or agent write capability was added; disposable release harness performed its documented operator fault/rollback scenario |
| Secret access | 0 | No Secret access added or used; release gate RBAC contract tests passed |
| Out-of-policy executions | 0 | authorization tests and persisted rejected-action non-execution assertions passed |
| Autonomous remediation | 0 | none added or invoked |
| LLM RCA authority | false | deterministic diagnosis remains source of RCA and state transitions |
| Arbitrary PromQL / LogQL / TraceQL | false | no telemetry query implementation changes in M13 |
| Ground-truth leakage | false | no benchmark prediction/grading run occurred |

## Reproduction

From this checkout, run `make check`, `make e2e-kind`, and `make release-check`. The last command currently fails at `verify-release-provenance` because the latest tag's historical result directory is a public summary rather than the sealed artifact layout required by the verifier. Do not rewrite or relabel those historical files to make the command pass.
