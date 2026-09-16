# E11 retrieval qualification v2

This is a provider-free post-hoc qualification on the known 35-scenario
ITBench-Lite set. Candidate catalogs and rankings were built before ground
truth was loaded. It is not an untouched generalization result.

| Config | Catalog coverage | R@1 | R@3 | R@5 | R@10 | R@20 |
|---|---:|---:|---:|---:|---:|---:|
| B0 legacy | .543 | .229 | .314 | .343 | .343 | .343 |
| B1 expanded, no telemetry ranking, causal propagation | .857 | .257 | .400 | .600 | .686 | .686 |
| B2 B1 + corrected telemetry | .857 | .057 | .200 | .257 | .314 | .314 |
| B3 B2 + direct topology | .857 | .057 | .143 | .229 | .314 | .314 |
| B4 B3 + causal propagation | .857 | .029 | .057 | .257 | .457 | .457 |
| B5 B4 + bounded temporal signal | .857 | .029 | .114 | .257 | .571 | .571 |
| B6 B5 + shortlist diversity | .857 | .029 | .114 | .286 | .686 | .686 |

The selected initial ranking default is B1. It passes the current regression
gate at every measured top-K level: `.257/.400/.600/.686`. Corrected telemetry
is not enabled in the initial rank because it regresses all four gates on this
qualification set. This is a measured implementation limitation, not a
subsystem blacklist; telemetry remains available for later evidence gathering.

Comparing B1 with B2 after frozen outputs were produced: telemetry improved the
first reachable rank in 5 scenarios, was unchanged in 16, and regressed in 14.
The regressed top-1 breakdown most often contained metric anomaly (10),
failure event (9), trace error propagation (9), and alert linkage (4). These
contributions are inspectable per candidate in the `.local` frozen outputs.

Accounting: `provider_invocations = 0`; ground-truth access during output
construction was `0`. The post-hoc evaluator accessed truth only after each
ablation output had been written. No E11 live run was performed.
