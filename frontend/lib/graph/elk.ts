import type { ELK, ElkNode } from "elkjs/lib/elk-api";

import type { Box, ViewEdge } from "@/lib/model/graph";

/**
 * The layered engine, run in a Web Worker so a large graph never blocks input.
 * Loaded on first use: the overview only needs it once repositories are linked.
 */
let engine: Promise<ELK> | null = null;

function load(): Promise<ELK> {
  engine ??= import("elkjs/lib/elk-api").then(
    ({ default: Elk }) =>
      new Elk({
        workerFactory: () => new Worker(new URL("./elk.worker.ts", import.meta.url), { type: "module" }),
      }),
  );
  return engine;
}

export async function layered(
  nodes: readonly { id: string; width: number; height: number }[],
  edges: readonly ViewEdge[],
): Promise<Map<string, Box>> {
  const elk = await load();
  const graph: ElkNode = {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.edgeRouting": "ORTHOGONAL",
      "elk.layered.spacing.nodeNodeBetweenLayers": "120",
      "elk.spacing.nodeNode": "48",
      "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
      "elk.separateConnectedComponents": "true",
    },
    children: nodes.map((n) => ({ id: n.id, width: n.width, height: n.height })),
    edges: edges.map((e) => ({ id: e.id, sources: [e.source], targets: [e.target] })),
  };
  const result = await elk.layout(graph);
  const layout = new Map<string, Box>();
  for (const child of result.children ?? []) {
    layout.set(child.id, { x: child.x ?? 0, y: child.y ?? 0, width: child.width ?? 0, height: child.height ?? 0 });
  }
  // centre on the origin, as the other layouts are
  let minX = Infinity;
  let maxX = -Infinity;
  for (const box of layout.values()) {
    minX = Math.min(minX, box.x);
    maxX = Math.max(maxX, box.x + box.width);
  }
  const shift = Number.isFinite(minX) ? -(minX + maxX) / 2 : 0;
  for (const box of layout.values()) box.x += shift;
  return layout;
}
