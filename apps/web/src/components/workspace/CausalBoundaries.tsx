import type { DiagnosisView, FindingView } from "@/api/types";
export function CausalBoundaries({
  diagnosis,
  onSelect,
}: {
  diagnosis: DiagnosisView;
  onSelect: (finding: FindingView) => void;
}) {
  const references = (ids: string[]) =>
    ids.length
      ? ids.map((id) => {
          const finding = diagnosis.evidence.find((item) =>
            item.evidence_ids.includes(id),
          );
          return finding ? (
            <button
              key={id}
              onClick={() => onSelect(finding)}
              className="mr-2 break-anywhere text-accent hover:underline"
            >
              Inspect evidence {id}
            </button>
          ) : (
            <span key={id} className="mr-2 break-anywhere">
              {id} · finding not included
            </span>
          );
        })
      : "No references recorded";
  return (
    <div className="space-y-5">
      <p className="text-sm text-muted">
        Engine-recorded claim relationships and remaining questions.
      </p>
      {(diagnosis.causal_explanations ?? []).map((claim, index) => (
        <div key={index} className="rounded-md border border-border p-3">
          <p className="break-anywhere font-mono text-xs">
            {claim.explaining_claim} → {claim.explained_claim}
          </p>
          <p className="mt-2 text-xs text-muted">
            {claim.mechanism} · {claim.consequence}
          </p>
          <p className="mt-2 break-anywhere font-mono text-xs text-subtle">
            Evidence: {references(claim.evidence_ids)}
          </p>
        </div>
      ))}
      {(diagnosis.frontier_answers ?? []).map((answer, index) => (
        <div key={index} className="rounded-md border border-border p-3">
          <p className="break-anywhere text-sm">{answer.question}</p>
          <p className="mt-2 text-xs text-muted">
            {answer.state} · {answer.investigation_state}
          </p>
          <p className="mt-2 break-anywhere text-xs text-muted">
            Remaining uncertainty:{" "}
            {answer.remaining_uncertainty.join(" · ") || "None recorded"}
          </p>
          <p className="mt-2 break-anywhere font-mono text-xs text-subtle">
            Alternative {answer.alternative_id}
            <br />
            Evidence: {references(answer.evidence_ids)}
          </p>
        </div>
      ))}
      {!(
        diagnosis.causal_explanations?.length ||
        diagnosis.frontier_answers?.length
      ) && (
        <p className="text-sm text-muted">
          No claim relationships or frontier answers recorded for this
          diagnosis.
        </p>
      )}
      <details>
        <summary className="text-xs text-accent">Claim reference IDs</summary>
        <dl className="mt-3 space-y-3 break-anywhere font-mono text-xs text-muted">
          <div>
            <dt>Context hypotheses</dt>
            <dd>
              {diagnosis.context_hypothesis_ids?.join(", ") || "None recorded"}
            </dd>
          </div>
          <div>
            <dt>Material frontier</dt>
            <dd>
              {diagnosis.material_frontier_ids?.join(", ") || "None recorded"}
            </dd>
          </div>
          <div>
            <dt>Mechanism-verified hypotheses</dt>
            <dd>
              {diagnosis.mechanism_verified_hypothesis_ids?.join(", ") ||
                "None recorded"}
            </dd>
          </div>
        </dl>
      </details>
    </div>
  );
}
