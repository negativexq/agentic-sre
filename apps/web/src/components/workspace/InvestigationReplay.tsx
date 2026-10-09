import type { DiagnosisView } from "@/api/types";
import { useUrlFilters } from "@/lib/useUrlFilters";
import { Button } from "@/components/ui/Button";
import { AuditTurn } from "./InvestigationHistory";

export function InvestigationReplay({
  diagnosis,
  onOpenHypothesis,
}: {
  diagnosis: DiagnosisView;
  onOpenHypothesis: (id: string) => void;
}) {
  const { params, update } = useUrlFilters();
  const actions = diagnosis.investigation_audit?.action_audits ?? [];
  const requested = params.get("replay_turn");
  const index =
    requested === null
      ? 0
      : actions.findIndex((turn) => String(turn.turn_index) === requested);
  const turn = actions[index];
  if (!actions.length)
    return (
      <p className="text-sm text-muted">
        No recorded investigation actions are available for this walkthrough.
        Engine steps remain accessible in the History view.
      </p>
    );
  if (!turn)
    return (
      <div className="space-y-3">
        <p className="text-sm text-muted">
          The requested investigation turn is not recorded.
        </p>
        <Button
          variant="secondary"
          onClick={() =>
            update({
              replay_turn: null,
              canvas_evidence: null,
              canvas_kind: "turn",
              canvas_id: String(actions[0].turn_index),
            })
          }
        >
          Start with first recorded turn
        </Button>
      </div>
    );
  return (
    <div className="space-y-4">
      <p className="text-xs text-muted">
        Read-only walkthrough of the persisted audit, in its recorded order. No
        tools are executed and no historical diagnosis snapshot is
        reconstructed. Hypothesis links open explanations included in the
        current diagnosis.
      </p>
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border p-3">
        <p aria-live="polite" className="text-sm">
          Recorded action {index + 1} of {actions.length} · Turn{" "}
          {turn.turn_index}
        </p>
        <div className="flex gap-2">
          <Button
            variant="secondary"
            disabled={index === 0}
            onClick={() =>
              update({
                replay_turn: String(actions[index - 1].turn_index),
                canvas_evidence: null,
                canvas_kind: "turn",
                canvas_id: String(actions[index - 1].turn_index),
              })
            }
          >
            Previous action
          </Button>
          <Button
            variant="secondary"
            disabled={index === actions.length - 1}
            onClick={() =>
              update({
                replay_turn: String(actions[index + 1].turn_index),
                canvas_evidence: null,
                canvas_kind: "turn",
                canvas_id: String(actions[index + 1].turn_index),
              })
            }
          >
            Next action
          </Button>
        </div>
      </div>
      <AuditTurn
        turn={turn}
        selected
        focusSelection={false}
        hypotheses={diagnosis.competing_hypotheses}
        onOpenHypothesis={onOpenHypothesis}
      />
      {index === actions.length - 1 && (
        <p className="text-xs text-muted">
          Recorded run stop reason:{" "}
          {diagnosis.investigation_audit?.stop_reason ?? "Not recorded"}
        </p>
      )}
    </div>
  );
}
