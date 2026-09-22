import { Badge, type BadgeTone } from "@/components/ui/Badge";
import type { CandidateView, HypothesisView } from "@/api/types";
import { shortEntity } from "@/lib/format";

function stateTone(state: string): BadgeTone {
  switch (state) {
    case "SUPPORTED":
      return "healthy";
    case "CONTRADICTED":
      return "critical";
    default:
      return "neutral"; // UNRESOLVED — plausible, not excluded
  }
}

export function CompetingHypotheses({
  hypotheses,
  candidates,
}: {
  hypotheses: HypothesisView[];
  candidates: CandidateView[];
}) {
  if (hypotheses.length === 0 && candidates.length === 0) {
    return (
      <p className="text-sm text-muted">
        No competing hypotheses survived — the leading actor stands alone.
      </p>
    );
  }

  return (
    <div className="space-y-2">
      {hypotheses.map((hypothesis) => (
        <div
          key={hypothesis.hypothesis_id}
          className="rounded-lg border border-border bg-surface px-3 py-2"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <code className="text-sm text-text break-anywhere">{shortEntity(hypothesis.actor)}</code>
            <Badge tone={stateTone(hypothesis.epistemic_state)}>
              {hypothesis.epistemic_state}
            </Badge>
          </div>
          {hypothesis.reasons.length > 0 && (
            <p className="mt-1 text-sm text-muted break-anywhere">{hypothesis.reasons[0]}</p>
          )}
        </div>
      ))}

      {hypotheses.length === 0 &&
        candidates.map((candidate) => (
          <div
            key={candidate.entity}
            className="rounded-lg border border-border bg-surface px-3 py-2"
          >
            <div className="flex items-center justify-between gap-2">
              <code className="text-sm text-text break-anywhere">
                {shortEntity(candidate.entity)}
              </code>
              <span className="font-mono text-xs text-subtle">{candidate.score.toFixed(1)}</span>
            </div>
            <p className="mt-1 text-sm text-muted break-anywhere">{candidate.strongest_signal}</p>
          </div>
        ))}
    </div>
  );
}
