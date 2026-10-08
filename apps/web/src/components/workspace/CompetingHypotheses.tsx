import { Badge } from "@/components/ui/Badge";
import { Entity } from "@/components/Entity";
import type { CandidateView, HypothesisView } from "@/api/types";

export function CompetingHypotheses({
  hypotheses,
  candidates,
}: {
  hypotheses: HypothesisView[];
  candidates: CandidateView[];
}) {
  if (!hypotheses.length && !candidates.length)
    return (
      <p className="text-sm text-muted">No competing hypotheses recorded.</p>
    );
  return (
    <div className="divide-y divide-border">
      <p className="pb-3 text-xs text-muted">
        Supported hypotheses remain candidates; support alone does not establish
        a root cause.
      </p>
      {hypotheses.map((hypothesis) => (
        <article key={hypothesis.hypothesis_id} className="py-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <Entity value={hypothesis.actor} />
            </div>
            <Badge
              tone={
                hypothesis.epistemic_state === "SUPPORTED"
                  ? "accent"
                  : "neutral"
              }
            >
              {hypothesis.epistemic_state}
            </Badge>
          </div>
          <p className="mt-3 break-anywhere text-sm text-muted">
            {hypothesis.causal_explanation || "No causal explanation recorded."}
          </p>
          <details className="mt-3">
            <summary className="text-xs text-accent">
              Engine reasons & identity
            </summary>
            <ul className="mt-2 list-inside list-disc space-y-2 text-xs text-muted">
              {hypothesis.reasons.map((reason, index) => (
                <li key={index} className="break-anywhere">
                  {reason}
                </li>
              ))}
            </ul>
            <p className="mt-3 break-anywhere font-mono text-[11px] text-subtle">
              {hypothesis.hypothesis_id} · Engine score {hypothesis.score}
            </p>
          </details>
        </article>
      ))}
      {!hypotheses.length &&
        candidates.map((candidate) => (
          <article key={candidate.entity} className="py-4">
            <Entity value={candidate.entity} />
            <p className="mt-2 break-anywhere text-sm text-muted">
              {candidate.strongest_signal}
            </p>
            <p className="mt-2 text-[11px] text-subtle">
              Engine score {candidate.score} · Epistemic state not provided
            </p>
          </article>
        ))}
    </div>
  );
}
