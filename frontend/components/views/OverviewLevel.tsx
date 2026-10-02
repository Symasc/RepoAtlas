"use client";

import { useCallback, useMemo, useState } from "react";

import { RepoSwatch } from "@/components/common/RepoSwatch";
import { Graph } from "@/components/graph/Graph";
import { flowEdges, place } from "@/components/graph/flow";
import { languagesOf, type GraphNode, type NodeActions } from "@/components/graph/nodes";
import { useTween } from "@/components/graph/useTween";
import { Connections } from "@/components/inspector/Connections";
import { Contents } from "@/components/inspector/Entries";
import { Evidence } from "@/components/inspector/Evidence";
import { Inspector, OpenButton } from "@/components/inspector/Inspector";
import inspectorClasses from "@/components/inspector/Inspector.module.css";
import type { RepositoryLink } from "@/lib/api/generated";
import { useTopologyOverview } from "@/lib/api/topology";
import { bounds, constellation, GROUP_ID, HUB_ID, type Layout } from "@/lib/graph/layout";
import { absoluteTime, formatCount, plural, relativeTime, shortSha } from "@/lib/model/format";
import { buildOverview, FAMILIES, repositoryId, type Connection, type Family } from "@/lib/model/graph";
import type { RepositoryModel } from "@/lib/model/repositories";
import { useAnimationsOff } from "@/lib/useMotion";

import type { Navigation } from "./navigation";
import { Totals } from "./Totals";
import classes from "./Views.module.css";

const NO_LINKS: readonly RepositoryLink[] = [];

/**
 * Level one: every indexed repository as a constellation. The selected
 * repository (else the one with the most partners) is the anchor at the centre;
 * its partners ring it; repositories with no links stand apart. Selecting one
 * re-centres the map on it and opens it in the inspector; a second press goes in.
 */
export function OverviewLevel({ repositories, nav }: { repositories: RepositoryModel[]; nav: Navigation }) {
  const overview = useTopologyOverview();
  const links = overview.data?.links ?? NO_LINKS;
  const graph = useMemo(() => buildOverview(repositories, links), [repositories, links]);
  const reduced = useAnimationsOff();

  const known = useMemo(() => new Map(repositories.map((r) => [repositoryId(r.slug), r])), [repositories]);
  const selected = nav.selected !== null && known.has(nav.selected) ? nav.selected : null;
  const ready = overview.isSuccess || overview.isError;

  const shape = useMemo(() => constellation(repositories, graph.edges, selected), [repositories, graph.edges, selected]);
  const target = useMemo(() => {
    const layout: Layout = new Map(shape.layout);
    for (const ring of shape.rings) {
      layout.set(ring.id, { x: -ring.rx, y: -ring.ry, width: ring.rx * 2, height: ring.ry * 2 });
    }
    return layout;
  }, [shape]);
  const anchorKey = shape.anchor ?? HUB_ID;
  // glides when the reader re-centres the map, not when the links first arrive
  // and move the anchor off the hub: the first view is drawn where it settles
  const layout = useTween(target, selected ?? "", reduced);
  const extent = useMemo(
    () => bounds([...shape.layout.entries()].filter(([id]) => id !== GROUP_ID).map(([, box]) => box)),
    [shape],
  );

  const open = useCallback(
    (id: string) => {
      const repository = known.get(id);
      if (repository) nav.go({ repository: repository.slug, path: "" }, { via: id });
    },
    [known, nav],
  );
  const activate = useCallback(
    (id: string) => {
      if (!known.has(id)) return;
      if (nav.isSelected(id)) open(id);
      else nav.select(id);
    },
    [known, nav, open],
  );
  const actions = useMemo<NodeActions>(
    () => ({ activate, go: (to) => nav.go({ repository: to.repository, path: to.path }) }),
    [activate, nav],
  );

  const partners = useMemo(() => {
    const map = new Map<string, number>();
    for (const [id, list] of graph.connections) map.set(id, new Set(list.map((c) => c.node)).size);
    return map;
  }, [graph.connections]);

  const nodes = useMemo<GraphNode[]>(() => {
    if (!ready) return [];
    const out: GraphNode[] = [];
    if (shape.group) {
      out.push(
        place(
          { id: GROUP_ID, type: "group", data: { count: graph.independent.length }, zIndex: -1 },
          layout.get(GROUP_ID) ?? shape.group,
          { pointerEvents: "none" },
        ),
      );
    }
    const hub = layout.get(HUB_ID);
    if (shape.anchor === null && hub) {
      const files = repositories.reduce((sum, r) => sum + r.files, 0);
      const note = overview.isError
        ? "Links between them could not be read"
        : repositories.length > 1
          ? "No links between them are recorded"
          : null;
      out.push(place({ id: HUB_ID, type: "hub", data: { repositories: repositories.length, files, note } }, hub));
    }
    for (const repository of repositories) {
      const id = repositoryId(repository.slug);
      const box = layout.get(id);
      if (box) {
        out.push(place({ id, type: "repository", data: { repository, actions, partners: partners.get(id) ?? 0 } }, box));
      }
    }
    return out;
  }, [ready, shape, layout, repositories, graph.independent.length, overview.isError, actions, partners]);

  const edges = useMemo(
    () =>
      flowEdges(graph.edges, {
        route: () => "float",
      }),
    [graph.edges],
  );

  const families = useMemo<Family[]>(() => {
    const present = new Set(graph.edges.map((e) => e.link.family));
    return present.size === 0 ? ["code", "http", "dependency"] : FAMILIES.filter((f) => present.has(f));
  }, [graph.edges]);

  const crossing = links.reduce((sum, link) => sum + link.count, 0);
  const note = overview.isError
    ? "The links between repositories could not be read. Repositories are shown without them."
    : overview.data?.ambiguous_total
      ? `${plural(overview.data.ambiguous_total, "package")} with more than one possible publisher ${overview.data.ambiguous_total === 1 ? "is" : "are"} not drawn.`
      : null;

  const repository = selected ? known.get(selected)! : null;
  const [connection, setConnection] = useState<{ subject: string | null; id: string | null }>({ subject: null, id: null });
  const [tab, setTab] = useState<{ subject: string | null; value: string }>({ subject: null, value: "connections" });
  const rows: Connection[] = selected ? (graph.connections.get(selected) ?? []) : [];
  const currentTab = tab.subject === selected ? tab.value : "connections";
  // Evidence opened with nothing chosen explains the strongest connection
  const explicit = connection.subject === selected ? rows.find((c) => c.id === connection.id) : undefined;
  const picked: Connection | null = explicit ?? (currentTab === "evidence" ? rows[0] : undefined) ?? null;
  const choose = (c: Connection) => {
    setConnection({ subject: selected, id: c.id });
    setTab({ subject: selected, value: "evidence" });
  };
  const hues = useMemo(() => new Map(repositories.map((r) => [r.slug, r.hue])), [repositories]);

  return (
    <div className={classes.level}>
      <div className={classes.stage} data-inspector={repository ? true : undefined}>
        <div className={classes.canvas}>
          <Graph
            nodes={nodes}
            edges={edges}
            level="overview"
            label="Repositories and the links between them"
            selected={selected}
            selectedEdge={picked?.edge ?? null}
            onSelect={nav.select}
            onActivate={activate}
            arrival={{
              direction: nav.arrival.direction,
              from: nav.arrival.previous?.repository ? repositoryId(nav.arrival.previous.repository) : null,
            }}
            extent={extent}
            refit={anchorKey}
            families={families}
            start={<h1 className={classes.levelTitle}>All repositories</h1>}
            end={<Totals repositories={repositories} crossing={overview.isSuccess ? crossing : null} />}
            notice={note && <p>{note}</p>}
          />
        </div>
        {repository && (
          <Inspector
            label={`${repository.name}, inspected`}
            icon={<RepoSwatch hue={repository.hue} size={12} />}
            title={repository.name}
            subtitle={languagesOf(repository, 4) || "Repository"}
            onClose={() => nav.select(null)}
            actions={<OpenButton name={repository.name} onOpen={() => open(selected!)} />}
            tab={currentTab}
            onTab={(value) => setTab({ subject: selected, value })}
            tabs={[
              {
                value: "connections",
                label: "Connections",
                count: rows.length,
                panel: (
                  <Connections
                    connections={rows}
                    chosen={picked?.id ?? null}
                    onChoose={choose}
                    hues={hues}
                    onGo={(partner) => partner.repository && nav.select(repositoryId(partner.repository))}
                  />
                ),
              },
              {
                value: "contents",
                label: "Contents",
                panel: (
                  <Contents
                    repository={repository.slug}
                    path=""
                    onGo={(path) => nav.go({ repository: repository.slug, path }, { via: selected })}
                  />
                ),
              },
              { value: "details", label: "Details", panel: <RepositoryDetails repository={repository} /> },
              {
                value: "evidence",
                label: "Evidence",
                panel: <Evidence connection={picked} subject={`repo:${repository.slug}`} />,
              },
            ]}
          />
        )}
      </div>
    </div>
  );
}

export function RepositoryDetails({ repository: r }: { repository: RepositoryModel }) {
  return (
    <dl className={inspectorClasses.details}>
      <dt>Branch</dt>
      <dd>{r.branch ?? "Detached"}</dd>
      <dt>Commit</dt>
      <dd className={inspectorClasses.mono}>{shortSha(r.headSha) ?? "Unknown"}</dd>
      <dt>Freshness</dt>
      <dd>{r.freshness.label}</dd>
      {r.lastIndexedAt && (
        <>
          <dt>Indexed</dt>
          <dd title={absoluteTime(r.lastIndexedAt)}>{relativeTime(r.lastIndexedAt)}</dd>
        </>
      )}
      <dt>Files</dt>
      <dd>{formatCount(r.files)}</dd>
      <dt>Symbols</dt>
      <dd>{formatCount(r.symbols)}</dd>
      <dt>Relationships</dt>
      <dd>{formatCount(r.relationships)}</dd>
      <dt>Languages</dt>
      <dd>{r.languages.join(", ") || "None recorded"}</dd>
      {r.manifests.length > 0 && (
        <>
          <dt>Manifests</dt>
          <dd className={inspectorClasses.mono}>{r.manifests.slice(0, 12).join(", ")}{r.manifests.length > 12 ? ` +${r.manifests.length - 12}` : ""}</dd>
        </>
      )}
    </dl>
  );
}
