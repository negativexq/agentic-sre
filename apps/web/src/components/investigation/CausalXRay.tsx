import { useId, useMemo, useState } from "react";
import type { CausalHopView } from "@/api/types";
import { Entity } from "@/components/Entity";
import { useInvestigationCanvas } from "./context";
import { layoutRecordedGraph } from "./graph";

export function CausalXRay({
  hops,
  onSelectResource,
}: {
  hops: CausalHopView[];
  onSelectResource: (resource: string) => void;
}) {
  const groups = useMemo(() => layoutRecordedGraph(hops), [hops]);
  const canvas = useInvestigationCanvas();
  const marker = useId().replace(/:/g, "");
  const [edge, setEdge] = useState<number | null>(null);
  const selectedEdge = edge === null ? null : hops[edge];
  if (!hops.length)
    return (
      <p className="text-sm text-muted">
        No causal path recorded by the engine.
      </p>
    );
  return (
    <div className="min-w-0 space-y-3">
      <p className="text-[11px] text-subtle">
        Current stored diagnosis · recorded source → target relationships ·{" "}
        {hops.length} edges · {groups.length} separate component
        {groups.length === 1 ? "" : "s"}. Highlight indicates selection, not
        verification. Scroll the graph to inspect long paths.
      </p>
      <div
        role="region"
        aria-label="Recorded causal graph"
        tabIndex={0}
        className="max-h-[480px] min-w-0 space-y-3 overflow-auto rounded-md border border-border bg-surface p-2"
      >
        {groups.map((group, groupIndex) => (
          <div
            key={group.nodes[0].id}
            aria-label={`Recorded component ${groupIndex + 1}`}
            className="relative rounded border border-border/60"
            style={{ width: group.width, height: group.height }}
          >
            <svg
              width={group.width}
              height={group.height}
              className="absolute inset-0 text-subtle"
              aria-hidden="true"
            >
              <defs>
                <marker
                  id={`${marker}-${groupIndex}`}
                  markerWidth="8"
                  markerHeight="8"
                  refX="7"
                  refY="4"
                  orient="auto"
                >
                  <path d="M0,0 L8,4 L0,8" fill="currentColor" />
                </marker>
              </defs>
              {group.edges.map(({ hop, index }) => {
                const source = group.nodes.find(
                  (node) => node.id === hop.source,
                )!;
                const target = group.nodes.find(
                  (node) => node.id === hop.target,
                )!;
                const forward = target.x > source.x;
                const sx = source.x + (forward ? 240 : 0),
                  sy = source.y + 48;
                const tx = target.x + (forward ? 0 : 240),
                  ty = target.y + 48;
                const parallel = group.edges.filter(
                  (edge) =>
                    edge.hop.source === hop.source &&
                    edge.hop.target === hop.target,
                );
                const offset =
                  (parallel.findIndex((edge) => edge.index === index) -
                    (parallel.length - 1) / 2) *
                  24;
                const d =
                  source.id === target.id
                    ? `M${source.x + 120},${source.y} C${source.x + 60},${source.y - 36} ${source.x + 180},${source.y - 36} ${source.x + 150},${source.y}`
                    : `M${sx},${sy} C${sx + (forward ? 40 : -50)},${sy + offset} ${tx + (forward ? -40 : 50)},${ty + offset} ${tx},${ty}`;
                const highlighted =
                  edge === index ||
                  (canvas?.focus.resources.has(hop.source) &&
                    canvas?.focus.resources.has(hop.target));
                return (
                  <g key={index}>
                    <path
                      d={d}
                      fill="none"
                      stroke="currentColor"
                      strokeWidth={highlighted ? 2.5 : 1.5}
                      className={highlighted ? "text-accent" : ""}
                      markerEnd={`url(#${marker}-${groupIndex})`}
                    />
                    <circle
                      cx={
                        source.id === target.id ? source.x + 132 : (sx + tx) / 2
                      }
                      cy={
                        source.id === target.id
                          ? source.y - 20
                          : (sy + ty) / 2 + offset
                      }
                      r="10"
                      className="fill-surface-raised stroke-border"
                    />
                    <text
                      x={
                        source.id === target.id ? source.x + 132 : (sx + tx) / 2
                      }
                      y={
                        source.id === target.id
                          ? source.y - 16
                          : (sy + ty) / 2 + offset + 4
                      }
                      textAnchor="middle"
                      fontSize="10"
                      fill="currentColor"
                    >
                      {index + 1}
                    </text>
                  </g>
                );
              })}
            </svg>
            {group.nodes.map((node) => (
              <button
                key={node.id}
                type="button"
                aria-label={`Select graph resource ${node.id}`}
                aria-pressed={canvas?.focus.resources.has(node.id) ?? false}
                onClick={() => onSelectResource(node.id)}
                className={`absolute w-[240px] rounded-md border p-3 text-left ${canvas?.focus.resources.has(node.id) ? "border-accent bg-accent-soft ring-1 ring-accent/30" : "border-border bg-surface-raised hover:border-accent/60"}`}
                style={{
                  left: node.x,
                  top: node.y,
                  minHeight: 96,
                  maxHeight: 128,
                  overflow: "auto",
                }}
              >
                <Entity value={node.id} />
                <span className="mt-2 block text-[10px] text-subtle">
                  Select resource & evidence
                </span>
              </button>
            ))}
          </div>
        ))}
      </div>
      <div
        className="flex flex-wrap gap-2"
        role="group"
        aria-label="Recorded graph relationships"
      >
        {hops.map((hop, index) => (
          <button
            key={index}
            aria-pressed={edge === index}
            onClick={() => setEdge(edge === index ? null : index)}
            className={`rounded border px-2 py-1 text-left text-[11px] ${edge === index ? "border-accent bg-accent-soft" : "border-border text-muted"}`}
          >
            {index + 1} · {hop.relation} · {hop.direction}
          </button>
        ))}
      </div>
      {selectedEdge && (
        <dl className="space-y-2 rounded-md border border-border p-3 text-xs break-anywhere">
          <div>
            <dt className="text-subtle">Recorded source</dt>
            <dd className="font-mono">{selectedEdge.source}</dd>
          </div>
          <div>
            <dt className="text-subtle">Relationship / recorded direction</dt>
            <dd>
              {selectedEdge.relation} / {selectedEdge.direction}
            </dd>
          </div>
          <div>
            <dt className="text-subtle">Recorded target</dt>
            <dd className="font-mono">{selectedEdge.target}</dd>
          </div>
          <p className="text-subtle">
            No edge-level evidence references are included in this path DTO.
            Resource findings are shown separately; they are not asserted to
            prove this edge.
          </p>
        </dl>
      )}
    </div>
  );
}
