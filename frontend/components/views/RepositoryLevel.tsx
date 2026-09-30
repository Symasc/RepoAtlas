"use client";

import { Button, Loader, UnstyledButton } from "@mantine/core";
import { ArrowLeftIcon } from "@phosphor-icons/react";
import { useMemo } from "react";

import { Graph } from "@/components/graph/Graph";
import { flowEdges, place } from "@/components/graph/flow";
import type { Actions, GraphNode } from "@/components/graph/nodes";
import { ApiError } from "@/lib/api/client";
import type { TopologyChildrenResponse } from "@/lib/api/generated";
import { useChildren, usePreviews } from "@/lib/api/topology";
import { layoutOpened } from "@/lib/graph/layout";
import { formatCount, plural } from "@/lib/model/format";
import { buildOpened, FILES_ID, PREVIEW_ROWS } from "@/lib/model/graph";
import type { RepositoryModel } from "@/lib/model/repositories";

import classes from "./Views.module.css";

/** At most this many cards list their contents; the rest still show their counts. */
const PREVIEWED = 15;

/**
 * A repository opened at a path: where you are (the chain from the repository
 * down), what is here (a card per directory, one for the files), and what it
 * links to (other entries here, things shared across the index, and other
 * repositories, dimmed at the side).
 */
export function RepositoryLevel({
  repository,
  path,
  repositories,
  onNavigate,
}: {
  repository: RepositoryModel;
  path: string;
  repositories: RepositoryModel[];
  onNavigate: (slug: string | null, path?: string) => void;
}) {
  const children = useChildren(repository.slug, path);
  const directoryPaths = useMemo(
    () =>
      (children.data?.entries ?? [])
        .filter((e) => e.kind === "directory")
        .slice(0, PREVIEWED)
        .map((e) => e.path),
    [children.data],
  );
  const previewResults = usePreviews(repository.slug, directoryPaths, PREVIEW_ROWS);
  const previews = useMemo(() => {
    const map = new Map<string, TopologyChildrenResponse>();
    directoryPaths.forEach((p, i) => {
      const preview = previewResults[i];
      if (preview) map.set(p, preview);
    });
    return map;
  }, [directoryPaths, previewResults]);

  const actions = useMemo<Actions>(
    () => ({
      openRepository: (slug) => onNavigate(slug, ""),
      openPath: (next) => onNavigate(repository.slug, next),
    }),
    [onNavigate, repository.slug],
  );
  const hues = useMemo(() => new Map(repositories.map((r) => [r.slug, r.hue])), [repositories]);

  const view = useMemo(() => {
    if (!children.data) return null;
    const graph = buildOpened({ repository, path, children: children.data, previews });
    const layout = layoutOpened(graph);
    const entries = children.data.entries ?? [];
    const here =
      entries.reduce((sum, e) => sum + (e.kind === "directory" ? (e.files ?? 0) : 0), 0) +
      entries.filter((e) => e.kind === "file").length;
    const nodes: GraphNode[] = [];
    graph.chain.forEach((step, i) => {
      const box = layout.get(step.id)!;
      const current = i === graph.chain.length - 1;
      nodes.push(
        i === 0
          ? place({ id: step.id, type: "repository", data: { repository, current, actions } }, box)
          : place(
              {
                id: step.id,
                type: "path",
                data: { name: step.name, path: step.path, files: current ? here : null, current, actions },
              },
              box,
            ),
      );
    });
    for (const card of graph.directories) {
      nodes.push(place({ id: card.id, type: "directory", data: { card, actions } }, layout.get(card.id)!));
    }
    if (graph.files) nodes.push(place({ id: FILES_ID, type: "files", data: { card: graph.files } }, layout.get(FILES_ID)!));
    for (const node of graph.shared) {
      nodes.push(place({ id: node.id, type: "shared", data: { node } }, layout.get(node.id)!));
    }
    for (const node of graph.peers) {
      nodes.push(
        place({ id: node.id, type: "peer", data: { node, hue: hues.get(node.repository) ?? null, actions } }, layout.get(node.id)!),
      );
    }
    for (const node of graph.away) {
      nodes.push(
        place({ id: node.id, type: "away", data: { node, repository: repository.name, actions } }, layout.get(node.id)!),
      );
    }
    return { nodes, edges: flowEdges(graph.edges, layout, false), graph };
  }, [children.data, previews, repository, path, actions, hues]);

  const depth = path ? path.split("/").length + 1 : 1;
  const data = children.data;
  const error = children.error instanceof ApiError ? children.error : null;

  return (
    <div className={classes.level}>
      <Graph
        nodes={view?.nodes ?? []}
        edges={view?.edges ?? []}
        level={`${repository.slug}:${path}`}
        label={`${repository.name}${path ? `, ${path}` : ""}: its contents and links`}
        containment
        topLeft={<Trail repository={repository} path={path} onNavigate={onNavigate} />}
        topRight={
          data && (
            <div className={classes.levelInfo}>
              <span className={classes.depth}>Depth {depth}</span>
              <span>{plural(data.entries_total ?? 0, data.target === "file" ? "declaration" : "entry", data.target === "file" ? "declarations" : "entries")}</span>
              {(data.links_total ?? 0) > 0 && <span>{plural(data.links_total ?? 0, "link group")}</span>}
            </div>
          )
        }
        topCenter={
          error ? (
            <div className={classes.problem} role="alert">
              <p>{error.message}</p>
              <Button size="xs" variant="default" leftSection={<ArrowLeftIcon size={13} />} onClick={() => onNavigate(repository.slug, "")}>
                Back to {repository.name}
              </Button>
            </div>
          ) : children.isPending ? (
            <p className={classes.note}>
              <Loader size={12} type="dots" /> Reading {path || repository.name}
            </p>
          ) : (
            view && <Caveats graph={view.graph} data={data!} />
          )
        }
      />
    </div>
  );
}

/** What is on screen but incomplete, said plainly rather than left out. */
function Caveats({ graph, data }: { graph: ReturnType<typeof buildOpened>; data: TopologyChildrenResponse }) {
  const notes: string[] = [];
  if (data.has_more) notes.push(`Showing the first ${formatCount(data.entries?.length ?? 0)} of ${formatCount(data.entries_total ?? 0)} entries.`);
  if (data.truncated) notes.push(`Showing the strongest ${formatCount(data.links?.length ?? 0)} of ${formatCount(data.links_total ?? 0)} link groups.`);
  if (data.complete === false) notes.push("Some links were not read: this level has more than can be counted at once.");
  if (graph.undrawn > 0) notes.push(`${plural(graph.undrawn, "link group")} reach${graph.undrawn === 1 ? "es" : ""} somewhere not on screen.`);
  if (notes.length === 0) return null;
  return <p className={classes.note}>{notes.join(" ")}</p>;
}

function Trail({
  repository,
  path,
  onNavigate,
}: {
  repository: RepositoryModel;
  path: string;
  onNavigate: (slug: string | null, path?: string) => void;
}) {
  const segments = path ? path.split("/") : [];
  return (
    <nav className={classes.trail} aria-label="Location">
      <UnstyledButton className={classes.crumb} onClick={() => onNavigate(null)}>
        All repositories
      </UnstyledButton>
      <span className={classes.separator} aria-hidden>
        /
      </span>
      {segments.length === 0 ? (
        <span className={classes.crumb} data-current aria-current="page">
          {repository.name}
        </span>
      ) : (
        <UnstyledButton className={classes.crumb} onClick={() => onNavigate(repository.slug, "")}>
          {repository.name}
        </UnstyledButton>
      )}
      {segments.map((segment, i) => {
        const at = segments.slice(0, i + 1).join("/");
        const last = i === segments.length - 1;
        return (
          <span key={at} className={classes.step}>
            <span className={classes.separator} aria-hidden>
              /
            </span>
            {last ? (
              <span className={classes.crumb} data-current aria-current="page">
                {segment}
              </span>
            ) : (
              <UnstyledButton className={classes.crumb} onClick={() => onNavigate(repository.slug, at)}>
                {segment}
              </UnstyledButton>
            )}
          </span>
        );
      })}
    </nav>
  );
}
