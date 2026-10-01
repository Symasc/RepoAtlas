"use client";

import { useCallback, useMemo, useState } from "react";

import { Graph } from "@/components/graph/Graph";
import { flowEdges, place } from "@/components/graph/flow";
import { PathBadge } from "@/components/graph/glyphs";
import type { GraphNode, NodeActions } from "@/components/graph/nodes";
import { CodeView } from "@/components/inspector/CodeView";
import { Connections } from "@/components/inspector/Connections";
import { EntryList } from "@/components/inspector/Entries";
import { Evidence, type LineSpan } from "@/components/inspector/Evidence";
import { Inspector } from "@/components/inspector/Inspector";
import inspectorClasses from "@/components/inspector/Inspector.module.css";
import type { TopologyChildrenResponse } from "@/lib/api/generated";
import { useChildren, useSource } from "@/lib/api/topology";
import { bounds, layoutFile } from "@/lib/graph/layout";
import { formatBytes, formatCount, plural, shortSha } from "@/lib/model/format";
import {
  buildFile,
  FAMILIES,
  FILE_ID,
  NEIGHBOURS,
  NEIGHBOURS_EXPANDED,
  type Connection,
  type Family,
} from "@/lib/model/graph";
import type { RepositoryModel } from "@/lib/model/repositories";

import type { Navigation } from "./navigation";
import { Trail } from "./Trail";
import classes from "./Views.module.css";

function dirname(path: string): string {
  const cut = path.lastIndexOf("/");
  return cut > 0 ? path.slice(0, cut) : "";
}

/**
 * A file, at the centre of its neighbourhood: what links to it on the left,
 * what it links to on the right, the strongest few of each, the rest a press
 * away. The inspector beside it holds the detail: every connection, the source,
 * the file's facts, and the evidence for the connection chosen.
 */
export function FileLevel({
  repository,
  repositories,
  data,
  nav,
}: {
  repository: RepositoryModel;
  repositories: RepositoryModel[];
  data: TopologyChildrenResponse;
  nav: Navigation;
}) {
  const path = nav.place.path;
  const [expanded, setExpanded] = useState({ left: false, right: false });
  const graph = useMemo(
    () =>
      buildFile({
        repository,
        path,
        children: data,
        limit: {
          left: expanded.left ? NEIGHBOURS_EXPANDED : NEIGHBOURS,
          right: expanded.right ? NEIGHBOURS_EXPANDED : NEIGHBOURS,
        },
      }),
    [repository, path, data, expanded],
  );
  const layout = useMemo(() => layoutFile(graph), [graph]);
  const extent = useMemo(() => bounds(layout.values()), [layout]);

  // the file's own facts: its language from the directory listing that holds
  // it (usually already read on the way here), its size from the source read
  const parent = useChildren(repository.slug, dirname(path));
  const entry = parent.data?.entries?.find((e) => e.path === path) ?? null;
  const source = useSource(repository.slug, path, true);
  const lines = source.data?.file.line_count ?? null;

  const ids = useMemo(() => new Set(layout.keys()), [layout]);
  const selected = nav.selected !== null && ids.has(nav.selected) && nav.selected !== FILE_ID ? nav.selected : null;

  const neighbours = useMemo(() => new Map([...graph.left, ...graph.right].map((n) => [n.id, n])), [graph]);
  const open = useCallback(
    (id: string) => {
      const n = neighbours.get(id);
      if (n?.kind === "file") nav.go({ repository: repository.slug, path: n.target }, { via: id });
      if (n?.kind === "repository") nav.go({ repository: n.target, path: "" }, { via: id });
    },
    [neighbours, nav, repository.slug],
  );
  const activate = useCallback(
    (id: string) => {
      if (id === FILE_ID) {
        nav.select(null);
        return;
      }
      if (id === "more:left" || id === "more:right") {
        setExpanded((e) => ({ ...e, [id === "more:left" ? "left" : "right"]: true }));
        return;
      }
      if (!ids.has(id)) return;
      if (nav.isSelected(id)) open(id);
      else nav.select(id);
    },
    [ids, nav, open],
  );
  const actions = useMemo<NodeActions>(
    () => ({ activate, go: (to) => nav.go({ repository: to.repository, path: to.path }) }),
    [activate, nav],
  );
  const hues = useMemo(() => new Map(repositories.map((r) => [r.slug, r.hue])), [repositories]);

  const nodes = useMemo<GraphNode[]>(() => {
    const at = (id: string) => layout.get(id)!;
    const out: GraphNode[] = [
      place(
        {
          id: FILE_ID,
          type: "anchor",
          data: {
            name: graph.anchor.name,
            path,
            repository,
            language: entry?.language ?? null,
            declarations: graph.anchor.declarations,
            lines,
            actions,
          },
        },
        at(FILE_ID),
      ),
    ];
    for (const n of [...graph.left, ...graph.right]) {
      if (n.kind === "file") {
        out.push(place({ id: n.id, type: "neighbour", data: { node: n, actions } }, at(n.id)));
      } else if (n.kind === "repository") {
        out.push(
          place(
            { id: n.id, type: "peer", data: { node: { id: n.id, repository: n.target, count: n.count }, hue: hues.get(n.target) ?? null, actions } },
            at(n.id),
          ),
        );
      } else {
        out.push(
          place({ id: n.id, type: "shared", data: { node: { id: n.id, entityType: n.target, label: n.label, count: n.count }, actions } }, at(n.id)),
        );
      }
    }
    for (const m of graph.more) {
      out.push(place({ id: m.id, type: "more", data: { node: m, actions, noun: "neighbours" } }, at(m.id)));
    }
    return out;
  }, [graph, layout, path, repository, entry, lines, actions, hues]);

  const edges = useMemo(() => flowEdges(graph.edges, { route: () => "side", labelled: true }), [graph]);
  const families = useMemo<Family[]>(() => {
    const present = new Set(graph.edges.map((e) => e.link.family));
    return FAMILIES.filter((f) => present.has(f));
  }, [graph]);

  // the connection being explained: one chosen in the list, else the strongest
  // with the selected neighbour
  const [view, setView] = useState<{ picked: string | null; tab: string; code: LineSpan | null }>({
    picked: null,
    tab: "connections",
    code: null,
  });
  const explicit = graph.connections.find((c) => c.id === view.picked) ?? null;
  const picked =
    explicit && (selected === null || explicit.node === selected)
      ? explicit
      : selected
        ? (graph.byNode.get(selected)?.[0] ?? null)
        : (explicit ?? (view.tab === "evidence" ? (graph.connections[0] ?? null) : null));
  const choose = (c: Connection) => {
    setView((v) => ({ ...v, picked: c.id, tab: "evidence" }));
    if (c.node !== selected && ids.has(c.node)) nav.select(c.node);
  };
  const showInCode = (span: LineSpan) => setView((v) => ({ ...v, tab: "code", code: span }));

  const declarations = (data.entries ?? []).filter((e) => e.kind === "declaration");
  const notes: string[] = [];
  if (graph.within > 0) notes.push(`${plural(graph.within, "link group")} between parts of this file ${graph.within === 1 ? "is" : "are"} not drawn.`);
  if (data.truncated) notes.push(`Showing the strongest ${formatCount(data.links?.length ?? 0)} of ${formatCount(data.links_total ?? 0)} link groups.`);
  if (data.complete === false) notes.push("Some links were not read: this file has more than can be counted at once.");

  return (
    <div className={classes.level}>
      <div className={classes.stage} data-inspector>
        <div className={classes.canvas}>
          <Graph
            nodes={nodes}
            edges={edges}
            level={`${repository.slug}:${path}`}
            label={`${graph.anchor.name}: what links to it and what it links to`}
            selected={selected}
            selectedEdge={picked?.edge ?? null}
            onSelect={nav.select}
            onActivate={activate}
            arrival={{ direction: nav.arrival.direction, from: null }}
            extent={extent}
            refit={`${expanded.left}:${expanded.right}`}
            families={families.length > 0 ? families : ["code"]}
            topLeft={
              <div className={classes.overlayStack}>
                <Trail repository={repository} nav={nav} file />
                {notes.length > 0 && <p className={classes.note}>{notes.join(" ")}</p>}
              </div>
            }
            topRight={
              <div className={classes.levelInfo}>
                <span className={classes.depth}>File</span>
                <span>
                  {plural(graph.left.length + graph.right.length + graph.more.reduce((s, m) => s + m.nodes, 0), "neighbour")}
                </span>
              </div>
            }
          />
        </div>
        <Inspector
          label={`${graph.anchor.name}, inspected`}
          icon={<PathBadge path={path} file size={38} />}
          title={graph.anchor.name}
          subtitle={`${repository.name} / ${dirname(path) || "root"}`}
          tab={view.tab}
          onTab={(value) => setView((v) => ({ ...v, tab: value }))}
          tabs={[
            {
              value: "connections",
              label: "Connections",
              count: graph.connections.length,
              panel: (
                <Connections
                  connections={graph.connections}
                  chosen={picked?.id ?? null}
                  onChoose={choose}
                  hues={hues}
                  onGo={(partner) =>
                    partner.repository !== undefined &&
                    partner.path !== undefined &&
                    nav.go({ repository: partner.repository, path: partner.path })
                  }
                />
              ),
            },
            { value: "code", label: "Code", panel: <CodeView repository={repository.slug} path={path} highlight={view.code} /> },
            {
              value: "details",
              label: "Details",
              panel: (
                <>
                  <dl className={inspectorClasses.details}>
                    <dt>Path</dt>
                    <dd className={inspectorClasses.mono}>{path}</dd>
                    <dt>Repository</dt>
                    <dd>
                      {repository.name}
                      {repository.branch ? ` · ${repository.branch}` : ""}
                      {repository.headSha ? ` @ ${shortSha(repository.headSha)}` : ""}
                    </dd>
                    {entry?.language && (
                      <>
                        <dt>Language</dt>
                        <dd>{entry.language}</dd>
                      </>
                    )}
                    {source.data && (
                      <>
                        <dt>Size</dt>
                        <dd>{formatBytes(source.data.file.size)}</dd>
                        {source.data.file.line_count != null && (
                          <>
                            <dt>Lines</dt>
                            <dd>{formatCount(source.data.file.line_count)}</dd>
                          </>
                        )}
                        <dt>Indexed as</dt>
                        <dd className={inspectorClasses.mono} title={source.data.file.indexed_content_hash}>
                          {source.data.file.indexed_content_hash.slice(0, 12)}
                        </dd>
                      </>
                    )}
                    <dt>Freshness</dt>
                    <dd>{repository.freshness.label} (the repository&apos;s)</dd>
                  </dl>
                  <h3 className={inspectorClasses.subhead}>Declarations · {formatCount(declarations.length)}</h3>
                  <EntryList
                    entries={declarations}
                    total={data.entries_total}
                    onGo={() => {}}
                    onLine={(line) => showInCode({ start: line, end: line })}
                  />
                </>
              ),
            },
            {
              value: "evidence",
              label: "Evidence",
              panel: (
                <Evidence
                  connection={picked}
                  subject={`file:${repository.slug}:${path}`}
                  here={{ repository: repository.slug, path }}
                  onShowInCode={showInCode}
                />
              ),
            },
          ]}
        />
      </div>
    </div>
  );
}
