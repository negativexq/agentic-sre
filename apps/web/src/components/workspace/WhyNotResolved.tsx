import type { DiagnosisView, FindingView } from "@/api/types";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";

export function WhyNotResolved({
  diagnosis,
  onSelect,
  onOpenHypothesis,
}: {
  diagnosis: DiagnosisView;
  onSelect: (finding: FindingView) => void;
  onOpenHypothesis: (id: string) => void;
}) {
  if (diagnosis.resolution === "RESOLVED") return null;
  return (
    <Card className="border-accent/30">
      <CardHeader
        title="Why not resolved?"
        action={<Badge tone="accent">{diagnosis.resolution}</Badge>}
      />
      <CardBody className="space-y-4">
        <p className="break-anywhere text-sm text-muted">
          {diagnosis.resolution_rationale ||
            "No resolution rationale recorded by the engine."}
        </p>
        <section>
          <h3 className="mb-2 text-xs font-medium">Unresolved dimensions</h3>
          {diagnosis.unresolved_dimensions.length ? (
            <ul className="flex flex-wrap gap-2">
              {diagnosis.unresolved_dimensions.map((dimension, index) => (
                <li key={index}>
                  <Badge>{dimension}</Badge>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-subtle">
              No unresolved dimensions recorded.
            </p>
          )}
        </section>
        <dl className="grid gap-3 border-t border-border pt-3 sm:grid-cols-2">
          <div>
            <dt className="text-xs text-subtle">Actor display</dt>
            <dd className="mt-1 text-xs">
              {diagnosis.leading_actor_display ?? "Not recorded"}
              {diagnosis.leading_actor_withheld_reason &&
                ` · ${diagnosis.leading_actor_withheld_reason}`}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-subtle">Timing assessment</dt>
            <dd className="mt-1 text-xs">
              {diagnosis.timing?.status ?? "No timing assessment recorded"}
            </dd>
          </div>
        </dl>
        {(diagnosis.frontier_answers ?? []).length > 0 ? (
          <section className="space-y-3">
            <h3 className="text-xs font-medium">Engine frontier questions</h3>
            {diagnosis.frontier_answers!.map((answer, index) => (
              <article key={index} className="border-t border-border pt-3">
                <p className="break-anywhere text-sm font-medium">
                  {answer.question}
                </p>
                <p className="mt-1 text-xs text-subtle">
                  {answer.state} · {answer.investigation_state}
                </p>
                <ul className="mt-2 space-y-1 text-xs text-muted">
                  {answer.remaining_uncertainty.map((uncertainty, i) => (
                    <li key={i} className="break-anywhere">
                      {uncertainty}
                    </li>
                  ))}
                </ul>
                {!answer.remaining_uncertainty.length && (
                  <p className="mt-2 text-xs text-subtle">
                    No remaining uncertainty recorded for this answer.
                  </p>
                )}
                {answer.alternative_id && (
                  <p className="mt-2 break-anywhere font-mono text-xs">
                    {diagnosis.competing_hypotheses.some(
                      (hypothesis) =>
                        hypothesis.hypothesis_id === answer.alternative_id,
                    ) ? (
                      <button
                        className="text-accent hover:underline"
                        onClick={() => onOpenHypothesis(answer.alternative_id)}
                      >
                        Open hypothesis {answer.alternative_id}
                      </button>
                    ) : (
                      `Alternative reference: ${answer.alternative_id}`
                    )}
                  </p>
                )}
                <div className="mt-2 flex flex-wrap gap-2">
                  {answer.evidence_ids.map((id) => {
                    const finding = diagnosis.evidence.find((item) =>
                      item.evidence_ids.includes(id),
                    );
                    return finding ? (
                      <button
                        key={id}
                        onClick={() => onSelect(finding)}
                        className="break-anywhere rounded border border-border px-2 py-1 font-mono text-xs text-accent"
                      >
                        Inspect evidence {id}
                      </button>
                    ) : (
                      <span
                        key={id}
                        className="break-anywhere font-mono text-xs text-subtle"
                      >
                        {id} · finding not included
                      </span>
                    );
                  })}
                </div>
              </article>
            ))}
          </section>
        ) : (
          <p className="text-xs text-subtle">
            No frontier answers recorded. Missing assessments are not evidence
            against a candidate.
          </p>
        )}
      </CardBody>
    </Card>
  );
}
