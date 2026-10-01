"use client";

import { Button, Loader } from "@mantine/core";
import { ArrowLeftIcon } from "@phosphor-icons/react";
import { useCallback, useMemo, useState, type ReactNode } from "react";

import { Graph } from "@/components/graph/Graph";
import { flowEdges, place } from "@/components/graph/flow";
import { EntityBadge, Monogram, PathBadge } from "@/components/graph/glyphs";
import type { GraphNode, NodeActions } from "@/components/graph/nodes";
import { Connections } from "@/components/inspector/Connections";
import { Contents, EntryList } from "@/components/inspector/Entries";
import { Evidence } from "@/components/inspector/Evidence";
import { Inspector, OpenButton, type InspectorTab } from "@/components/inspector/Inspector";
import { ApiError } from "@/lib/api/client";
import type { TopologyChildrenResponse } from "@/lib/api/generated";
import { useChildren, usePreviews } from "@/lib/api/topology";
import { bounds, layoutOpened } from "@/lib/graph/layout";
import { formatCount, plural } from "@/lib/model/format";
import {
  buildOpened,
  childOf,
  directoryId,
  FAMILIES,
  FILES_ID,
  FRAME_ID,
  PREVIEW_ROWS,
  type Connection,
  type Family,
  type OpenedGraph,
} from "@/lib/model/graph";
import type { RepositoryModel } from "@/lib/model/repositories";

import { FileLevel } from "./FileLevel";
import type { Navigation } from "./navigation";
import { Trail } from "./Trail";
import classes from "./Views.module.css";

/** At most this many cards list their contents; the rest still show their counts. */
const PREVIEWED = 15;

/**
 * A repository opened at a path. A directory draws as a frame of cards; a file
 * as its neighbourhood (./FileLevel). Which one is known once the path is read.
 */
export function RepositoryLevel({
  repository,
  repositories,
  nav,
}: {
  repository: RepositoryModel;
  repositories: RepositoryModel[];
  nav: Navigation;
}) {
  const path = nav.place.path;
  const children = useChildren(repository.slug, path);
  if (children.data?.target === "file") {
    return <FileLevel key={path} repository={repository} repositories={repositories} data={children.data} nav={nav} />;
  }
  return (
    <DirectoryLevel
      repository={repository}
      repositories={repositories}
      nav={nav}
      data={children.data ?? null}
      pending={children.isPending}
      error={children.error instanceof ApiError ? children.error : null}
    />
  );
}

function DirectoryLevel({
  repository,
  repositories,
  nav,
  data,
  pending,
  error,
}: {
  repository: RepositoryModel;
  repositories: RepositoryModel[];
  nav: Navigation;
  data: TopologyChildrenResponse | null;
  pending: boolean;
  error: ApiError | null;
}) {
  const path = nav.place.path;
  const directoryPaths = useMemo(
    () =>
      (data?.entries ?? [])
        .filter((e) => e.kind === "directory")
        .slice(0, PREVIEWED)
        .map((e) => e.path),
    [data],
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

  const graph = useMemo(
    () => (data ? buildOpened({ repository, path, children: data, previews }) : null),
    [data, repository, path, previews],
  );
  const layout = useMemo(() => (graph ? layoutOpened(graph) : null), [graph]);
  const ids = useMemo(() => new Set(layout ? layout.keys() : []), [layout]);
  const selected = nav.selected !== null && ids.has(nav.selected) && nav.selected !== FRAME_ID ? nav.selected : null;


  // the connection the inspector explains, and its tab: both belong to one selection
  const [view, setView] = useState<{ subject: string | null; picked: Connection | null; tab: string }>({
    subject: null,
    picked: null,
    tab: "connections",
  });
  const tab = view.subject === selected ? view.tab : "connections";
  // Evidence opened with nothing chosen explains the strongest connection
  const strongest = selected && graph ? (graph.connections.get(selected)?.[0] ?? null) : null;
  const picked = (view.subject === selected ? view.picked : null) ?? (tab === "evidence" ? strongest : null);

  /** Where opening a node leads: a directory, a place, another repository. */
  const destination = useCallback(
    (id: string) => {
      if (!graph) return null;
      const card = graph.directories.find((d) => d.id === id);
      if (card) return { repository: repository.slug, path: card.path };
      const placeNode = graph.places.find((p) => p.id === id);
      if (placeNode) return { repository: repository.slug, path: placeNode.path };
      const peer = graph.peers.find((p) => p.id === id);
      if (peer) return { repository: peer.repository, path: "" };
      return null;
    },
    [graph, repository.slug],
  );
  const open = useCallback(
    (id: string) => {
      const to = destination(id);
      if (to) nav.go(to, { via: id });
    },
    [destination, nav],
  );
  const activate = useCallback(
    (id: string) => {
      if (!ids.has(id) || id === FRAME_ID) return;
      if (nav.isSelected(id)) open(id);
      else nav.select(id);
    },
    [ids, nav, open],
  );
  const actions = useMemo<NodeActions>(
    () => ({
      activate,
      go: (to) => {
        // a row inside a card: zoom into that card on the way
        const card = graph?.directories.find((d) => to.path === d.path || to.path.startsWith(`${d.path}/`));
        const via = card?.id ?? (graph?.files?.entries.some((e) => e.path === to.path) ? FILES_ID : null);
        nav.go({ repository: to.repository, path: to.path }, { via });
      },
    }),
    [activate, graph, nav],
  );
  const hues = useMemo(() => new Map(repositories.map((r) => [r.slug, r.hue])), [repositories]);

  const nodes = useMemo<GraphNode[]>(() => {
    if (!graph || !layout) return [];
    const at = (id: string) => layout.get(id)!;
    const out: GraphNode[] = [
      place({ id: FRAME_ID, type: "frame", data: { frame: graph.frame, repository }, zIndex: -1 }, at(FRAME_ID), {
        pointerEvents: "none",
      }),
    ];
    for (const card of graph.directories) {
      out.push(place({ id: card.id, type: "directory", data: { card, repository: repository.slug, actions } }, at(card.id)));
    }
    if (graph.files) {
      out.push(place({ id: FILES_ID, type: "files", data: { card: graph.files, repository: repository.slug, actions } }, at(FILES_ID)));
    }
    for (const node of graph.shared) out.push(place({ id: node.id, type: "shared", data: { node, actions } }, at(node.id)));
    for (const node of graph.peers) {
      out.push(place({ id: node.id, type: "peer", data: { node, hue: hues.get(node.repository) ?? null, actions } }, at(node.id)));
    }
    for (const node of graph.places) {
      out.push(place({ id: node.id, type: "place", data: { node, repository: repository.name, actions } }, at(node.id)));
    }
    for (const node of graph.more) {
      out.push(
        place(
          { id: node.id, type: "more", data: { node, actions, noun: node.side === "left" ? "places" : "kinds" } },
          at(node.id),
        ),
      );
    }
    return out;
  }, [graph, layout, repository, actions, hues]);

  const edges = useMemo(() => {
    if (!graph) return [];
    const cards = new Set([...graph.directories.map((d) => d.id), FILES_ID]);
    return flowEdges(graph.edges, {
      route: (e) => (e.scope === "aggregate" ? "lane" : cards.has(e.source) && cards.has(e.target) ? "float" : "side"),
      curve: () => 0.12,
    });
  }, [graph]);
  const extent = useMemo(() => (layout ? bounds(layout.values()) : null), [layout]);
  const families = useMemo<Family[]>(() => {
    const present = new Set(graph?.edges.map((e) => e.link.family) ?? []);
    return FAMILIES.filter((f) => present.has(f));
  }, [graph]);

  // arriving from inside a child: start on that child's card
  const previous = nav.arrival.previous;
  let from: string | null = null;
  if (graph && previous?.repository === repository.slug) {
    const child = childOf(path, previous.path);
    if (child !== null) {
      from = graph.directories.some((d) => d.path === child)
        ? directoryId(child)
        : graph.files?.entries.some((e) => e.path === child)
          ? FILES_ID
          : null;
    }
  }

  const depth = path ? path.split("/").length + 1 : 1;
  return (
    <div className={classes.level}>
      <div className={classes.stage} data-inspector={selected && graph ? true : undefined}>
        <div className={classes.canvas}>
          <Graph
            nodes={nodes}
            edges={edges}
            level={`${repository.slug}:${path}`}
            label={`${repository.name}${path ? `, ${path}` : ""}: its contents and links`}
            selected={selected}
            selectedEdge={picked?.edge ?? null}
            onSelect={nav.select}
            onActivate={activate}
            arrival={{ direction: nav.arrival.direction, from }}
            extent={extent}
            families={families}
            topLeft={
              <div className={classes.overlayStack}>
                <Trail repository={repository} nav={nav} file={false} />
                {error ? (
                  <div className={classes.problem} role="alert">
                    <p>{error.message}</p>
                    <Button
                      size="xs"
                      variant="default"
                      leftSection={<ArrowLeftIcon size={13} />}
                      onClick={() => nav.go({ repository: repository.slug, path: "" })}
                    >
                      Back to {repository.name}
                    </Button>
                  </div>
                ) : pending ? (
                  <p className={classes.note}>
                    <Loader size={12} type="dots" /> Reading {path || repository.name}
                  </p>
                ) : (
                  graph && data && <Caveats graph={graph} data={data} />
                )}
              </div>
            }
            topRight={
              data && (
                <div className={classes.levelInfo}>
                  <span className={classes.depth}>Depth {depth}</span>
                  <span>{plural(data.entries_total ?? 0, "entry", "entries")}</span>
                </div>
              )
            }
          />
        </div>
        {selected && graph && (
          <CardInspector
            key={selected}
            id={selected}
            graph={graph}
            repository={repository}
            hues={hues}
            onClose={() => nav.select(null)}
            onOpen={destination(selected) ? () => open(selected) : null}
            nav={nav}
            data={data!}
            picked={picked}
            tab={tab}
            onPick={(c) => setView({ subject: selected, picked: c, tab: "evidence" })}
            onTab={(value) => setView({ subject: selected, picked, tab: value })}
          />
        )}
      </div>
    </div>
  );
}

/** The inspector for whatever is selected at a directory level. */
function CardInspector({
  id,
  graph,
  repository,
  hues,
  onClose,
  onOpen,
  nav,
  data,
  picked,
  tab,
  onPick,
  onTab,
}: {
  id: string;
  graph: OpenedGraph;
  repository: RepositoryModel;
  hues: ReadonlyMap<string, number>;
  onClose: () => void;
  onOpen: (() => void) | null;
  nav: Navigation;
  data: TopologyChildrenResponse;
  picked: Connection | null;
  tab: string;
  onPick: (connection: Connection) => void;
  onTab: (value: string) => void;
}) {
  const rows = graph.connections.get(id) ?? [];
  const go = (path: string) => nav.go({ repository: repository.slug, path }, { via: id });

  let icon: ReactNode = <PathBadge path="" file={false} size={34} />;
  let title = id;
  let subtitle: string | null = null;
  const tabs: InspectorTab[] = [];
  const card = graph.directories.find((d) => d.id === id);
  const shared = graph.shared.find((s) => s.id === id);
  const peer = graph.peers.find((p) => p.id === id);
  const placeNode = graph.places.find((p) => p.id === id);
  const more = graph.more.find((m) => m.id === id);
  if (card) {
    title = card.name;
    subtitle = `${plural(card.files, "file")} · ${repository.name}/${card.path}`;
    tabs.push({ value: "contents", label: "Contents", panel: <Contents repository={repository.slug} path={card.path} onGo={go} /> });
  } else if (id === FILES_ID && graph.files) {
    icon = <PathBadge path="file" file size={34} />;
    title = "Files here";
    subtitle = plural(graph.files.total, "file");
    tabs.push({
      value: "contents",
      label: "Files",
      count: graph.files.total,
      panel: <EntryList entries={graph.files.entries} total={data.entries_total} onGo={go} />,
    });
  } else if (shared) {
    icon = <EntityBadge entityType={shared.entityType} size={34} />;
    title = shared.label;
    subtitle = `Shared across the index · ${plural(shared.count, "relationship")} from here`;
  } else if (peer) {
    icon = <Monogram name={peer.repository} hue={hues.get(peer.repository) ?? null} size={34} />;
    title = peer.repository;
    subtitle = `Another repository · ${plural(peer.count, "relationship")}`;
  } else if (placeNode) {
    icon = <PathBadge path={placeNode.path} size={34} />;
    title = placeNode.path.slice(placeNode.path.lastIndexOf("/") + 1);
    subtitle = `Elsewhere in ${repository.name} · ${placeNode.path}`;
  } else if (more) {
    title = `${formatCount(more.nodes)} more ${more.side === "left" ? "places" : "kinds"}`;
    subtitle = `${plural(more.count, "relationship")}, folded to keep the map readable`;
  }
  tabs.unshift({
    value: "connections",
    label: "Connections",
    count: rows.length,
    panel: (
      <Connections
        connections={rows}
        chosen={picked?.id ?? null}
        onChoose={onPick}
        hues={hues}
        onGo={(partner) => partner.repository !== undefined && partner.path !== undefined && nav.go({ repository: partner.repository, path: partner.path })}
      />
    ),
  });
  tabs.push({ value: "evidence", label: "Evidence", panel: <Evidence connection={picked} subject={`${repository.slug}:${id}`} /> });

  return (
    <Inspector
      label={`${title}, inspected`}
      icon={icon}
      title={title}
      subtitle={subtitle}
      onClose={onClose}
      actions={
        onOpen && (
          <OpenButton name={title} onOpen={onOpen} />
        )
      }
      tab={tab}
      onTab={onTab}
      tabs={tabs}
    />
  );
}

/** What is on screen but incomplete, said plainly rather than left out. */
function Caveats({ graph, data }: { graph: OpenedGraph; data: TopologyChildrenResponse }) {
  const notes: string[] = [];
  if (data.has_more) notes.push(`Showing the first ${formatCount(data.entries?.length ?? 0)} of ${formatCount(data.entries_total ?? 0)} entries.`);
  if (data.truncated) notes.push(`Showing the strongest ${formatCount(data.links?.length ?? 0)} of ${formatCount(data.links_total ?? 0)} link groups.`);
  if (data.complete === false) notes.push("Some links were not read: this level has more than can be counted at once.");
  if (graph.undrawn > 0) notes.push(`${plural(graph.undrawn, "link group")} reach${graph.undrawn === 1 ? "es" : ""} somewhere not on screen.`);
  if (notes.length === 0) return null;
  return <p className={classes.note}>{notes.join(" ")}</p>;
}
