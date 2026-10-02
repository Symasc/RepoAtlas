"use client";

import { Button, SegmentedControl } from "@mantine/core";
import { ArrowSquareOutIcon, FlowArrowIcon } from "@phosphor-icons/react";
import { useCallback, useMemo, useState } from "react";

import { CodeReader } from "@/components/code/CodeReader";
import { Graph, Notice, Toolbar } from "@/components/graph/Graph";
import { flowEdges, place } from "@/components/graph/flow";
import { EntityBadge, PathBadge } from "@/components/graph/glyphs";
import type { GraphNode, NodeActions } from "@/components/graph/nodes";
import { typeNoun } from "@/components/graph/traceNodes";
import { Connections } from "@/components/inspector/Connections";
import { EntryList } from "@/components/inspector/Entries";
import { Evidence } from "@/components/inspector/Evidence";
import { Inspector, Quiet, type InspectorTab } from "@/components/inspector/Inspector";
import { Requests } from "@/components/inspector/Requests";
import inspectorClasses from "@/components/inspector/Inspector.module.css";
import { linesOf, SymbolList } from "@/components/inspector/Symbols";
import type { TopologyChildrenResponse } from "@/lib/api/generated";
import { useChildren, useReferences, useSource } from "@/lib/api/topology";
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
import { symbolIndex, type SymbolIndex, type SymbolTarget } from "@/lib/model/symbols";

import { LINE_SELECT, openAt, type Navigation } from "./navigation";
import { Trail, useClimb } from "./Trail";
import classes from "./Views.module.css";

function dirname(path: string): string {
  const cut = path.lastIndexOf("/");
  return cut > 0 ? path.slice(0, cut) : "";
}

/** A selection naming a line to show: `line:<n>`. */
const LINE = LINE_SELECT;

type Mode = "code" | "links";

function ModeSwitch({ mode, onMode }: { mode: Mode; onMode: (mode: Mode) => void }) {
  return (
    <SegmentedControl
      size="xs"
      value={mode}
      onChange={(value) => onMode(value as Mode)}
      aria-label="Show"
      data={[
        { label: "Code", value: "code" },
        { label: "Links", value: "links" },
      ]}
      className={classes.modeSwitch}
    />
  );
}

/**
 * A file. Its source first, every name the index resolved a link: a press
 * selects what it stands for, a second press (or Trace) follows the chain of
 * what a change to it reaches. Links shows the file at the centre of its
 * neighbourhood instead. The inspector beside either holds the detail.
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
  const [mode, setMode] = useState<Mode>("code");

  // the file's own facts: its language and key from the directory listing that
  // holds it (usually already read on the way here), its size from the source
  const parent = useChildren(repository.slug, dirname(path));
  const entry = parent.data?.entries?.find((e) => e.path === path) ?? null;
  const source = useSource(repository.slug, path, true);

  // -- the source, and what its names stand for -----------------------------------------
  const references = useReferences(repository.slug, path);
  const index = useMemo<SymbolIndex>(
    () =>
      symbolIndex(
        references.data?.declarations ?? (data.entries ?? []).filter((e) => e.kind === "declaration"),
        references.data?.references ?? [],
        { repository: repository.slug, path },
      ),
    [references.data, data.entries, repository.slug, path],
  );
  const chosen = nav.selected;
  const symbol = chosen !== null && index.targets.has(chosen) ? index.targets.get(chosen)! : null;
  const [jump, setJump] = useState<{ subject: string | null; line: number } | null>(null);
  const shownLine =
    chosen?.startsWith(LINE) ? Number(chosen.slice(LINE.length)) || null : jump && jump.subject === chosen ? jump.line : null;

  const trace = useCallback(
    (key: string) => nav.go({ repository: repository.slug, path, trace: key }, { via: key }),
    [nav, repository.slug, path],
  );
  const pick = useCallback(
    (key: string) => {
      if (nav.isSelected(key)) trace(key);
      else nav.select(key);
    },
    [nav, trace],
  );
  const goTo = useCallback(
    (target: SymbolTarget) => {
      if (!target.repository || !target.path) return;
      nav.go(
        { repository: target.repository, path: target.path },
        { select: target.line !== null ? `${LINE}${target.line}` : null },
      );
    },
    [nav],
  );

  // -- the neighbourhood, for Links ----------------------------------------------------
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
  const ids = useMemo(() => new Set(layout.keys()), [layout]);
  const selected = chosen !== null && ids.has(chosen) && chosen !== FILE_ID ? chosen : null;
  const lines = source.data?.file.line_count ?? null;

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
  const climb = useClimb(repository, nav, true);

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
            climb,
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
  }, [graph, layout, path, repository, entry, lines, actions, hues, climb]);

  const edges = useMemo(() => flowEdges(graph.edges, { route: () => "side", labelled: true }), [graph]);
  const families = useMemo<Family[]>(() => {
    const present = new Set(graph.edges.map((e) => e.link.family));
    return FAMILIES.filter((f) => present.has(f));
  }, [graph]);

  // the connection being explained: one chosen in the list, else the strongest
  // with the selected neighbour
  const [view, setView] = useState<{ picked: string | null; tab: string }>({ picked: null, tab: "" });
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

  const declarations = (data.entries ?? []).filter((e) => e.kind === "declaration");
  const notes: string[] = [];
  if (graph.within > 0) notes.push(`${plural(graph.within, "link group")} between parts of this file ${graph.within === 1 ? "is" : "are"} not drawn.`);
  if (data.truncated) notes.push(`Showing the strongest ${formatCount(data.links?.length ?? 0)} of ${formatCount(data.links_total ?? 0)} link groups.`);
  if (data.complete === false) notes.push("Some links were not read: this file has more than can be counted at once.");

  // -- the inspector -------------------------------------------------------------------
  const details = (
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
      <h3 className={inspectorClasses.subhead}>
        Declarations <span className={inspectorClasses.sectionCount}>{formatCount(declarations.length)}</span>
      </h3>
      <EntryList
        entries={declarations}
        total={data.entries_total}
        onLine={(line) => {
          setMode("code");
          nav.select(`${LINE}${line}`);
        }}
      />
    </>
  );
  const symbols = (
    <SymbolList
      index={index}
      repository={repository.slug}
      selected={symbol?.key ?? null}
      onPick={(key) => {
        setMode("code");
        nav.select(key);
      }}
      onTrace={trace}
    />
  );

  const requests = (
    <Requests
      scope={{ repository: repository.slug, path }}
      subject={`file:${repository.slug}:${path}`}
      onOpenPlace={(at) => openAt(nav, at)}
    />
  );

  let inspector;
  if (mode === "code" && symbol) {
    const occurs = linesOf(index, symbol.key);
    const elsewhere = !symbol.here && symbol.repository !== null && symbol.path !== null;
    const tabs: InspectorTab[] = [
      {
        value: "symbol",
        label: "Symbol",
        panel: (
          <div className={inspectorClasses.sections}>
            <dl className={inspectorClasses.details}>
              <dt>Kind</dt>
              <dd>{typeNoun(symbol.type)}</dd>
              <dt>Declared</dt>
              <dd className={inspectorClasses.mono}>
                {symbol.here
                  ? `here${symbol.line !== null ? `, line ${symbol.line}` : ""}`
                  : symbol.path
                    ? `${symbol.repository ?? ""}${symbol.repository ? "/" : ""}${symbol.path}${symbol.line !== null ? `:${symbol.line}` : ""}`
                    : "not in a file: shared across the index"}
              </dd>
            </dl>
            {occurs.length > 0 && (
              <section className={inspectorClasses.section}>
                <h3 className={inspectorClasses.sectionTitle}>
                  Used in this file <span className={inspectorClasses.sectionCount}>{occurs.length}</span>
                </h3>
                <div className={classes.lineChips}>
                  {occurs.map((line) => (
                    <button key={line} type="button" className={classes.lineChip} onClick={() => setJump({ subject: chosen, line })}>
                      L{line}
                    </button>
                  ))}
                </div>
              </section>
            )}
            <Quiet>Trace follows everything a change to {symbol.name} reaches, file by file and line by line, into other repositories where the index links them.</Quiet>
          </div>
        ),
      },
      { value: "symbols", label: "Symbols", count: index.targets.size, panel: symbols },
      { value: "details", label: "Details", panel: details },
    ];
    inspector = (
      <Inspector
        label={`${symbol.name}, inspected`}
        icon={<EntityBadge entityType={symbol.type} size={18} />}
        title={symbol.name}
        subtitle={`${typeNoun(symbol.type)} · ${symbol.here ? graph.anchor.name : (symbol.repository ?? "shared")}`}
        onClose={() => nav.select(null)}
        actions={
          <div className={classes.actionRow}>
            <Button size="xs" color="atlas.7" leftSection={<FlowArrowIcon size={14} />} onClick={() => trace(symbol.key)}>
              Trace {symbol.name}
            </Button>
            {elsewhere && (
              <Button size="xs" variant="default" leftSection={<ArrowSquareOutIcon size={14} />} onClick={() => goTo(symbol)}>
                Go to declaration
              </Button>
            )}
          </div>
        }
        tab={view.tab || "symbol"}
        onTab={(value) => setView((v) => ({ ...v, tab: value }))}
        tabs={tabs}
      />
    );
  } else if (mode === "code") {
    inspector = (
      <Inspector
        label={`${graph.anchor.name}, inspected`}
        icon={<PathBadge path={path} file size={18} />}
        title={graph.anchor.name}
        subtitle={`${repository.name} / ${dirname(path) || "root"}`}
        actions={
          entry?.key ? (
            <Button size="xs" variant="default" leftSection={<FlowArrowIcon size={14} />} onClick={() => nav.go({ repository: repository.slug, path, trace: entry.key! })}>
              Trace this file
            </Button>
          ) : undefined
        }
        tab={view.tab === "details" || view.tab === "requests" ? view.tab : "symbols"}
        onTab={(value) => setView((v) => ({ ...v, tab: value }))}
        tabs={[
          { value: "symbols", label: "Symbols", count: index.targets.size, panel: symbols },
          { value: "requests", label: "Requests", panel: requests },
          { value: "details", label: "Details", panel: details },
        ]}
      />
    );
  } else {
    inspector = (
      <Inspector
        label={`${graph.anchor.name}, inspected`}
        icon={<PathBadge path={path} file size={18} />}
        title={graph.anchor.name}
        subtitle={`${repository.name} / ${dirname(path) || "root"}`}
        tab={view.tab || "connections"}
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
          { value: "requests", label: "Requests", panel: requests },
          { value: "details", label: "Details", panel: details },
          {
            value: "evidence",
            label: "Evidence",
            panel: (
              <Evidence
                connection={picked}
                subject={`file:${repository.slug}:${path}`}
                here={{ repository: repository.slug, path }}
                onOpenPlace={(at) => openAt(nav, at)}
                onShowInCode={(span) => {
                  setMode("code");
                  nav.select(`${LINE}${span.start}`);
                }}
              />
            ),
          },
        ]}
      />
    );
  }

  const unavailable = references.isError;
  return (
    <div className={classes.level}>
      <div className={classes.stage} data-inspector>
        <div className={classes.canvas}>
          {mode === "code" ? (
            <div className={classes.reading}>
              <Toolbar
                start={<Trail repository={repository} nav={nav} file />}
                end={
                  <>
                    <div className={classes.levelInfo}>
                      {entry?.language && <span>{entry.language}</span>}
                      {lines !== null && <span>{plural(lines, "line")}</span>}
                      {!references.isPending && !unavailable && <span>{plural(index.targets.size, "name")} to follow</span>}
                    </div>
                    <ModeSwitch mode={mode} onMode={setMode} />
                  </>
                }
              />
              {unavailable && (
                <Notice role="status">
                  Names here cannot be followed yet: the RepoAtlas server answering this app does not know
                  topology/references. Restart <code>repoatlas serve</code> to update it.
                </Notice>
              )}
              <CodeReader
                repository={repository.slug}
                path={path}
                index={index}
                selected={symbol?.key ?? null}
                line={shownLine}
                onPick={pick}
                arrival={nav.arrival.direction}
              />
            </div>
          ) : (
            <Graph
              nodes={nodes}
              edges={edges}
              level={`${repository.slug}:${path}`}
              label={`${graph.anchor.name}: what links to it and what it links to`}
              selected={selected}
              selectedEdge={picked?.edge ?? null}
              onSelect={nav.select}
              onActivate={activate}
              arrival={{ direction: "none", from: null }}
              extent={extent}
              refit={`${expanded.left}:${expanded.right}`}
              families={families.length > 0 ? families : ["code"]}
              start={<Trail repository={repository} nav={nav} file />}
              end={
                <>
                  <div className={classes.levelInfo}>
                    <span>{plural(graph.left.length + graph.right.length + graph.more.reduce((s, m) => s + m.nodes, 0), "neighbour")}</span>
                  </div>
                  <ModeSwitch mode={mode} onMode={setMode} />
                </>
              }
              notice={notes.length > 0 && <p>{notes.join(" ")}</p>}
            />
          )}
        </div>
        {inspector}
      </div>
    </div>
  );
}
