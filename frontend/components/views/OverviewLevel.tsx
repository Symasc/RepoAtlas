"use client";

import { Button } from "@mantine/core";
import { useCallback, useMemo, useState } from "react";

import { RepoSwatch } from "@/components/common/RepoSwatch";
import { Graph } from "@/components/graph/Graph";
import { flowEdges, place } from "@/components/graph/flow";
import { languagesOf, type GraphNode, type NodeActions } from "@/components/graph/nodes";
import { ConnectionsPanel } from "@/components/connections/ConnectionsPanel";
import { InclusionsPanel } from "@/components/connections/InclusionsPanel";
import { Connections } from "@/components/inspector/Connections";
import { Contents } from "@/components/inspector/Entries";
import { Deployments } from "@/components/inspector/Deployments";
import { Evidence } from "@/components/inspector/Evidence";
import { Inspector, OpenButton } from "@/components/inspector/Inspector";
import { Requests } from "@/components/inspector/Requests";
import inspectorClasses from "@/components/inspector/Inspector.module.css";
import type { RepositoryLink } from "@/lib/api/generated";
import { useConnections } from "@/lib/api/connections";
import { useTopologyOverview } from "@/lib/api/topology";
import { districts, wholeId } from "@/lib/graph/districts";
import { summaryId } from "@/lib/graph/districtLayout";
import { inclusionMarks } from "@/lib/graph/inclusionMarks";
import { districtLinks, isolable, wholeLabel } from "@/lib/graph/isolate";
import { HUB_ID } from "@/lib/graph/layout";
import {
  frameFacts,
  hiddenSelectionText,
  isolationText,
  notDrawn,
  notDrawnText,
  summaryFacts,
  whyNotDrawn,
  whyNotDrawnText,
} from "@/lib/graph/overviewCards";
import {
  collapseAllState,
  expandAllState,
  isolateState,
  resolveOverview,
  showAllState,
  toggleState,
  type ResolvedOverview,
} from "@/lib/graph/overviewState";
import { overviewView, visibleEdge, visibleSelection } from "@/lib/graph/overviewView";
import type { OverviewState } from "@/lib/model/location";
import { absoluteTime, formatCount, relativeTime, shortSha } from "@/lib/model/format";
import { NO_FILTERS } from "@/lib/model/connections";
import { buildOverview, FAMILIES, repositoryId, type Box, type Connection, type Family } from "@/lib/model/graph";
import type { RepositoryModel } from "@/lib/model/repositories";

import { openAt, type Navigation } from "./navigation";
import { Totals } from "./Totals";
import classes from "./Views.module.css";

const NO_LINKS: readonly RepositoryLink[] = [];

/** The workspace's own box, when there is no repository to draw. */
const HUB_BOX: Box = { x: 0, y: 0, width: 184, height: 184 };

/**
 * Level one: every indexed repository, in districts. A district is open (a frame
 * holding its repositories as cards) or collapsed to one summary card; which
 * open at rest is the most that stays readable on this screen, and the reader
 * opens and closes the rest. An open district can be isolated: the map then shows
 * it and the districts linked to it, counts the links to the rest, and a notice
 * says so and brings the whole map back. Links are drawn between what is on screen. Selecting
 * a repository opens it in the inspector and highlights it but never moves the
 * map; a second press goes in.
 */
export function OverviewLevel({ repositories, nav }: { repositories: RepositoryModel[]; nav: Navigation }) {
  const overview = useTopologyOverview();
  // the HTTP requests the join left unlinked, counted for the totals and listed in the worklist
  const [connectionsOpen, setConnectionsOpen] = useState(false);
  const [submodulesOpen, setSubmodulesOpen] = useState(false);
  const unresolved = useConnections(NO_FILTERS, true);
  const links = overview.data?.links ?? NO_LINKS;
  const graph = useMemo(() => buildOverview(repositories, links), [repositories, links]);

  const known = useMemo(() => new Map(repositories.map((r) => [repositoryId(r.slug), r])), [repositories]);
  const selected = nav.selected !== null && known.has(nav.selected) ? nav.selected : null;
  const ready = overview.isSuccess || overview.isError;
  const repository = selected ? known.get(selected)! : null;
  const inspecting = repository !== null;

  // -- the districts, and which are open ---------------------------------------------------

  const ds = useMemo(() => (ready ? districts(repositories, graph.edges) : []), [ready, repositories, graph.edges]);
  const dsKey = ds.map((d) => d.id).join("|");
  const byId = useMemo(() => new Map(ds.map((d) => [d.id, d])), [ds]);
  const names = useMemo(() => new Map(repositories.map((r) => [repositoryId(r.slug), r.name])), [repositories]);
  const marks = useMemo(() => inclusionMarks(repositories, graph.edges), [repositories, graph.edges]);
  // which districts a connection joins, and which of them have something to leave out when isolated
  const adjacency = useMemo(() => districtLinks(ds, graph.edges), [ds, graph.edges]);
  const canIsolate = useMemo(() => isolable(ds, adjacency), [ds, adjacency]);

  // The canvas's size decides what opens at rest. While the inspector narrows it
  // the size is not taken: the map holds still, and follows the canvas again once
  // the inspector closes.
  const [pane, setPane] = useState<{ width: number; height: number } | null>(null);
  const onPaneSize = useCallback(
    (size: { width: number; height: number }) =>
      setPane((prev) =>
        (prev === null || !inspecting) && (prev?.width !== size.width || prev?.height !== size.height) ? size : prev,
      ),
    [inspecting],
  );
  // what was selected, or came from, on arrival: its district opens, now and never again
  const [pinned] = useState<readonly string[]>(() =>
    [nav.selected, nav.arrival.previous?.repository ? repositoryId(nav.arrival.previous.repository) : null].filter(
      (x): x is string => x !== null,
    ),
  );
  const paneWidth = pane?.width;
  const paneHeight = pane?.height;
  // which districts are open, and which are on the map, is in the address; an address that says nothing is the adaptive default
  const resolveFor = useCallback(
    (state: OverviewState) =>
      paneWidth !== undefined && paneHeight !== undefined && ds.length > 0
        ? resolveOverview({ districts: ds, state, pane: { width: paneWidth, height: paneHeight }, pinned, links: adjacency })
        : null,
    [ds, paneWidth, paneHeight, pinned, adjacency],
  );
  const resolved = useMemo(() => resolveFor(nav.overview), [resolveFor, nav.overview]);
  const isolation = resolved?.isolation ?? null;

  const view = useMemo(
    () =>
      resolved
        ? overviewView({
            districts: ds,
            edges: graph.edges,
            expanded: resolved.expanded,
            shelf: resolved.shelf,
            visible: resolved.shown,
          })
        : null,
    [ds, graph.edges, resolved],
  );

  // a press is resolved against the address as it is now, so two presses within one frame are both kept
  const change = useCallback(
    (next: (r: ResolvedOverview) => OverviewState) => {
      const r = resolveFor(nav.currentOverview());
      if (r) nav.setOverview(next(r));
    },
    [resolveFor, nav],
  );
  const toggle = useCallback((id: string) => change((r) => toggleState(r, id)), [change]);
  const expandAll = useCallback(() => change(expandAllState), [change]);
  const collapseAll = useCallback(() => change(collapseAllState), [change]);
  // isolating and coming back are places to return to: they push an entry, so Back undoes them
  const isolateDistrict = useCallback(
    (id: string) => {
      const r = resolveFor(nav.currentOverview());
      const next = r ? isolateState(r, ds, id) : null;
      if (next) nav.setOverview(next, { push: true });
    },
    [resolveFor, ds, nav],
  );
  const showAll = useCallback(() => {
    const r = resolveFor(nav.currentOverview());
    if (r) nav.setOverview(showAllState(r), { push: true });
  }, [resolveFor, nav]);

  // -- what the cards ask for ----------------------------------------------------------------

  const open = useCallback(
    (id: string) => {
      const repository = known.get(id);
      if (repository) nav.go({ repository: repository.slug, path: "" }, { via: id });
    },
    [known, nav],
  );
  const activate = useCallback(
    (id: string) => {
      if (id.startsWith("summary:")) {
        const d = ds.find((district) => summaryId(district.id) === id);
        if (d) toggle(d.id);
        return;
      }
      if (id.startsWith("district:")) {
        if (byId.has(id)) toggle(id);
        return;
      }
      if (!known.has(id)) return;
      if (nav.isSelected(id)) open(id);
      else nav.select(id);
    },
    [ds, byId, toggle, known, nav, open],
  );
  const actions = useMemo<NodeActions>(
    () => ({ activate, go: (to) => nav.go({ repository: to.repository, path: to.path }), isolate: isolateDistrict }),
    [activate, nav, isolateDistrict],
  );

  const partners = useMemo(() => {
    const map = new Map<string, number>();
    for (const [id, list] of graph.connections) map.set(id, new Set(list.map((c) => c.node)).size);
    return map;
  }, [graph.connections]);

  const nodes = useMemo<GraphNode[]>(() => {
    if (!ready) return [];
    if (ds.length === 0) {
      // nothing indexed: the workspace alone
      const note = overview.isError ? "Links between them could not be read" : null;
      return [place({ id: HUB_ID, type: "hub", data: { repositories: 0, files: 0, note } }, HUB_BOX)];
    }
    if (!view) return [];
    const out: GraphNode[] = [];
    for (const [id, box] of view.layout.frames) {
      const d = byId.get(id);
      if (!d) continue;
      // isolating the isolated district, or one that leaves nothing out, would change nothing
      const offered = canIsolate.has(wholeId(d)) && wholeId(d) !== isolation?.focus;
      out.push(
        place(
          {
            id,
            type: "district",
            data: { facts: frameFacts(d, marks), actions, isolate: offered ? { label: wholeLabel(d) } : null },
            zIndex: -1,
          },
          box,
          { pointerEvents: "none" },
        ),
      );
    }
    for (const d of ds) {
      const id = summaryId(d.id);
      const box = view.layout.cards.get(id);
      if (box) {
        out.push(place({ id, type: "districtSummary", data: { facts: summaryFacts(view, d, names), actions } }, box));
      }
    }
    for (const repository of repositories) {
      const id = repositoryId(repository.slug);
      const box = view.layout.cards.get(id);
      if (!box) continue;
      const mark = marks.get(id);
      const inclusion = mark
        ? {
            parentName: mark.parent ? (names.get(mark.parent) ?? null) : null,
            depth: mark.depth,
            kind: mark.kind,
            label: mark.label,
          }
        : null;
      out.push(place({ id, type: "repository", data: { repository, actions, partners: partners.get(id) ?? 0, inclusion } }, box));
    }
    return out;
  }, [ready, ds, view, byId, marks, names, repositories, overview.isError, actions, partners, canIsolate, isolation]);

  const edges = useMemo(() => (view ? flowEdges(view.edges, { route: () => "float" }) : []), [view]);
  const extent = view ? view.layout.extent : ready && ds.length === 0 ? HUB_BOX : null;

  const families = useMemo<Family[]>(() => {
    const present = new Set(graph.edges.map((e) => e.link.family));
    return present.size === 0 ? ["code", "http", "dependency"] : FAMILIES.filter((f) => present.has(f));
  }, [graph.edges]);

  const crossing = links.reduce((sum, link) => sum + link.count, 0);
  const note = overview.isError ? "The links between repositories could not be read. Repositories are shown without them." : null;

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

  // what is on the map for the selection and the chosen connection, and what is left out of it
  const shownSelection = view ? visibleSelection(view, selected) : null;
  const shownEdge = picked && view ? visibleEdge(view, picked.edge) : null;
  const previous = nav.arrival.previous?.repository ? repositoryId(nav.arrival.previous.repository) : null;
  let statusText: string | null = null;
  if (view) {
    if (picked && shownEdge === null) {
      const edge = graph.edges.find((e) => e.id === picked.edge);
      const why = edge ? whyNotDrawn(view, ds, edge) : null;
      if (why) statusText = whyNotDrawnText(why);
    }
    // a selection in a district that isolating leaves out is on no card: say so
    if (statusText === null && repository !== null && shownSelection === null) statusText = hiddenSelectionText(repository.name);
    statusText ??= notDrawnText(notDrawn(view), overview.data?.ambiguous_total ?? 0);
  }

  return (
    <div className={classes.level}>
      <div className={classes.stage} data-inspector={repository ? true : undefined}>
        <div
          className={classes.canvas}
          data-overview
          data-pane={pane ? `${pane.width}x${pane.height}` : undefined}
          data-level={resolved?.level}
          data-readable={resolved ? String(resolved.readable) : undefined}
          data-isolate={isolation?.focus}
        >
          <Graph
            nodes={nodes}
            edges={edges}
            level="overview"
            label="Repositories and the links between them"
            selected={shownSelection}
            selectedEdge={shownEdge}
            onSelect={nav.select}
            onActivate={activate}
            arrival={{
              direction: nav.arrival.direction,
              from: view && previous ? visibleSelection(view, previous) : null,
            }}
            extent={extent}
            refit={`${dsKey}|${isolation?.focus ?? ""}|${pane?.width}x${pane?.height}`}
            holdOnGrowth
            holdOnResize
            onPaneSize={onPaneSize}
            families={families}
            start={
              <>
                <h1 className={classes.levelTitle}>All repositories</h1>
                <div className={classes.toolbarGroup} role="group" aria-label="Districts">
                  <Button size="compact-xs" variant="subtle" onClick={expandAll} disabled={view === null}>
                    Expand all districts
                  </Button>
                  <Button size="compact-xs" variant="subtle" onClick={collapseAll} disabled={view === null}>
                    Collapse all districts
                  </Button>
                </div>
              </>
            }
            end={
              <Totals
                repositories={repositories}
                crossing={overview.isSuccess ? crossing : null}
                unresolved={unresolved.data ? unresolved.data.summary.unresolved : null}
                onOpenConnections={() => setConnectionsOpen(true)}
                unlinkedSubmodules={overview.data?.unsettled_inclusions_total ?? null}
                onOpenSubmodules={() => setSubmodulesOpen(true)}
              />
            }
            notice={
              (note || isolation) && (
                <>
                  {note && <p>{note}</p>}
                  {isolation && (
                    <div className={classes.isolation}>
                      <p role="status">{isolationText(isolation)}</p>
                      <Button size="compact-xs" variant="default" onClick={showAll}>
                        Show all districts
                      </Button>
                    </div>
                  )}
                </>
              )
            }
            status={statusText === null ? null : <span title={statusText}>{statusText}</span>}
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
              {
                value: "requests",
                label: "Requests",
                panel: (
                  <Requests
                    scope={{ repository: repository.slug, path: "" }}
                    subject={`repo:${repository.slug}`}
                    onOpenPlace={(at) => openAt(nav, at)}
                  />
                ),
              },
              {
                value: "deployments",
                label: "Deployments",
                panel: <Deployments repository={repository.slug} />,
              },
              { value: "details", label: "Details", panel: <RepositoryDetails repository={repository} /> },
              {
                value: "evidence",
                label: "Evidence",
                panel: (
                  <Evidence connection={picked} subject={`repo:${repository.slug}`} onOpenPlace={(at) => openAt(nav, at)} />
                ),
              },
            ]}
          />
        )}
      </div>
      <ConnectionsPanel
        opened={connectionsOpen}
        onClose={() => setConnectionsOpen(false)}
        repositories={repositories}
        onOpenPlace={(at) => openAt(nav, at)}
      />
      <InclusionsPanel
        opened={submodulesOpen}
        onClose={() => setSubmodulesOpen(false)}
        unsettled={overview.data?.unsettled_inclusions ?? []}
        total={overview.data?.unsettled_inclusions_total ?? 0}
        repositories={repositories}
      />
    </div>
  );
}

function RepositoryDetails({ repository: r }: { repository: RepositoryModel }) {
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
