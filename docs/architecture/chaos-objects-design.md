# Chaos experiment objects in the journal

Status: **PROPOSED** (2026-10-02). Owner decision requested: it changes what the engine receives, so testbed results
can change.

## 1. Problem (measured)

The Kubernetes reader lists Chaos Mesh kinds (`NetworkChaos`, `StressChaos`, `PodChaos`, `IOChaos`, `HTTPChaos`,
`Schedule`) only in its configured chaos namespaces (`chaos_namespaces`, `chaos-mesh` by default,
`packages/rca/live.py`). Experiments are created where their targets live: the lab creates them in `sre-demo`, and a
customer may do the same. None of the 36 testbed databases, from the polling period through the ten-hour
product-mode run, contains a single Chaos object version; the engine saw every fault only through its Events
(`Applied`, `Recovered` and their messages).

The engine does use these objects when it has them: the topology and mechanism bridge read an experiment's selector
to find its targets, the channel catalog and the information-gap rules know the kinds, and the ITBench snapshots
contain them. In the lab that input was always missing. Roadmap C2 ("observe the fault action, `spec.action`")
depends on it.

## 2. Rule

List the Chaos Mesh kinds in **every journaled namespace** (watched and evidence namespaces), as well as in the
chaos namespaces; watch them like any other scope (they receive real bookmarks; contract §15). Secrets stay denied;
nothing else changes. Experiment objects are journaled as object versions with their Connector observation time, so
membership and coverage (C9) apply to them unchanged.

## 3. Measurement before adoption

1. **Replay is unaffected:** recorded runs replay from their manifests, which do not contain these objects.
2. **Testbed:** re-run slices 1 to 3 (three repeats each) and report against the frozen results: execution witness
   and effect link per run, cause and instance named, false strong authority, false `RESOLVED`. The acceptance bar
   is the frozen one: no false strong authority, no false `RESOLVED`; any change in the named cause is explained run
   by run.
3. **Cost:** scopes and API requests before and after (six more scopes per journaled namespace, with bookmarks).

## 4. Decision requested

The rule of §2, measured as §3 before it counts as adopted.
