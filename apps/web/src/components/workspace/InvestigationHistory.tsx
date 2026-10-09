import type { DiagnosisView, InvestigationActionAuditView } from "@/api/types";
import { Badge } from "@/components/ui/Badge";
import { useEffect, useRef } from "react";
import { useUrlFilters } from "@/lib/useUrlFilters";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { CopyButton } from "@/components/ui/CopyButton";
import { useInvestigationCanvas } from "@/components/investigation/context";
import { Button } from "@/components/ui/Button";

function Field({
  label,
  value,
}: {
  label: string;
  value: string | null | undefined;
}) {
  return (
    <div className="min-w-0">
      <dt className="mb-1 text-[11px] text-subtle">{label}</dt>
      <dd className="break-anywhere text-xs text-muted">
        {value || "Not recorded"}
      </dd>
    </div>
  );
}
export function AuditTurn({
  turn,
  selected,
  hypotheses,
  onOpenHypothesis,
  focusSelection = true,
}: {
  turn: InvestigationActionAuditView;
  selected: boolean;
  hypotheses: DiagnosisView["competing_hypotheses"];
  onOpenHypothesis: (id: string) => void;
  focusSelection?: boolean;
}) {
  const details = useRef<HTMLDetailsElement>(null);
  const canvas = useInvestigationCanvas();
  const summary = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!selected) return;
    if (details.current) details.current.open = true;
    if (focusSelection) {
      summary.current?.focus();
      details.current?.scrollIntoView({ block: "nearest" });
    }
  }, [selected, turn.turn_index, focusSelection]);
  return (
    <details
      ref={details}
      data-canvas-highlight={
        canvas?.focus.turnIndex === turn.turn_index || undefined
      }
      className={`group rounded-md border bg-surface ${selected ? "border-accent" : "border-border"}`}
    >
      <summary ref={summary} className="px-4 py-3 text-sm">
        <span className="ml-2 font-mono text-xs text-subtle">
          {String(turn.turn_index).padStart(2, "0")}
        </span>
        <span className="ml-3 font-medium">{turn.action}</span>
        <span className="ml-3 text-xs text-muted">
          {turn.backend_execution_status}
        </span>
        <div className="ml-6 mt-1 break-anywhere font-mono text-[11px] text-muted">
          {turn.target ?? "Target not recorded"}
        </div>
        <div className="ml-6 mt-2 flex flex-wrap gap-2 text-[11px] text-subtle">
          <span>
            {turn.new_evidence_refs.length} new /{" "}
            {turn.already_known_refs.length} known evidence
          </span>
          <span>
            · {turn.resolution_before} →{" "}
            {turn.resolution_after ?? "Not recorded"}
          </span>
          <span>
            ·{" "}
            {turn.decision_state_changed === null
              ? "Decision change not recorded"
              : turn.decision_state_changed
                ? "Decision changed"
                : "Decision unchanged"}
          </span>
        </div>
      </summary>
      <div className="space-y-4 border-t border-border px-4 py-4">
        {canvas && (
          <Button
            variant="secondary"
            aria-pressed={canvas.focus.turnIndex === turn.turn_index}
            onClick={() =>
              canvas.select({ kind: "turn", id: String(turn.turn_index) })
            }
          >
            Focus canvas on turn {turn.turn_index}
          </Button>
        )}
        <p className="break-anywhere text-sm">
          {turn.action_rationale || "Action rationale not recorded."}
        </p>
        <dl className="grid gap-4 sm:grid-cols-2">
          <Field label="Capability" value={turn.capability} />
          <Field label="Target" value={turn.target} />
          <Field
            label="Authorization decision"
            value={turn.authorization_result}
          />
          <Field
            label="Authorization reason"
            value={turn.authorization_reason}
          />
          <Field
            label="Execution result"
            value={turn.backend_execution_status}
          />
          <Field
            label="Progress classification"
            value={turn.progress_classification}
          />
          <Field label="Observation ID" value={turn.observation_id} />
          <Field label="Observation outcome" value={turn.observation_outcome} />
          <Field
            label="Returned evidence"
            value={turn.returned_evidence_refs.join(" · ")}
          />
          <Field
            label="New evidence"
            value={turn.new_evidence_refs.join(" · ")}
          />
          <Field
            label="Already-known evidence"
            value={turn.already_known_refs.join(" · ")}
          />
          <Field
            label="Normalized finding IDs"
            value={turn.normalized_finding_ids.join(" · ")}
          />
          <div>
            <dt className="mb-1 text-[11px] text-subtle">
              Affected hypotheses
            </dt>
            <dd className="space-y-2 break-anywhere text-xs text-muted">
              {turn.affected_hypothesis_ids.length
                ? turn.affected_hypothesis_ids.map((id) =>
                    hypotheses.some(
                      (hypothesis) => hypothesis.hypothesis_id === id,
                    ) ? (
                      <button
                        key={id}
                        type="button"
                        onClick={() => onOpenHypothesis(id)}
                        className="block rounded text-left font-mono text-accent hover:underline"
                      >
                        <span>{id}</span> <span aria-hidden>→</span>
                      </button>
                    ) : (
                      <p key={id}>
                        <span>{id}</span>
                        <span className="ml-2 text-subtle">
                          Explanation not included in this diagnosis
                        </span>
                      </p>
                    ),
                  )
                : "Not recorded"}
            </dd>
          </div>
          <Field
            label="Gap ID / dimension"
            value={[turn.gap_id, turn.gap_dimension]
              .filter(Boolean)
              .join(" · ")}
          />
          <Field label="Missing fact" value={turn.missing_fact} />
          <Field
            label="Intent ID / kind"
            value={[turn.intent_id, turn.intent_kind]
              .filter(Boolean)
              .join(" · ")}
          />
        </dl>
      </div>
    </details>
  );
}
export function InvestigationHistory({
  diagnosis,
  onOpenHypothesis,
}: {
  diagnosis: DiagnosisView;
  onOpenHypothesis: (id: string) => void;
}) {
  const audit = diagnosis.investigation_audit;
  const { params, update } = useUrlFilters();
  const requestedFilter = params.get("audit_filter") ?? "all";
  const filter = ["all", "changed", "new"].includes(requestedFilter)
    ? requestedFilter
    : "all";
  const actions = audit?.action_audits ?? [];
  const changed = actions.filter(
    (turn) => turn.decision_state_changed === true,
  );
  const newEvidence = actions.filter(
    (turn) => turn.new_evidence_refs.length > 0,
  );
  const visible =
    filter === "changed" ? changed : filter === "new" ? newEvidence : actions;
  const selectedTurn = params.get("turn");
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted">
        Recorded reads, their authorization, and their effect on the diagnosis.
      </p>
      {audit ? (
        <>
          <div className="rounded-md border border-border p-4">
            <div className="flex flex-wrap items-center gap-2">
              <Badge>{audit.initial_resolution}</Badge>
              <span className="text-muted">→</span>
              <Badge>{audit.final_resolution}</Badge>
            </div>
            <dl className="mt-3 grid gap-3 sm:grid-cols-2">
              <div>
                <Field label="Diagnosis run" value={audit.diagnosis_run_id} />
                <CopyButton
                  value={audit.diagnosis_run_id}
                  label="diagnosis run"
                />
              </div>
              <Field label="Stop reason" value={audit.stop_reason} />
              <Field
                label="Bounded activity"
                value={`${audit.turns} turns · ${audit.tool_calls} tool calls · ${audit.model_calls} model calls`}
              />
              <Field label="Artifact version" value={audit.artifact_version} />
            </dl>
          </div>
          <SegmentedControl
            label="Investigation filter"
            value={filter}
            onChange={(value) =>
              update({
                audit_filter: value === "all" ? null : value,
                turn: null,
              })
            }
            options={[
              { value: "all", label: "All turns", count: actions.length },
              {
                value: "changed",
                label: "Decision changed",
                count: changed.length,
              },
              {
                value: "new",
                label: "New evidence",
                count: newEvidence.length,
              },
            ]}
          />
          {selectedTurn !== null &&
            !actions.some(
              (turn) => String(turn.turn_index) === selectedTurn,
            ) && (
              <p className="text-sm text-muted">
                The requested investigation turn is not recorded in this run.
              </p>
            )}
          {visible.length ? (
            visible.map((turn, index) => (
              <AuditTurn
                key={`${turn.turn_index}-${index}`}
                turn={turn}
                selected={String(turn.turn_index) === selectedTurn}
                hypotheses={diagnosis.competing_hypotheses}
                onOpenHypothesis={onOpenHypothesis}
              />
            ))
          ) : (
            <p className="text-sm text-muted">
              {actions.length
                ? "No turns match this filter."
                : "No bounded actions recorded for this run."}
            </p>
          )}
        </>
      ) : (
        <p className="text-sm text-muted">
          No bounded investigation audit recorded.
        </p>
      )}
      {diagnosis.steps.length > 0 && (
        <details className="rounded-md border border-border p-4">
          <summary className="text-sm font-medium">
            Recorded engine steps ({diagnosis.steps.length})
          </summary>
          <ol className="mt-3 divide-y divide-border">
            {diagnosis.steps.map((step, index) => (
              <li key={index} className="py-3">
                <p className="break-anywhere font-mono text-xs">
                  {index + 1}. {step.actor} · {step.action}
                </p>
                <p className="mt-1 break-anywhere text-sm text-muted">
                  {step.detail}
                </p>
              </li>
            ))}
          </ol>
        </details>
      )}
    </div>
  );
}
