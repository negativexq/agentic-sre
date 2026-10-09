import type { CausalHopView } from "@/api/types";
export type GraphGroup = {
  nodes: { id: string; x: number; y: number }[];
  edges: { hop: CausalHopView; index: number }[];
  width: number;
  height: number;
};
/** Deterministic layout only: identities deduplicate exactly; every edge is an input hop. */
export function layoutRecordedGraph(hops: CausalHopView[]): GraphGroup[] {
  const identities = [
    ...new Set(hops.flatMap((hop) => [hop.source, hop.target])),
  ];
  const remaining = new Set(identities);
  const groups: GraphGroup[] = [];
  const adjacency = new Map(identities.map((id) => [id, new Set<string>()]));
  hops.forEach((hop) => {
    adjacency.get(hop.source)!.add(hop.target);
    adjacency.get(hop.target)!.add(hop.source);
  });
  for (const root of identities) {
    if (!remaining.has(root)) continue;
    const members = new Set<string>();
    const queue = [root];
    remaining.delete(root);
    for (let i = 0; i < queue.length; i++) {
      const id = queue[i];
      members.add(id);
      for (const next of adjacency.get(id)!)
        if (remaining.delete(next)) queue.push(next);
    }
    const edges = hops
      .map((hop, index) => ({ hop, index }))
      .filter((edge) => members.has(edge.hop.source));
    const ids = identities.filter((id) => members.has(id));
    const indegree = new Map(ids.map((id) => [id, 0]));
    const outgoing = new Map(ids.map((id) => [id, [] as string[]]));
    edges.forEach(({ hop }) => {
      indegree.set(hop.target, indegree.get(hop.target)! + 1);
      outgoing.get(hop.source)!.push(hop.target);
    });
    const layers = new Map(ids.map((id) => [id, 0]));
    const ready = ids.filter((id) => indegree.get(id) === 0);
    for (let i = 0; i < ready.length; i++) {
      const id = ready[i];
      for (const target of outgoing.get(id)!) {
        layers.set(target, Math.max(layers.get(target)!, layers.get(id)! + 1));
        indegree.set(target, indegree.get(target)! - 1);
        if (indegree.get(target) === 0) ready.push(target);
      }
    }
    // Cycles remain recorded cycles. Place them in separate lanes without creating a DAG claim.
    let cycleLayer = Math.max(...layers.values());
    ids
      .filter((id) => indegree.get(id)! > 0)
      .forEach((id) => layers.set(id, cycleLayer++));
    const rows = new Map<number, number>();
    const nodes = ids.map((id) => {
      const layer = layers.get(id)!;
      const row = rows.get(layer) ?? 0;
      rows.set(layer, row + 1);
      return { id, x: 24 + layer * 300, y: 40 + row * 160 };
    });
    groups.push({
      nodes,
      edges,
      width: Math.max(...nodes.map((node) => node.x)) + 264,
      height: Math.max(...nodes.map((node) => node.y)) + 150,
    });
  }
  return groups;
}
