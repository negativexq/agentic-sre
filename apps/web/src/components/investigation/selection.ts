import type { ChangeView, DiagnosisView, FindingView } from "@/api/types";

export type CanvasSelection =
  | { kind: "all" }
  | { kind: "resource"; id: string }
  | { kind: "change"; id: string }
  | { kind: "turn"; id: string }
  | { kind: "finding"; id: string };
export type CanvasFocus = {
  active: boolean;
  label: string;
  note: string;
  resources: Set<string>;
  evidenceIds: Set<string>;
  hypothesisIds: Set<string>;
  findings: FindingView[];
  turnIndex: number | null;
  changeId: string | null;
};
export function findingKey(finding: FindingView) {
  return JSON.stringify([
    finding.entity,
    finding.kind,
    finding.at,
    finding.evidence_ids,
  ]);
}
export function changeIdentity(change: ChangeView): string | null {
  return change.namespace
    ? `${change.namespace}/${change.resource_type}/${change.resource_name}`
    : null;
}
/** Reference joins only. No time/name similarity, inferred topology or historic decisions. */
export function resolveCanvasFocus(
  diagnosis: DiagnosisView,
  changes: ChangeView[],
  selection: CanvasSelection,
): CanvasFocus {
  const focus: CanvasFocus = {
    active: selection.kind !== "all",
    label: "All incident context",
    note: "",
    resources: new Set(),
    evidenceIds: new Set(),
    hypothesisIds: new Set(),
    findings: [],
    turnIndex: null,
    changeId: null,
  };
  if (selection.kind === "all") return focus;
  const knownResources = new Set([
    ...diagnosis.evidence.map((finding) => finding.entity),
    ...(diagnosis.causal_path ?? []).flatMap((hop) => [hop.source, hop.target]),
    ...diagnosis.competing_hypotheses.map((hypothesis) => hypothesis.actor),
    ...(diagnosis.executing_instances ?? []).flatMap((witness) => [
      witness.actor,
      witness.instance,
      ...(witness.target ? [witness.target] : []),
    ]),
  ]);
  if (selection.kind === "resource") {
    focus.label = selection.id;
    if (knownResources.has(selection.id)) focus.resources.add(selection.id);
    focus.note = knownResources.has(selection.id)
      ? "Exact recorded resource identity. Selection is context, not a causal conclusion."
      : "This resource identity is not included in the current incident records.";
    focus.findings = diagnosis.evidence.filter(
      (finding) => finding.entity === selection.id,
    );
  } else if (selection.kind === "change") {
    const change = changes.find((change) => change.change_id === selection.id);
    focus.changeId = selection.id;
    focus.label = change
      ? `Change · ${change.resource_type}/${change.resource_name}`
      : "Change not included in the current window";
    const identity = change && changeIdentity(change);
    if (identity) {
      focus.resources.add(identity);
      focus.findings = diagnosis.evidence.filter(
        (finding) => finding.entity === identity,
      );
    }
    focus.note = identity
      ? "Matched by explicit namespace, kind and name. Temporal correlation is not causation."
      : "Namespace is not recorded. No resource or evidence relationship is inferred from kind/name alone.";
  } else if (selection.kind === "finding") {
    focus.findings = diagnosis.evidence.filter(
      (finding) => findingKey(finding) === selection.id,
    );
    focus.label =
      focus.findings[0]?.summary ??
      "Finding not included in the current diagnosis";
    focus.note =
      "Selected normalized finding and its explicit evidence references.";
  } else {
    const turn = diagnosis.investigation_audit?.action_audits.find(
      (turn) => String(turn.turn_index) === selection.id,
    );
    focus.label = turn
      ? `Recorded turn ${turn.turn_index} · ${turn.action}`
      : "Turn not included in the current audit";
    focus.note =
      "Recorded action references against the current diagnosis. Historical hypotheses, graph and diagnosis state are not reconstructed.";
    if (turn) {
      focus.turnIndex = turn.turn_index;
      turn.returned_evidence_refs.forEach((id) => focus.evidenceIds.add(id));
      turn.affected_hypothesis_ids.forEach((id) => focus.hypothesisIds.add(id));
      if (turn.target && knownResources.has(turn.target))
        focus.resources.add(turn.target);
      focus.findings = diagnosis.evidence.filter((finding) =>
        finding.evidence_ids.some((id) => focus.evidenceIds.has(id)),
      );
    }
  }
  focus.findings.forEach((finding) => {
    focus.resources.add(finding.entity);
    if (selection.kind !== "turn")
      finding.evidence_ids.forEach((id) => focus.evidenceIds.add(id));
  });
  // Resource identity is an explicit actor association, not evidence that this hypothesis is true.
  diagnosis.competing_hypotheses.forEach((hypothesis) => {
    if (selection.kind === "turn") {
      if (focus.hypothesisIds.has(hypothesis.hypothesis_id))
        focus.resources.add(hypothesis.actor);
    } else if (focus.resources.has(hypothesis.actor))
      focus.hypothesisIds.add(hypothesis.hypothesis_id);
  });
  return focus;
}
