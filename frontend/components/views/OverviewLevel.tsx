"use client";

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { Graph } from "@/components/graph/Graph";
import { flowEdges, place } from "@/components/graph/flow";
import type { Actions, GraphNode } from "@/components/graph/nodes";
import { useTopologyOverview } from "@/lib/api/topology";
import { layered } from "@/lib/graph/elk";
import { belowAll, rowsOf, SIZE, type Layout } from "@/lib/graph/layout";
import { plural } from "@/lib/model/format";
import type { RepositoryLink } from "@/lib/api/generated";
import { buildOverview, repositoryId } from "@/lib/model/graph";
import type { RepositoryModel } from "@/lib/model/repositories";

import { Metrics } from "./Metrics";
import classes from "./Views.module.css";

const NO_LINKS: readonly RepositoryLink[] = [];

/**
 * Level one: every indexed repository, and the links the index records between
 * them, one line per kind. Unlinked repositories get their own row.
 */
export function OverviewLevel({
  repositories,
  onOpen,
}: {
  repositories: RepositoryModel[];
  onOpen: (slug: string) => void;
}) {
  const overview = useTopologyOverview();
  const links = overview.data?.links ?? NO_LINKS;
  const graph = useMemo(() => buildOverview(repositories, links), [repositories, links]);

  const signature = `${graph.linked.map((r) => r.slug).join(",")}|${graph.edges.map((e) => e.id).join(",")}`;
  const engine = useQuery({
    queryKey: ["layout", "overview", signature],
    queryFn: () =>
      layered(
        graph.linked.map((r) => ({ id: repositoryId(r.slug), ...SIZE.repository })),
        graph.edges,
      ),
    enabled: graph.linked.length > 0,
    staleTime: Infinity,
  });

  const actions = useMemo<Actions>(() => ({ openRepository: onOpen, openPath: () => {} }), [onOpen]);

  const { nodes, edges } = useMemo(() => {
    const ready = overview.isSuccess || overview.isError;
    if (!ready || (graph.linked.length > 0 && !engine.data)) return { nodes: [], edges: [] };
    const layout: Layout = new Map(engine.data ?? []);
    for (const [id, box] of rowsOf(graph.independent, belowAll(layout))) layout.set(id, box);
    const placed: GraphNode[] = [];
    for (const repository of [...graph.linked, ...graph.independent]) {
      const id = repositoryId(repository.slug);
      const box = layout.get(id);
      if (box) placed.push(place({ id, type: "repository", data: { repository, current: false, actions } }, box));
    }
    return { nodes: placed, edges: flowEdges(graph.edges, layout, true) };
  }, [graph, engine.data, actions, overview.isSuccess, overview.isError]);

  const crossing = (overview.data?.links ?? []).reduce((sum, link) => sum + link.count, 0);
  const note = overview.isError
    ? "The links between repositories could not be read. Repositories are shown without them."
    : overview.isSuccess && graph.edges.length === 0 && repositories.length > 1
      ? "No links between these repositories are recorded in the index. Open one to see how its parts connect."
      : overview.data?.ambiguous_total
        ? `${plural(overview.data.ambiguous_total, "package")} with more than one possible publisher ${overview.data.ambiguous_total === 1 ? "is" : "are"} not drawn.`
        : null;

  return (
    <div className={classes.level}>
      <Metrics repositories={repositories} crossing={overview.isSuccess ? crossing : null} />
      <Graph
        nodes={nodes}
        edges={edges}
        level="overview"
        label="Repositories and the links between them"
        containment={false}
        topCenter={note && <p className={classes.note}>{note}</p>}
      />
    </div>
  );
}
