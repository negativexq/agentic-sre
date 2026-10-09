import { describe, expect, it } from "vitest";
import type {
  ChangeView,
  DiagnosisView,
  FindingView,
  InvestigationActionAuditView,
} from "@/api/types";
import { resolveCanvasFocus } from "./selection";
import { layoutRecordedGraph } from "./graph";
const findings: FindingView[] = [
  {
    kind: "CHANGE",
    entity: "one/Deployment/payment",
    at: null,
    summary: "one",
    temporal_role: "INITIATING",
    onset_delta_seconds: null,
    evidence_ids: ["ref-one", "shared"],
  },
  {
    kind: "CHANGE",
    entity: "two/Deployment/payment",
    at: null,
    summary: "two",
    temporal_role: "SUPPORTING",
    onset_delta_seconds: null,
    evidence_ids: ["ref-two"],
  },
];
const diagnosis = {
  evidence: findings,
  competing_hypotheses: [
    { hypothesis_id: "hyp-one", actor: findings[0].entity },
  ],
  investigation_audit: {
    action_audits: [
      {
        turn_index: 1,
        target: findings[1].entity,
        returned_evidence_refs: ["ref-one"],
        affected_hypothesis_ids: ["hyp-one", "absent"],
      } as InvestigationActionAuditView,
    ],
  },
} as DiagnosisView;
const change = {
  change_id: "change-one",
  resource_type: "Deployment",
  resource_name: "payment",
  namespace: "one",
} as ChangeView;

describe("shared canvas reference joins", () => {
  it("distinguishes same-named resources across namespaces and refuses missing namespace", () => {
    expect(
      resolveCanvasFocus(diagnosis, [change], {
        kind: "change",
        id: change.change_id,
      }).findings,
    ).toEqual([findings[0]]);
    const missing = resolveCanvasFocus(
      diagnosis,
      [{ ...change, namespace: null }],
      { kind: "change", id: change.change_id },
    );
    expect(missing.findings).toEqual([]);
    expect(missing.resources.size).toBe(0);
    expect(missing.note).toContain("Namespace is not recorded");
  });
  it("turns use returned references and affected IDs without expanding admitted evidence or inventing historical states", () => {
    const focus = resolveCanvasFocus(diagnosis, [], { kind: "turn", id: "1" });
    expect(focus.findings).toEqual([findings[0]]);
    expect([...focus.evidenceIds]).toEqual(["ref-one"]);
    expect([...focus.hypothesisIds]).toEqual(["hyp-one", "absent"]);
    expect(focus.note).toContain("not reconstructed");
  });
  it("invalid and cleared selections retain explicit empty states", () => {
    expect(
      resolveCanvasFocus(diagnosis, [], { kind: "turn", id: "999" }).findings,
    ).toEqual([]);
    expect(resolveCanvasFocus(diagnosis, [], { kind: "all" }).active).toBe(
      false,
    );
  });
});

describe("recorded graph layout", () => {
  it("retains parallel edges, self edges, cycles and disconnected components without adding edges", () => {
    const hops = [
      { source: "a", target: "b", relation: "one", direction: "forward" },
      { source: "a", target: "b", relation: "two", direction: "forward" },
      { source: "b", target: "a", relation: "return", direction: "backward" },
      { source: "c", target: "c", relation: "self", direction: "forward" },
      {
        source: "other/a",
        target: "other/b",
        relation: "other",
        direction: "forward",
      },
    ];
    const graph = layoutRecordedGraph(hops);
    expect(graph).toHaveLength(3);
    expect(
      graph.flatMap((group) => group.edges).map((edge) => edge.hop),
    ).toEqual(hops);
    expect(
      graph.flatMap((group) => group.nodes).map((node) => node.id),
    ).toEqual(["a", "b", "c", "other/a", "other/b"]);
    expect(graph).toEqual(layoutRecordedGraph(hops));
    expect(layoutRecordedGraph([])).toEqual([]);
  });
});
