import type { RemediationView } from "@/api/types";
import { CodeBlock } from "@/components/ui/CodeBlock";
import { CopyButton } from "@/components/ui/CopyButton";

export function RemediationProposal({
  proposals,
}: {
  proposals: RemediationView[];
}) {
  if (!proposals.length) return null;
  return (
    <details className="rounded-lg border border-border p-4">
      <summary className="text-sm font-medium">
        Operator action proposals ({proposals.length})
      </summary>
      <p className="mt-3 text-xs text-muted">
        Recorded proposals only. Copying a command does not approve or execute
        it; this console has no remediation execution workflow.
      </p>
      <div className="mt-4 space-y-4">
        {proposals.map((proposal, index) => (
          <section
            key={index}
            className="space-y-2 border-t border-border pt-3"
          >
            <h3 className="break-anywhere text-sm font-medium">
              {proposal.action}
            </h3>
            <p className="text-xs text-muted">
              Risk: {proposal.risk || "Not recorded"}
            </p>
            <p className="text-xs text-muted">
              Approval requirement:{" "}
              {proposal.requires_approval
                ? "Required by proposal"
                : "Not required by proposal"}
            </p>
            <CodeBlock value={proposal.command} />
            {proposal.command && (
              <CopyButton value={proposal.command} label="proposed command" />
            )}
          </section>
        ))}
      </div>
    </details>
  );
}
