# Chaos experiment objects in the journal

Status: **APPROVED** by the owner (2026-10-02) and **implemented** (the reader lists the kinds in every journaled
namespace; a namespace without the CRD is skipped, not failed). Adoption waits for the measurement of §3. In the lab
the listing grows from 28 to 40 object scopes (12 new Chaos scopes, none failed), so from 31 to 43 watches.

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

## 5. Live finding (2026-10-02)

The first live run after the change failed at once: the lab Connector's service account could read Chaos kinds in
`chaos-mesh` only, every journaled namespace answered 403, each counted as a failed scope, the listing was never
complete, and the Connector relisted every five seconds instead of watching (126 global relists, 5,504 LISTs in 12
minutes). The read-only check before had run with an administrator's credentials. Fixed twice over: outside the chaos
namespaces a 403 or 404 skips the scope with one warning instead of failing it (an optional kind must never stop the
stream), and the lab's reader roles in `sre-demo` and `lab-control` grant read access to the Chaos kinds.

After the fix the same run journaled every experiment it created (`NetworkChaos` `lr-0`, `lr-3`; `StressChaos` `lr-1`,
`lr-4`) with 43 watches and no relisting. The measurement of §3 (slices 1 to 3 against their frozen results) is next.
