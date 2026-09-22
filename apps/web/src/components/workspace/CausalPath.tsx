import type { CausalHopView } from "@/api/types";
import { shortEntity } from "@/lib/format";

/**
 * The engine's causal path, rendered hop by hop. The UI draws exactly the hops
 * the diagnosis produced; it never infers a relation of its own.
 */
export function CausalPath({ hops }: { hops: CausalHopView[] }) {
  if (hops.length === 0) {
    return (
      <p className="text-sm text-muted">
        The strongest evidence attaches directly to the symptom; no multi-hop path.
      </p>
    );
  }

  const nodes: { entity: string; relation?: string }[] = [{ entity: hops[0].source }];
  for (const hop of hops) {
    nodes.push({ entity: hop.target, relation: hop.relation });
  }

  return (
    <ol className="space-y-0">
      {nodes.map((node, index) => (
        <li key={index}>
          {node.relation && (
            <div className="ml-3 flex items-center gap-2 py-1 text-xs text-subtle">
              <span className="h-4 w-px bg-border-strong" />
              <span className="font-mono">↓ {node.relation}</span>
            </div>
          )}
          <div className="rounded-lg border border-border bg-surface px-3 py-2">
            <code className="text-sm text-text break-anywhere">{shortEntity(node.entity)}</code>
          </div>
        </li>
      ))}
    </ol>
  );
}
