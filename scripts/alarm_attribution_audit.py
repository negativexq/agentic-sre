"""Read-only audit: does a Service alert's own measurement attribute it to the pods a strong claim witnesses?

For each recent-era incident (its widest evidence window, engine as checked out) the script reads the alert's rule
from Prometheus, finds the evaluation ticks it was pending through, and decomposes the rule's terms by
``(job, exported_instance)``. OpenTelemetry instances are mapped to pods through stored spans
(``service.instance.id`` and ``k8s.pod.name``) and to pod UIDs through the journal, over a bounded interval.
Every Prometheus response is saved. Nothing is written to a database; sessions are read-only.

    python -m scripts.alarm_attribution_audit --prometheus http://localhost:19090 --output DIR [--workers N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from packages.rca import resolution
from packages.rca.engine import EngineConfig, _selected_pods, build_case, diagnose_case
from packages.rca.model import (
    EntityRef,
    ObjectVersion,
    PodStatusObservation,
    RootSupportStatus,
    TraceSpanObservation,
)
from packages.rca.replay import ReplaySource
from packages.rca.service_effect import _name_holder, _presence
from scripts.dependency_coverage_recalculation import readonly_engine

RECENT = re.compile(r"^testbed_(holdout(19|2[0-2])|dev(19|2[0-3]))[a-z]*_")
# the forms decomposed here: a sum of one rate, or a ratio of two sums of rates, against a constant
SIMPLE = re.compile(r"^(?P<expr>sum\(rate\(.*\)\)(?: / sum\(rate\(.*\)\))?) > (?P<thr>[0-9.]+)$")
SELECTOR = re.compile(r"rate\((?P<sel>[a-z_]+\{[^}]*\})\[(?P<range>\d+)s\]\)")
STEP = timedelta(seconds=5)  # the rule evaluation interval (infra/kubernetes/observability.yaml)
LOOKBACK = timedelta(minutes=2)


def fetch(base: str, path: str, params: Mapping[str, str]) -> Any:
    url = f"{base}/api/v1/{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def stamp(at: datetime) -> str:
    return f"{at.timestamp():.3f}"


def by_instance(expr: str) -> str:
    return expr.replace("sum(", "sum by (job, exported_instance) (", 1)


def series(result: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], dict[float, float]]:
    out: dict[tuple[str, str], dict[float, float]] = {}
    for item in result:
        key = (item["metric"].get("job", ""), item["metric"].get("exported_instance", ""))
        out[key] = {float(t): float(v) for t, v in item["values"]}
    return out


def instance_pods(
    spans: Sequence[TraceSpanObservation],
) -> dict[str, tuple[set[str], datetime, datetime]]:
    """Each process instance: the pod names its spans carry, and the time its spans cover."""
    out: dict[str, tuple[set[str], datetime, datetime]] = {}
    for span in spans:
        instance = span.semantic_attributes.get("service.instance.id")
        pod = span.semantic_attributes.get("k8s.pod.name")
        if not instance or not pod:
            continue
        names, first, last = out.get(instance, (set(), span.start_at, span.start_at))
        names.add(pod)
        out[instance] = (names, min(first, span.start_at), max(last, span.start_at))
    return out


def attribute(
    base: str,
    raw: dict[str, Any],
    alert_name: str,
    rule: str,
    hold: float,
    at: datetime,
    service: EntityRef,
    selected: frozenset[EntityRef],
    witnessed: set[EntityRef],
    instances: Mapping[str, tuple[set[str], datetime, datetime]],
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
    statuses: Sequence[PodStatusObservation],
) -> dict[str, Any]:
    match = SIMPLE.match(rule)
    if match is None:
        return {"class": "NOT_DECOMPOSABLE"}
    expr, threshold = match["expr"], float(match["thr"])
    terms = expr.split(" / ")
    selectors = [m.groupdict() for m in SELECTOR.finditer(expr)]
    window = timedelta(seconds=int(selectors[0]["range"]))
    start, end = at - LOOKBACK, at + timedelta(seconds=30)
    end = max(end, at + timedelta(seconds=hold + 10))
    params = {"start": stamp(start), "end": stamp(end), "step": "5"}
    timing = fetch(base, "query_range", {"query": expr, **params})
    raw[f"{alert_name}:timing"] = timing
    values = {
        round(float(t), 3): float(v) for r in timing["data"]["result"] for t, v in r["values"]
    }
    ticks = sorted(values)
    fired = round(at.timestamp(), 3)
    if fired not in values or values[fired] <= threshold:
        return {"class": "UNKNOWN", "reason": "ALERT_START_NOT_A_TRUE_TICK", "threshold": threshold}
    active = fired
    for tick in reversed([t for t in ticks if t < fired]):
        if values[tick] <= threshold or fired - tick > 600:
            break
        active = tick
    # the alert's start is its first true tick (activeAt) or the tick it fired after ``for`` (firedAt)
    if fired - active + 0.5 < hold:
        held_until = fired + hold
        if not all(values.get(t, 0) > threshold for t in ticks if fired <= t <= held_until):
            return {
                "class": "UNKNOWN",
                "reason": "HOLD_NOT_TRUE_AFTER_START",
                "threshold": threshold,
            }
        fired_at = held_until
    else:
        fired_at = fired
    pending = [t for t in ticks if active <= t <= fired_at]
    decomposed = []
    for term in terms:
        response = fetch(base, "query_range", {"query": by_instance(term), **params})
        raw[f"{alert_name}:{term}"] = response
        decomposed.append(series(response["data"]["result"]))
    # completeness is read on the metric family the process exports, not on the rule's filtered series: a pod
    # that never failed has no 5xx series, though it exported its counters
    metric = selectors[-1]["sel"].split("{")[0]
    owner = re.search(r'service="[^"]+"', selectors[-1]["sel"])
    family = f"{metric}{{{owner.group(0) if owner else ''}}}"
    counts = fetch(
        base,
        "query_range",
        {
            "query": f"max by (job, exported_instance) (count_over_time({family}[{window.seconds}s]))",
            **params,
        },
    )
    raw[f"{alert_name}:samples"] = counts
    samples = series(counts["data"]["result"])
    # kubelet probes reach every running pod every 5 to 10 s, so a working export always advances the pod's request
    # counter within one range; a series that holds still is a stalled export, not a pod without requests
    service_label = owner.group(0) if owner else ""
    beat = fetch(
        base,
        "query_range",
        {
            "query": f"sum by (job, exported_instance) (rate(http_requests_total{{{service_label}}}[{window.seconds}s]))",
            **params,
        },
    )
    raw[f"{alert_name}:liveness"] = beat
    liveness = series(beat["data"]["result"])
    # the bounded interval the rule read: its range before the first pending tick, to the firing tick
    w0 = datetime.fromtimestamp(active, at.tzinfo) - window
    w1 = datetime.fromtimestamp(fired_at, at.tzinfo)
    out: dict[str, Any] = {
        "threshold": threshold,
        "form": "RATIO" if len(terms) == 2 else "RATE",
        "active_at": datetime.fromtimestamp(active, at.tzinfo).isoformat(),
        "start_is": "ACTIVE_AT" if fired == active and hold else "FIRED_AT",
        "pending_longer_than_for": fired_at - active > hold + 5,
        "pending_seconds": round(fired_at - active, 3),
        "ticks": len(pending),
    }
    # instance -> pod -> UID, each over the instance's own spans and the rule's interval
    mapping: dict[str, EntityRef] = {}
    unknown: list[str] = []
    active_instances = {
        inst
        for (job, inst), by_tick in samples.items()
        if job == "otel-collector" and any(by_tick.get(t, 0) >= 1 for t in pending)
    }
    for inst in active_instances:
        names, seen_from, seen_to = instances.get(inst, (set(), w0, w1))
        if len(names) != 1:
            unknown.append(f"INSTANCE_NOT_MAPPED:{inst}")
            continue
        pod = EntityRef(kind="Pod", name=next(iter(names)), namespace=service.namespace)
        if _name_holder(history.get(pod, ()), min(seen_from, w0), max(seen_to, w1)) is None:
            unknown.append(f"POD_UID_NOT_UNIQUE:{pod.name}")
            continue
        if pod not in selected:
            unknown.append(f"INSTANCE_POD_NOT_SELECTED:{pod.name}")
            continue
        mapping[inst] = pod
    # every selected pod present in the interval and possibly Ready needs a series with a rate at every tick
    serving: set[EntityRef] = set()
    for pod in selected:
        versions = history.get(pod, ())
        if not versions:
            unknown.append(f"SELECTED_POD_NOT_IN_JOURNAL:{pod.name}")
            continue
        created, deleted = _presence(versions)
        if created > w1 or (deleted is not None and deleted < w0):
            continue
        observed = sorted((s for s in statuses if s.pod == pod), key=lambda s: s.observed_at)
        before = [s for s in observed if s.observed_at <= w0]
        if (
            before
            and before[-1].ready is False
            and all(s.ready is False for s in observed if w0 <= s.observed_at <= w1)
        ):
            continue  # known not Ready over the whole interval: it served nothing
        serving.add(pod)
        served = [i for i, p in mapping.items() if p == pod]
        if not served or not all(
            any(samples.get(("otel-collector", i), {}).get(t, 0) >= 2 for i in served)
            for t in pending
        ):
            unknown.append(f"SELECTED_POD_SERIES_MISSING:{pod.name}")
        elif not all(
            any(liveness.get(("otel-collector", i), {}).get(t, 0) > 0 for i in served)
            for t in pending
        ):
            unknown.append(f"SERIES_NOT_ADVANCING:{pod.name}")
    out["unknown"] = sorted(set(unknown))
    out["instances"] = {i: p.name for i, p in mapping.items()}
    mine = {i for i, p in mapping.items() if p in witnessed}
    out["witnessed_instances"] = sorted(mine)
    # the Service-DNS series came from whichever pod answered the scrape: attributable only when every pod that
    # could have answered is witnessed
    whole = bool(serving) and serving <= witnessed
    out["direct_attributable"] = whole
    checks = []
    for tick in pending:
        rows = [{key: by_tick.get(tick) for key, by_tick in d.items()} for d in decomposed]
        numerator = rows[0]
        otel = {k: v for k, v in numerator.items() if k[0] == "otel-collector" and v is not None}
        direct = sum(v for k, v in numerator.items() if k[0] != "otel-collector" and v is not None)
        own = sum(v for k, v in otel.items() if k[1] in mine) + (direct if whole else 0.0)
        check: dict[str, Any] = {"tick": tick, "value": values[tick]}
        if len(rows) == 1:
            check.update(
                own=own, otel=sum(otel.values()), direct=direct, sufficient=own > threshold
            )
        else:
            counts_ = rows[1]
            total = sum(v for v in counts_.values() if v is not None)
            own_count = sum(
                v
                for k, v in counts_.items()
                if v
                and (
                    (k[0] == "otel-collector" and k[1] in mine)
                    or (whole and k[0] != "otel-collector")
                )
            )
            others = {k: v for k, v in otel.items() if k[1] not in mine}
            others_count = sum(
                v for k, v in counts_.items() if k[0] == "otel-collector" and k[1] not in mine and v
            )
            check.update(
                own_sum=own,
                own_count=own_count,
                all_count=total,
                share=own / total if total else None,
                own_mean=own / own_count if own_count else None,
                others_mean=sum(others.values()) / others_count if others_count else None,
                sufficient=bool(total) and own / total > threshold,
            )
        checks.append(check)
    out["checks"] = checks
    # informational: each mapped pod whose own OpenTelemetry share alone crosses the threshold at every tick
    out["explaining_pods"] = sorted(
        {
            pod.name
            for pod in set(mapping.values())
            if all(
                _share(decomposed, tick, {i for i, p in mapping.items() if p == pod}) > threshold
                for tick in pending
            )
        }
    )
    gaps = [
        u
        for u in out["unknown"]
        if not u.startswith(("SELECTED_POD_SERIES_MISSING:", "SERIES_NOT_ADVANCING:"))
    ]
    sufficient = bool(checks) and all(c["sufficient"] for c in checks)
    if not mine:
        out["class"] = "NO_WITNESSED_INSTANCE" if not out["unknown"] else "UNKNOWN"
    elif gaps:
        out["class"] = "UNKNOWN"
    elif out["unknown"]:
        # the alert's own value is explained by the series it read, but a serving pod lacked samples: not proof
        out["class"] = "EXPLAINS_WITH_SERIES_GAP" if sufficient else "UNKNOWN_SERIES_GAP"
    else:
        out["class"] = "EXPLAINS" if sufficient else "NOT_SUFFICIENT"
    return out


def _share(
    decomposed: Sequence[Mapping[tuple[str, str], Mapping[float, float]]],
    tick: float,
    instances: set[str],
) -> float:
    numerator = sum(
        v.get(tick, 0.0)
        for k, v in decomposed[0].items()
        if k[0] == "otel-collector" and k[1] in instances
    )
    if len(decomposed) == 1:
        return numerator
    total = sum(v.get(tick, 0.0) for v in decomposed[1].values())
    return numerator / total if total else 0.0


def one(job: tuple[str, str, Mapping[str, tuple[str, float]], str, str]) -> list[dict[str, Any]]:
    database, base, rules, raw_dir, _ = job
    engine = readonly_engine(database)
    rows: list[dict[str, Any]] = []
    try:
        with engine.connect() as connection:
            runs = [
                str(r[0])
                for r in connection.execute(
                    text(
                        "select distinct on (incident_id) payload->>'run_id' from incident_events"
                        " where event_type = 'EVIDENCE_GATHERED'"
                        " order by incident_id, (payload->>'window_end') desc, event_id desc"
                    )
                )
            ]
        factory = sessionmaker(engine)
        for run in runs:
            try:
                source = ReplaySource.from_run(run, session_factory=factory)
                case = build_case(source, EngineConfig())
                diagnosis = diagnose_case(case, config=EngineConfig())
                trace = diagnosis.resolution_trace
                assert trace is not None
                selects = _selected_pods(case.context.topology)
                owned_by = {
                    e.source: e.target
                    for e in case.context.topology.edges
                    if e.relation == "owned_by"
                }
                statuses = source.pod_status_observations()
                history = source.object_history()
                holders = {h.hypothesis_id: h for h in case.hypotheses}
                claims = []
                for audit in trace.hypothesis_audits:
                    if audit.hypothesis_id not in trace.mechanism_verified_hypotheses:
                        continue
                    witnesses = [
                        w
                        for r in audit.root_support
                        if r.status is RootSupportStatus.FIRED
                        and r.support_kind == "OBSERVED_MECHANISM_CAUSE"
                        for w in r.witnesses
                    ]
                    covered = resolution._covered_symptoms(witnesses, owned_by, selects, statuses)
                    holder = holders[audit.hypothesis_id]
                    claims.append(
                        {
                            "id": audit.hypothesis_id,
                            "actor": holder.causal_actor.canonical,
                            "covered": sorted(e.canonical for e in covered),
                            "uncovered": sorted(
                                e.canonical for e in holder.symptom_entities if e not in covered
                            ),
                        }
                    )
                instances = instance_pods(source.trace_observations())
                raw: dict[str, Any] = {}
                alerts = []
                for alert in source.alerts():
                    service = EntityRef(
                        kind="Service", name=alert.service or "", namespace=alert.namespace or ""
                    )
                    witnessed = {
                        EntityRef.model_validate(_ref(c))
                        for claim in claims
                        for c in claim["covered"]
                        if c.split("/")[1] == "Pod"
                    }
                    record = {
                        "alert": alert.name,
                        "at": alert.starts_at.isoformat(),
                        "service": service.canonical,
                    }
                    try:
                        record.update(
                            attribute(
                                base,
                                raw,
                                alert.name,
                                rules.get(alert.name, ("", 0.0))[0],
                                rules.get(alert.name, ("", 0.0))[1],
                                alert.starts_at,
                                service,
                                selects.get(service, frozenset()),
                                witnessed,
                                instances,
                                history,
                                statuses,
                            )
                        )
                    except Exception as error:  # a failed read is unknown, never a result
                        record.update(
                            {"class": "UNKNOWN", "reason": f"READ_FAILED:{error!r}"[:200]}
                        )
                    alerts.append(record)
                (Path(raw_dir) / f"{database}__{run}.json").write_text(json.dumps(raw))
                rows.append(
                    {
                        "database": database,
                        "run": run,
                        "incident": case.incident_id,
                        "resolution": diagnosis.resolution.value,
                        "root": diagnosis.root_cause.canonical if diagnosis.root_cause else None,
                        "supported": sorted(trace.plausible_hypotheses),
                        "strong": sorted(trace.mechanism_verified_hypotheses),
                        "unresolved": sorted(trace.unresolved_hypotheses),
                        "limiting": sorted(trace.material_frontier_ids),
                        "claims": claims,
                        "alerts": alerts,
                    }
                )
            except Exception as error:
                rows.append({"database": database, "run": run, "error": repr(error)[:300]})
    finally:
        engine.dispose()
    return rows


def _ref(canonical: str) -> dict[str, str]:
    namespace, kind, name = canonical.split("/", 2)
    return {"kind": kind, "name": name, "namespace": namespace}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    raw_dir = args.output / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    rules_response = fetch(args.prometheus, "rules", {})
    (args.output / "rules.json").write_text(json.dumps(rules_response, indent=1))
    rules = {
        r["name"]: (r["query"], float(r.get("duration", 0)))
        for g in rules_response["data"]["groups"]
        for r in g["rules"]
    }
    engine = readonly_engine("postgres")
    with engine.connect() as connection:
        databases = [
            r[0]
            for r in connection.execute(
                text("select datname from pg_database where datname like 'testbed_%' order by 1")
            )
            if RECENT.match(r[0])
        ]
    engine.dispose()
    (args.output / "provenance.json").write_text(
        json.dumps(
            {
                "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "prometheus": args.prometheus,
                "databases": databases,
                "read_at": datetime.now().astimezone().isoformat(),
                "database_access": "default_transaction_read_only=on",
            },
            indent=1,
        )
    )
    counts: Counter[str] = Counter()
    jobs = [(d, args.prometheus, rules, str(raw_dir), "") for d in databases]
    with (
        (args.output / "rows.jsonl").open("w") as output,
        ProcessPoolExecutor(args.workers) as pool,
    ):
        for number, future in enumerate(as_completed([pool.submit(one, j) for j in jobs]), 1):
            for row in future.result():
                output.write(json.dumps(row, default=str) + "\n")
                counts["runs"] += 1
                counts["errors"] += "error" in row
                for alert in row.get("alerts", ()):
                    counts[alert["class"]] += 1
            if number % 10 == 0 or number == len(jobs):
                print(number, len(jobs), dict(counts), flush=True)


if __name__ == "__main__":
    main()
