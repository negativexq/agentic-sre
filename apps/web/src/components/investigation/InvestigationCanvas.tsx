import { useMemo, type ReactNode } from "react";
import type { ChangeView, DiagnosisView, FindingView } from "@/api/types";
import { useUrlFilters } from "@/lib/useUrlFilters";
import { InvestigationContext, useInvestigationCanvas } from "./context";
import { resolveCanvasFocus, type CanvasSelection } from "./selection";
import { CausalXRay } from "./CausalXRay";
import { FindingsList } from "@/components/workspace/FindingsList";
import { Button } from "@/components/ui/Button";

export function InvestigationCanvasProvider({
  diagnosis,
  changes,
  children,
}: {
  diagnosis: DiagnosisView;
  changes: ChangeView[];
  children: ReactNode;
}) {
  const { params, update } = useUrlFilters();
  const requested = params.get("canvas_kind");
  const kind =
    requested ??
    (params.get("investigation_view") === "recorded" ? "turn" : "all");
  const id =
    params.get("canvas_id") ??
    (kind === "turn"
      ? (params.get("replay_turn") ??
        String(
          diagnosis.investigation_audit?.action_audits[0]?.turn_index ?? "",
        ))
      : "");
  const selection: CanvasSelection = useMemo(
    () =>
      ["resource", "change", "finding", "turn"].includes(kind)
        ? { kind: kind as Exclude<CanvasSelection["kind"], "all">, id }
        : { kind: "all" },
    [kind, id],
  );
  const focus = useMemo(
    () => resolveCanvasFocus(diagnosis, changes, selection),
    [diagnosis, changes, selection],
  );
  const select = (value: CanvasSelection) =>
    update({
      canvas_kind: value.kind,
      canvas_id: value.kind === "all" ? null : value.id,
      canvas_evidence: null,
    });
  const expanded =
    params.get("canvas_view") === "open" ||
    (focus.active && params.get("canvas_view") !== "closed");
  return (
    <InvestigationContext.Provider
      value={{ focus, expanded, selection, select }}
    >
      {children}
    </InvestigationContext.Provider>
  );
}

/** This canvas remains mounted across task tabs so selecting a record never loses context. */
export function InvestigationCanvas({
  diagnosis,
  onSelect,
  onOpenResource,
  onOpenHypothesis,
}: {
  diagnosis: DiagnosisView;
  onSelect: (finding: FindingView, findings?: FindingView[]) => void;
  onOpenResource: (resource: string) => void;
  onOpenHypothesis: (id: string) => void;
}) {
  const canvas = useInvestigationCanvas()!;
  const { update } = useUrlFilters();
  const expanded = canvas.expanded;
  const hypotheses = diagnosis.competing_hypotheses.filter((hypothesis) =>
    canvas.focus.hypothesisIds.has(hypothesis.hypothesis_id),
  );
  const actions = diagnosis.investigation_audit?.action_audits ?? [];
  const turnIndex = actions.findIndex(
    (turn) => turn.turn_index === canvas.focus.turnIndex,
  );
  const moveTurn = (index: number) =>
    update({
      replay_turn: String(actions[index].turn_index),
      canvas_kind: "turn",
      canvas_id: String(actions[index].turn_index),
      canvas_evidence: null,
    });
  return (
    <section
      aria-label="Shared investigation canvas"
      id="shared-investigation-canvas"
      tabIndex={-1}
      className="mb-5 min-w-0 scroll-mt-20 rounded-lg border border-border bg-surface-raised outline-none focus-visible:outline-accent"
    >
      <div className="flex flex-wrap items-center gap-3 p-3">
        <span className="text-xs font-semibold">Investigation canvas</span>
        <p
          aria-live="polite"
          className="min-w-0 flex-1 basis-48 break-anywhere text-xs text-accent"
        >
          {canvas.focus.label}
        </p>
        {canvas.selection.kind === "turn" && turnIndex >= 0 && (
          <div
            className="flex flex-wrap items-center gap-2"
            role="group"
            aria-label="Time Machine recorded context"
          >
            <span className="text-[11px] text-subtle">
              Recorded step {turnIndex + 1}/{actions.length}
            </span>
            <Button
              variant="secondary"
              disabled={turnIndex === 0}
              onClick={() => moveTurn(turnIndex - 1)}
            >
              Previous recorded turn
            </Button>
            <Button
              variant="secondary"
              disabled={turnIndex === actions.length - 1}
              onClick={() => moveTurn(turnIndex + 1)}
            >
              Next recorded turn
            </Button>
          </div>
        )}
        {canvas.focus.active && (
          <Button
            variant="ghost"
            onClick={() => canvas.select({ kind: "all" })}
          >
            Clear selection
          </Button>
        )}
        <Button
          variant="secondary"
          aria-expanded={expanded}
          aria-controls="investigation-canvas-content"
          onClick={() => update({ canvas_view: expanded ? "closed" : "open" })}
        >
          {expanded ? "Collapse canvas" : "Open canvas"}
        </Button>
      </div>
      {expanded && (
        <div
          id="investigation-canvas-content"
          className="grid min-w-0 gap-4 border-t border-border p-4 xl:grid-cols-[minmax(0,1fr)_340px]"
        >
          <div className="min-w-0">
            <h2 className="mb-2 text-sm font-semibold">Causal X-Ray</h2>
            <CausalXRay
              hops={diagnosis.causal_path}
              onSelectResource={(resource) => {
                const findings = diagnosis.evidence.filter(
                  (finding) => finding.entity === resource,
                );
                if (findings.length) onSelect(findings[0], findings);
                canvas.select({ kind: "resource", id: resource });
              }}
            />
            {canvas.focus.resources.size > 0 && (
              <div className="mt-3 flex flex-wrap gap-2">
                {[...canvas.focus.resources].map((resource) => (
                  <button
                    key={resource}
                    className="break-anywhere rounded border border-border px-2 py-1 text-left font-mono text-[11px] text-accent"
                    onClick={() => onOpenResource(resource)}
                  >
                    Open resource context for {resource}
                  </button>
                ))}
              </div>
            )}
          </div>
          <aside
            className="min-w-0 space-y-3"
            aria-label="Selected investigation context"
          >
            <p className="text-xs text-muted">
              {canvas.focus.note ||
                "Select a graph node, timeline change, finding or recorded investigation action. The same selection follows you across task views."}
            </p>
            {canvas.focus.active ? (
              <>
                <h3 className="text-xs font-semibold">
                  Linked evidence · {canvas.focus.findings.length} findings
                </h3>
                {canvas.focus.findings.length ? (
                  <FindingsList
                    labelPrefix="Inspect linked finding:"
                    findings={canvas.focus.findings.slice(0, 3)}
                    onSelect={(finding) =>
                      onSelect(finding, canvas.focus.findings)
                    }
                  />
                ) : (
                  <p className="text-xs text-subtle">
                    No normalized findings match the recorded selection.
                  </p>
                )}
                <Button
                  variant="secondary"
                  onClick={() =>
                    update({
                      tab: "evidence",
                      finding_group: null,
                      finding_q: null,
                    })
                  }
                >
                  Explore selected evidence
                </Button>
                {canvas.focus.evidenceIds.size > 0 && (
                  <details>
                    <summary className="text-xs text-accent">
                      Recorded evidence IDs ({canvas.focus.evidenceIds.size})
                    </summary>
                    <p className="mt-2 break-anywhere font-mono text-[11px] text-muted">
                      {[...canvas.focus.evidenceIds].join(" · ")}
                    </p>
                  </details>
                )}
                <h3 className="text-xs font-semibold">
                  Linked hypotheses · {hypotheses.length}
                </h3>
                {hypotheses.map((hypothesis) => (
                  <button
                    key={hypothesis.hypothesis_id}
                    onClick={() => onOpenHypothesis(hypothesis.hypothesis_id)}
                    className="block w-full break-anywhere rounded-md border border-accent/40 bg-accent-soft p-2 text-left text-xs"
                  >
                    {hypothesis.actor}
                    <span className="mt-1 block text-subtle">
                      Current state: {hypothesis.epistemic_state}
                    </span>
                  </button>
                ))}
                {[...canvas.focus.hypothesisIds]
                  .filter(
                    (id) =>
                      !hypotheses.some(
                        (hypothesis) => hypothesis.hypothesis_id === id,
                      ),
                  )
                  .map((id) => (
                    <p
                      key={id}
                      className="break-anywhere font-mono text-[11px] text-subtle"
                    >
                      {id} · hypothesis not included in current diagnosis
                    </p>
                  ))}
                {!canvas.focus.hypothesisIds.size && (
                  <p className="text-xs text-subtle">
                    No hypothesis reference matches this selection.
                  </p>
                )}
              </>
            ) : (
              <p className="text-xs text-subtle">
                Graph and hypotheses show the current stored diagnosis.
              </p>
            )}
          </aside>
        </div>
      )}
    </section>
  );
}
