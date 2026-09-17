# Agentic SRE

Root-cause analysis for Kubernetes incidents that starts from what changed.
An alert arrives; Agentic SRE finds the config edit, rollout, injected fault,
or failing dependency behind it, shows the evidence, labels how sure it is,
and proposes a reversible fix. It never changes the cluster.

[![CI](https://github.com/negativexq/agentic-sre/actions/workflows/checks.yml/badge.svg)](https://github.com/negativexq/agentic-sre/actions/workflows/checks.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)

## Results

Measured on [ITBench-Lite](https://huggingface.co/datasets/ibm-research/ITBench-Lite)
SRE snapshots with a pre-registered dev/test split. The v1.0.1 release result
and every miss are in [`evals/results/v1.0.1`](evals/results/v1.0.1/README.md);
the previous v1.0.0 result remains available for comparison.

| Run | Split | Scenarios | Macro F1 | Root cause ranked first | In top 3 | Model calls |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| **Engine v1.0.1** (run once) | Test | 25 | **0.68** | 68% | 80% | 0 |
| **Engine v1.0.0** (run once) | Test | 25 | **0.68** | 68% | 80% | 0 |
| **Engine v0.7.0** (run once) | Test | 25 | **0.68** | 68% | 80% | 0 |
| Engine v0.5.0 + LLM investigator | Test | 25 | 0.64 | 64% | 80% | 44 |
| Engine v0.4.0 (run once) | Test | 25 | 0.64 | 64% | 76% | 0 |
| Engine v0.7.0 (used for tuning) | Dev | 10 | 0.90 | 90% | 90% | 0 |
| Previous LLM agent (E10) | All | 35 | 0.00 | — | — | — |

v0.8-quality live validation and v0.9-quality causal topology were added
without changing the deterministic test predictions: v1.0.1 matches v1.0.0
scenario for scenario. On
the test split, 16 of 23 answers labelled `VERIFIED` were correct. Part of
the v0.5.0 gain came from rules chosen after reading v0.4.0 test misses; the
results page discloses which. The LLM investigator (last measured at v0.5.0,
predictions unchanged since) agreed with the engine in 23 of 25 scenarios and
lost one correct answer (Scenario-31), so the deterministic engine is the
default. Three test scenarios cannot be scored by the published label
filters; see [`evals/README.md`](evals/README.md) for the
method and a disclosure of prior exposure to this dataset.

## Try it

```bash
uv sync --locked --extra dev   # or: make install when uv is installed
make demo
```

`make demo` diagnoses a built-in bad-rollout incident offline and writes
`.local/demo/diagnosis.html`:

```text
Root cause   shop/Deployment/payment
Confidence   VERIFIED
Summary      shop/Deployment/payment: spec changed:
             [payment].env[FAULT_DELAY_MS].value: 0 -> 2500 ...
Proposed remediation (not executed)
  - Roll back Deployment payment to the previous revision
      $ kubectl rollout undo deployment/payment -n shop
```

## How it works

```mermaid
flowchart LR
  AM[Alertmanager] --> CP[Control plane]
  K8s[Kubernetes API<br/>read-only] --> J[Change journal]
  J --> E[RCA engine]
  CP --> E
  Loki --> E
  E -.optional.-> L[LLM investigator]
  E --> D[Diagnosis<br/>evidence, confidence, fix]
```

1. **Symptoms** from diagnostic alerts; always-on platform alerts are ignored.
2. **Signals**: object version diffs (down to the env var or container that
   changed), Chaos Mesh experiments, network policies, quotas that reject
   pods, container failures (OOM kills, crash loops, bad images), memory or
   CPU pressure that appears with the incident, dependency connection errors,
   and warning events.
3. **Topology** from ownership, selectors, config references, and service
   calls declared in environment variables links each signal to the alerting
   components.
4. **Ranking and verification** with explainable scores and deterministic
   rules that decide `VERIFIED`, `LIKELY`, or `UNVERIFIED`.
5. **Optional LLM investigator** inspects the top candidates with read-only
   tools and may choose another one; it cannot verify its own answer.
6. **Remediation proposal** such as `rollout undo`, reverting a ConfigMap,
   pausing a chaos schedule, or raising a quota or memory limit. Nothing is executed.

More in [`docs/architecture.md`](docs/architecture.md) and
[ADR-003](docs/adr/ADR-003-change-first-rca-product.md).

## Reproduce the benchmark

```bash
make itbench-setup     # downloads the pinned ITBench-Lite snapshots (~29 GB)
make itbench-index
make eval-dev          # predict, seal, grade the dev split
make eval-test         # only from a clean, tagged commit
```

Prediction never reads ground truth, and grading refuses modified predictions.
Re-grade a stored run with `agentic-sre grade --out evals/results/v1.0.1/test`.
`seal.json` protects the prediction manifest and prediction files;
`report-seal.json` separately protects derived grading reports.
Add the investigator to a run with `make eval-dev EVAL_FLAGS=--llm` and the `SRE_LLM_*` settings.

## Live demo on kind

Requires Docker, kind, and kubectl. All five images (control plane,
migrator, and the demo shop services) are targets of one
[`infra/docker/Dockerfile`](infra/docker/Dockerfile); `make images` builds
them without a cluster, and `make deploy` builds, loads, and rolls them out.

```bash
make cluster-up deploy
make load                  # in another terminal: steady traffic
make ui                    # http://localhost:8080
make inject-bad-rollout    # payment requests now take 2.5 s
```

The control plane journals the rollout, Alertmanager fires on latency, and
the incident page shows the diagnosis. `make recover` rolls back;
`make rbac-check` confirms the control plane can read but not write.

The release gate runs this lifecycle against a fresh real cluster and cleans it
up when finished:

```bash
make e2e-kind
```

It asserts the expected verified `sre-demo/Deployment/payment-service` cause,
persisted Event evidence, rollback recovery, and replayable A → B → A object
journal behavior. The benchmark results above and this live-cluster result are
different kinds of evidence: one measures frozen snapshots, the other proves a
real Kubernetes lifecycle.

The LLM investigator is off by default. To enable it, set
`SRE_LLM_ENABLED=true`, a total call budget for the process in `SRE_LLM_MAX_CALLS` (a test-split run uses about 50), and
`OPENAI_API_KEY`; the CLI takes `--llm`.

## Repository

| Path | Contents |
| --- | --- |
| `packages/rca` | Engine, topology, signals, ranking, investigator, live readers, reports |
| `apps/control_plane` | API, Alertmanager webhook, web UI, journal watcher |
| `apps/cli` | `agentic-sre` commands: demo, diagnose, eval, grade, serve |
| `packages/evals/itbench` | Snapshot source, sealed benchmark runner, grader |
| `evals` | Split, method, stored results |
| `workload`, `infra` | Demo shop services, kind manifests, observability stack |

The earlier single-agent runtime and the E1–E11 experiment series are kept in
the `archive/experiments-2026-09` tag.

Development and CI use the checked-in `uv.lock`. Refresh it deliberately with
`make lock` (or `uv lock`) and review the complete dependency diff.

## Limits

- Causes without an observable change, fault, or error signal (for example a
  traffic spike from a load generator) are often missed.
- Namespace-level causes are not named directly.
- The measured bounded LLM investigator did not improve the deterministic engine
  on ITBench-Lite and remains optional; its effect on harder or messier
  incidents has not been measured.
- The local/demo control plane is open by default. Use the secure Kubernetes
  overlay in `infra/kubernetes/secure-api-auth/` to wire the same
  operator-provided bearer token into the control plane and Alertmanager.
  Kubernetes Secret access is deliberately disabled, but incidents, evidence,
  change history, topology, and log-derived errors may still be sensitive; do
  not expose read endpoints outside a trusted network without external auth.
  The deployment is single-process; see `docs/architecture.md` before scaling.
- Loki evidence is bounded captured observation data, not a complete historical
  log archive. Open-incident diagnosis persists the records it reads; an
  incident resolved before any diagnosis/log capture may have no persisted log
  evidence. Resolved replay never falls back to post-cutoff Loki data.
