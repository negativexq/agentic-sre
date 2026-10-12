# Alarm attribution: does the alert's own measurement explain a Service alert?

2026-10-12, engine 2.13.0 at `045e9fd`. Read-only development audit; no engine rule, lab setting or stored record is
changed. Scripts: `scripts/alarm_attribution_audit.py`, `scripts/alarm_attribution_summary.py`.

## Question

§32 R5 found that a Service alert's crossing cannot be shown from sampled spans for mean-latency alerts and for
metrics no span measures. This audit asks whether the alert's own measurement, read from Prometheus, attributes the
crossing to the pods a strong claim witnesses, and what that would change.

## Method

- **Population:** the recent era (nineteenth to twenty-second `HOLDOUT`, `DEV` 19–23), each incident's widest evidence
  window: 108 databases, 488 diagnoses, recalculated on engine 2.13.0. All are development data.
- **Rules:** read from the lab Prometheus (`/api/v1/rules`) and saved with the output. They are today's rules; a run
  recorded under an earlier rule set is read with today's expression.
- **Retention:** Prometheus keeps 15 days; the recent era lies inside it. Stored runs hold no Prometheus series, so
  nothing here can be replayed later from the stored record alone.
- **Decomposed forms:** `sum(rate(X[r])) > c` and `sum(rate(S[r])) / sum(rate(N[r])) > c`. Lag alerts, `changes()`
  and the composite `OrderRequestLatencyHigh` are not decomposed (207 of 488 alert occurrences).
- **Timing.** For each alert the rule expression is read at the evaluation ticks around its stored `starts_at`. The
  stored `starts_at` is the firing tick in all 281 decomposed alerts: the expression is true from `starts_at − for` to
  `starts_at` (15 s for most rules, 0 s for three). Every tick of that pending interval is checked, each over the
  rule's own range. In 20 alerts the expression was already true before the pending interval began (recorded,
  `pending_longer_than_for`).
- **Terms by instance.** Each term is read `by (job, exported_instance)`. Two jobs carry `service="..."`:
  `otel-collector` (one series per process, `exported_instance` = `service.instance.id`) and a direct scrape of the
  Service's DNS name, which reaches whichever pod answers each scrape.
- **Instance → pod → UID, bounded.** A process instance maps to a pod only when all its stored spans carry one
  `k8s.pod.name`; the name maps to one UID only when the journal shows exactly one holder over the instance's span
  interval joined with the rule's interval (`_name_holder`). The pod must be selected by the alert's Service.
- **Completeness, not zero.** Every selected pod present in the interval and not known not-`Ready` throughout must
  have an instance with at least two samples of the metric family at every tick (`SELECTED_POD_SERIES_MISSING`
  otherwise), and its request counter must advance at every tick (`SERIES_NOT_ADVANCING` otherwise: kubelet probes
  reach every running pod every 5–10 s, so a working export always advances it). An active instance that cannot be
  mapped is `INSTANCE_NOT_MAPPED`. None of these is read as zero.
- **Sufficiency, by numerator and denominator.** The witnessed pods are the Pods the diagnosis's strong claims cover.
  - Rate alert: the witnessed instances' rate alone exceeds the threshold at every tick.
  - Ratio alert: the witnessed instances' numerator divided by the **whole** denominator (every OpenTelemetry
    instance and the direct scrape) exceeds the threshold at every tick: even if every other request took no time,
    the alert would cross.
  - The direct-scrape series is attributed to the witnessed pods only when every pod that could have answered the
    scrape is witnessed; otherwise its numerator counts for no one and its denominator still counts.
- **Classes:** `EXPLAINS` (complete and sufficient at every tick), `NOT_SUFFICIENT`, `EXPLAINS_WITH_SERIES_GAP` and
  `UNKNOWN_SERIES_GAP` (sufficient or not over the series present, with a serving pod's series missing or stalled),
  `UNKNOWN` (an unmapped instance, a UID not unique, a pod outside the Service), `NO_WITNESSED_INSTANCE`.
- Every Prometheus response is saved per run; ground truth labels the result only.

## Findings

**1. The alert expressions mix two sources.** Both the OpenTelemetry series and the Service-DNS scrape carry
`service="payment-service"` (likewise for `order-service`), and the rules sum both. The direct series cannot be tied
to a pod, and because successive scrapes may reach different pods, its counter can jump between pods' counters,
which `rate()` reads as resets. The problem is not only double counting: one of the two terms is not a measurement of
any one process.

**2. The stored `starts_at` is the firing tick,** in all 281 decomposed alerts; the pending interval is `for` before it.

**3. Alert classes** (281 decomposed alert occurrences):

| Class | Alerts |
|---|---:|
| `EXPLAINS` | 117 |
| `EXPLAINS_WITH_SERIES_GAP` | 16 |
| `UNKNOWN_SERIES_GAP` | 18 |
| `UNKNOWN` | 32 |
| `NO_WITNESSED_INSTANCE` | 98 |
| `NOT_SUFFICIENT` | 0 |

- **Gaps** (58 `SERIES_NOT_ADVANCING`, 8 `SELECTED_POD_SERIES_MISSING`, 4 `INSTANCE_NOT_MAPPED`; an alert may carry
  several). The stalled series are concentrated in network-loss runs: the faulted pod cannot export, the collector
  keeps re-exposing the last value, and the series reads as a rate of 0 while the direct scrape rises. Before the
  advancement check these 18 alerts read as `NOT_SUFFICIENT`; they are unknown, not insufficient.
- **A pod's own mean is not its contribution.** The share test divides the witnessed pods' numerator by the whole
  denominator. In this population it agrees with the witnessed pods' own `sum/count` on all 107 ratio alerts that
  have witnessed instances (89 pass both, 18 pass neither), because the witnessed pods carry nearly all requests and
  the direct series is attributable to them. The first, scratch version of this audit read the pod mean and left the
  direct series out of the denominator; a review found 4 of its 68 "target alone" latency alerts that fail the share
  test. The share test is the one used.

**4. The 49 rollout diagnoses of §32.** None has a witnessed instance (§32's witness does not exist), so alarm
attribution alone cannot cover their Service. Read per pod, informational only: 18 of the 49 alerts have exactly one
pod whose own share explains the alert at every tick, the new revision's pod; 23 are unknown (19 a stalled series,
4 a missing one, mostly a pod started inside the rule's range). The earlier reading "the target explains all 49" was
too strong.

**5. What the evidence would change.** A counterfactual, not a rule: a diagnosis with one strong supported claim
counts a Service as covered when every alert naming that Service is `EXPLAINS`.

| Blockers of the 204 single-strong-claim diagnoses | Now | With explained alerts |
|---|---:|---:|
| none (`RESOLVED`) | 54 | 57 |
| frontier only | 20 | 22 |
| coverage + frontier | 27 | 25 |
| coverage + rival | 3 | 3 |
| coverage + rival + frontier | 43 | 43 |
| rival + frontier | 54 | 54 |
| coverage only | 3 | 0 |

- **3 diagnoses would become `RESOLVED`,** all three in one `DEV` run, `dev19-two-replicas/direct-stress-payment/0`;
  their leader is the run's cause. Both payment pods are witnessed by the same `StressChaos` claim, so the direct
  series is attributable and the Service alerts are explained. These are the same three found by the collective
  replica coverage audit; this route needs no new coverage rule for them, but it is one run, and that run was
  pre-registered (testbed design §35) as a control for no Service coverage at two replicas.
- **The largest coverage blocker is not a Service.** Of the 76 single-strong-claim diagnoses with uncovered
  symptoms, 68 are payment Deployment rollouts whose uncovered symptoms are `order-service`'s Deployment and Pod; 5
  are a Service alone; 3 a payment Pod and Service. Alarm attribution does not reach the 68.

## Limits

- One Prometheus, today's rules, 15 days of retention; no stored run holds these series.
- The journal's pod population is assumed complete, as in §33 and §34.
- A pending interval that began before `starts_at − for` (20 alerts) is checked from its first true tick.
- Counts are diagnoses of development runs; nothing here validates a rule.

## Reproduction

With the lab Prometheus forwarded to `localhost:19090`:

```sh
.venv/bin/python -m scripts.alarm_attribution_audit --prometheus http://localhost:19090 --output .local/diagnostics/alarm-attribution
.venv/bin/python -m scripts.alarm_attribution_summary .local/diagnostics/alarm-attribution
```

Outputs under `.local/diagnostics/alarm-attribution/`: `rules.json`, `provenance.json`, `rows.jsonl`,
`summary.json`, and every Prometheus response in `raw/`. Re-running after the retention period has passed reads
nothing; the saved responses are the record.
