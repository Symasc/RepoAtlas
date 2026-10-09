"use client";

import { Button, Loader } from "@mantine/core";
import { ArrowSquareOutIcon, FlowArrowIcon } from "@phosphor-icons/react";
import { useCallback, useMemo, useState, type ReactNode } from "react";

import { Snippet } from "@/components/code/Snippet";
import { Graph, Notice, Toolbar } from "@/components/graph/Graph";
import { flowEdges, place } from "@/components/graph/flow";
import { EntityBadge, PathBadge } from "@/components/graph/glyphs";
import type { GraphNode } from "@/components/graph/nodes";
import { typeNoun, type TraceActions } from "@/components/graph/traceNodes";
import { Inspector, Quiet, type InspectorTab } from "@/components/inspector/Inspector";
import inspectorClasses from "@/components/inspector/Inspector.module.css";
import type { ImpactCandidate, ImpactResponse } from "@/lib/api/generated";
import { useImpact, useLines } from "@/lib/api/topology";
import { bounds, columnLabelId, layoutTrace, traceFrameId } from "@/lib/graph/layout";
import { formatCount, plural } from "@/lib/model/format";
import { FAMILIES, type Family } from "@/lib/model/graph";
import { DIRECTORY_TRACE, traceSeed } from "@/lib/model/location";
import type { RepositoryModel } from "@/lib/model/repositories";
import {
  boundaryLabel,
  buildTrace,
  chainOf,
  decisionsOf,
  nameFromKey,
  reachOf,
  relationOfRow,
  SEED_ID,
  splitAt,
  stepEvidence,
  traceEdgeId,
  unitLabel,
  type TraceGraph,
  type TraceRow,
} from "@/lib/model/trace";

import type { Navigation } from "./navigation";
import { Trail } from "./Trail";
import classes from "./Views.module.css";

const LINE = "line:";

function stepsAway(distance: number): string {
  return distance === 1 ? "Directly" : `${distance} steps away`;
}

/**
 * A trace: what a change to one thing reaches, and by which indexed steps. It
 * opens close on what changes, then pulls back to the whole chain: each column
 * one step further, each card a file the chain lands in, each row the thing
 * there and the line where the step lands, framed by repository when the chain
 * leaves the one it started in. Choosing a row reads that step's lines; a second
 * press opens the file there.
 */
export function TraceLevel({
  repository,
  repositories,
  nav,
}: {
  repository: RepositoryModel | null;
  repositories: RepositoryModel[];
  nav: Navigation;
}) {
  const seed = traceSeed(nav.place);
  const impact = useImpact(seed);
  const resolved = impact.data?.seed.status === "resolved";
  const graph = useMemo(() => (impact.data && resolved ? buildTrace(impact.data) : null), [impact.data, resolved]);
  const subject = `trace:${seed ?? ""}`;
  const hues = useMemo(() => new Map(repositories.map((r) => [r.slug, r.hue])), [repositories]);
  const slugs = useMemo(() => repositories.map((r) => r.slug), [repositories]);
  const home = graph?.seed.repository ?? nav.place.repository;
  const origin = nav.arrival.previous;

  // -- what is chosen: a row, a card, or what changes --------------------------------
  const chosen = nav.selected;
  const row = graph && chosen ? (graph.rows.get(chosen) ?? null) : null;
  const card = graph ? (row ? graph.cardOf.get(row.id)! : chosen && graph.columns.some((c) => c.cards.some((k) => k.id === chosen)) ? chosen : null) : null;
  const selectedNode = row ? card : card ?? (chosen === SEED_ID ? SEED_ID : null);

  const openAt = useCallback(
    (target: { repository: string | null; path: string | null }, line: number | null) => {
      if (!target.repository || !target.path) return;
      nav.go({ repository: target.repository, path: target.path }, { select: line !== null ? `${LINE}${line}` : null });
    },
    [nav],
  );
  const traceFrom = useCallback(
    (candidate: ImpactCandidate) =>
      nav.go({ repository: candidate.repository ?? nav.place.repository, path: candidate.path ?? nav.place.path, trace: candidate.entity }, { via: null }),
    [nav],
  );
  const pick = useCallback(
    (id: string) => {
      const picked = graph?.rows.get(id);
      if (!picked) return;
      if (nav.isSelected(id)) openAt(stepFile(picked, slugs), picked.line);
      else nav.select(id);
    },
    [graph, nav, openAt, slugs],
  );
  const activate = useCallback(
    (id: string) => {
      if (nav.isSelected(id)) {
        const target = graph?.columns.flatMap((c) => c.cards).find((c) => c.id === id);
        if (target) openAt(target, target.rows[0]?.line ?? null);
        else if (id === SEED_ID && graph) openAt(graph.seed, graph.seed.lines?.[0] ?? null);
        return;
      }
      nav.select(id);
    },
    [graph, nav, openAt],
  );
  const actions = useMemo<TraceActions>(
    () => ({ activate, pick, go: (to) => nav.go({ repository: to.repository, path: to.path }) }),
    [activate, pick, nav],
  );

  // the seed's own first lines, on its card
  const seedLines = graph?.seed.lines ?? null;
  const seedRead = useLines(
    graph?.seed.repository ?? null,
    graph?.seed.path ?? null,
    seedLines ? { start: seedLines[0], end: Math.min(seedLines[1], seedLines[0] + 4) } : null,
    subject,
    0,
  );
  const snippet = useMemo(
    () =>
      (seedRead.data?.segments ?? [])
        .filter((s) => !s.declaration && s.ref.line_start != null)
        .flatMap((s) =>
          (s.text ?? "")
            .replace(/\n$/, "")
            .split("\n")
            .map((text, i) => ({ number: s.ref.line_start! + i, text })),
        )
        .slice(0, 5),
    [seedRead.data],
  );

  const layout = useMemo(() => (graph ? layoutTrace(graph) : null), [graph]);
  const extent = useMemo(() => (layout ? bounds(layout.values()) : null), [layout]);
  const nodes = useMemo<GraphNode[]>(() => {
    if (!graph || !layout) return [];
    const at = (id: string) => layout.get(id)!;
    const out: GraphNode[] = [];
    const framed = graph.repositories.length > 1;
    if (framed) {
      for (const column of graph.columns) {
        for (const repo of new Set(column.cards.map((c) => c.repository ?? ""))) {
          const id = traceFrameId(column.distance, repo);
          if (!layout.has(id)) continue;
          out.push(place({ id, type: "traceFrame", data: { repository: repo || "Shared", hue: hues.get(repo) ?? null, home: repo === home }, zIndex: -1 }, at(id)));
        }
      }
    }
    for (const column of graph.columns) {
      const files = column.cards.length;
      out.push(
        place(
          { id: columnLabelId(column.distance), type: "traceLabel", data: { text: stepsAway(column.distance), detail: plural(files, "place") } },
          at(columnLabelId(column.distance)),
        ),
      );
    }
    out.push(place({ id: SEED_ID, type: "traceSeed", data: { seed: graph.seed, hue: hues.get(graph.seed.repository ?? "") ?? null, snippet: snippet.length ? snippet : null, actions } }, at(SEED_ID)));
    for (const column of graph.columns) {
      for (const c of column.cards) {
        out.push(
          place(
            {
              id: c.id,
              type: "traceCard",
              data: { card: c, hue: hues.get(c.repository ?? "") ?? null, picked: row && graph.cardOf.get(row.id) === c.id ? row.id : null, home, actions },
            },
            at(c.id),
          ),
        );
      }
    }
    return out;
  }, [graph, layout, hues, home, snippet, actions, row]);

  const edges = useMemo(() => (graph ? flowEdges(graph.edges, { route: () => "side", labelled: true }) : []), [graph]);
  const families = useMemo<Family[]>(() => {
    const present = new Set(graph?.edges.map((e) => e.link.family) ?? []);
    const list = FAMILIES.filter((f) => present.has(f));
    return list.length > 0 ? list : ["code"];
  }, [graph]);
  const selectedEdge = useMemo(() => {
    if (!graph || !row) return null;
    const before = row.candidate.from === 0 ? null : graph.rows.get(`c:${row.candidate.from}`);
    const from = before ? graph.cardOf.get(before.id) : SEED_ID;
    const id = from ? traceEdgeId(from, graph.cardOf.get(row.id)!, relationOfRow(row)) : null;
    return graph.edges.some((e) => e.id === id) ? id : null;
  }, [graph, row]);

  const [tab, setTab] = useState<string>("");

  // -- what the canvas says, whatever state it is in --------------------------------------
  const title = graph?.seed.name ?? (seed ? nameFromKey(seed) : "Trace");
  const start = (
    <>
      {repository && <Trail repository={repository} nav={nav} file={!!nav.place.path && !nav.place.trace?.startsWith(DIRECTORY_TRACE)} />}
      <span className={classes.traceStep}>
        <FlowArrowIcon size={14} aria-hidden /> Trace of {title}
      </span>
    </>
  );
  const notice = graph ? traceNote(impact.data!, graph) : null;

  let canvas;
  if (impact.isPending) {
    canvas = (
      <div className={classes.traceState} aria-busy="true">
        <Loader size="sm" type="dots" />
        <p>Following what a change to {title} reaches</p>
      </div>
    );
  } else if (impact.error) {
    canvas = (
      <div className={classes.traceState} role="alert">
        {impact.error.message}
      </div>
    );
  } else if (impact.data && !resolved) {
    canvas = <Unresolved impact={impact.data} onChoose={(entity) => nav.go({ ...nav.place, trace: entity })} />;
  } else if (graph && graph.listed === 0) {
    canvas = (
      <div className={classes.traceState}>
        <EntityBadge entityType={graph.seed.type ?? "file"} size={24} />
        <p>Nothing outside {graph.seed.name} depends on it: a change to it reaches no other indexed code.</p>
      </div>
    );
  } else {
    canvas = (
      <Graph
        nodes={nodes}
        edges={edges}
        level={`trace:${nav.place.trace}`}
        label={`What a change to ${title} reaches`}
        selected={selectedNode}
        selectedEdge={selectedEdge}
        onSelect={nav.select}
        onActivate={activate}
        arrival={{
          direction: nav.arrival.direction,
          from: nav.arrival.direction === "out" ? (selectedNode ?? null) : null,
          focus: nav.arrival.direction === "out" ? null : SEED_ID,
        }}
        extent={extent}
        // a long chain stays legible: it starts at the seed and pans to the right
        minFit={0.7}
        families={families}
        start={start}
        end={
          graph && (
            <div className={classes.levelInfo}>
              <span>
                {plural(graph.listed, "thing")} in {plural(graph.columns.reduce((s, c) => s + c.cards.length, 0), "place")}
              </span>
            </div>
          )
        }
        notice={notice}
      />
    );
  }

  return (
    <div className={classes.level}>
      <div className={classes.stage} data-inspector>
        <div className={classes.canvas}>
          {!graph || graph.listed === 0 ? (
            <div className={classes.traceFrame}>
              <Toolbar start={start} />
              {notice && <Notice>{notice}</Notice>}
              {canvas}
            </div>
          ) : (
            canvas
          )}
        </div>
        {graph ? (
          <TraceInspector
            graph={graph}
            impact={impact.data!}
            row={row}
            card={card}
            subject={subject}
            slugs={slugs}
            origin={origin}
            tab={tab}
            onTab={setTab}
            onPick={(id) => nav.select(id)}
            onClose={() => nav.select(null)}
            onOpen={openAt}
            onTraceFrom={traceFrom}
          />
        ) : (
          <Inspector
            label={`${title}, inspected`}
            icon={<FlowArrowIcon size={18} />}
            title={title}
            subtitle="Trace"
            tab="about"
            onTab={() => {}}
            tabs={[
              {
                value: "about",
                label: "About",
                panel: <Quiet>A trace follows what a change to one thing reaches, step by step, through every file the index links it to.</Quiet>,
              },
            ]}
          />
        )}
      </div>
    </div>
  );
}

/** Where a step's lines are: the file that asserts it, else the candidate's own. */
function stepFile(row: TraceRow, slugs: readonly string[]): { repository: string | null; path: string | null } {
  return splitAt(row.candidate.step.at, slugs) ?? { repository: row.candidate.repository ?? null, path: row.candidate.path ?? null };
}

/** What the trace leaves out or crosses, said plainly; `null` when there is nothing to say. */
function traceNote(impact: ImpactResponse, graph: TraceGraph): ReactNode {
  const reach = reachOf(impact);
  const notes: string[] = [];
  if (reach.beyond > 0) notes.push(`Showing the nearest ${formatCount(graph.listed)} of ${formatCount(reach.outside)} things a change reaches.`);
  if (impact.summary.complete === false) notes.push("The walk stopped at its limits; there may be more.");
  if (graph.repositories.length > 1) notes.push(`Crosses into ${plural(graph.repositories.length - 1, "other repository", "other repositories")}.`);
  const decided = decisionsOf(impact);
  const refused = decided.shown.length + decided.more;
  if (refused > 0) notes.push(`${plural(refused, "link")} not made where it went; see the summary.`);
  if (notes.length === 0) return null;
  return <p>{notes.join(" ")}</p>;
}

function Unresolved({ impact, onChoose }: { impact: ImpactResponse; onChoose: (entity: string) => void }) {
  const candidates = impact.seed.candidates ?? [];
  return (
    <div className={classes.traceState}>
      <p>
        {impact.seed.status === "ambiguous"
          ? `“${impact.seed.term}” names more than one thing. Which one?`
          : `The index holds nothing called “${impact.seed.term}” to trace${impact.seed.reason ? `: ${impact.seed.reason}` : "."}`}
      </p>
      {candidates.length > 0 && (
        <ul className={inspectorClasses.rows}>
          {candidates.map((c) => (
            <li key={c.entity}>
              <button type="button" className={inspectorClasses.row} onClick={() => onChoose(c.entity)}>
                <EntityBadge entityType={c.type} />
                <span className={inspectorClasses.rowText}>
                  <span className={inspectorClasses.rowLabel}>{nameFromKey(c.entity)}</span>
                  <span className={inspectorClasses.rowDetail}>
                    {c.repository}/{c.path}
                    {Array.isArray(c.lines) ? `:${c.lines[0]}` : ""}
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function TraceInspector({
  graph,
  impact,
  row,
  card,
  subject,
  slugs,
  origin,
  tab,
  onTab,
  onPick,
  onClose,
  onOpen,
  onTraceFrom,
}: {
  graph: TraceGraph;
  impact: ImpactResponse;
  row: TraceRow | null;
  card: string | null;
  subject: string;
  slugs: readonly string[];
  origin: Navigation["arrival"]["previous"];
  tab: string;
  onTab: (tab: string) => void;
  onPick: (id: string) => void;
  onClose: () => void;
  onOpen: (target: { repository: string | null; path: string | null }, line: number | null) => void;
  onTraceFrom: (candidate: ImpactCandidate) => void;
}) {
  const seed = graph.seed;
  if (row) {
    const c = row.candidate;
    const at = stepFile(row, slugs);
    const lines = Array.isArray(c.step.lines) ? (c.step.lines as [number, number]) : null;
    const chain = chainOf(graph, row);
    const crossing = c.step.crossing;
    const http = c.step.http;
    const tabs: InspectorTab[] = [
      {
        value: "step",
        label: "Step",
        panel: (
          <div className={inspectorClasses.sections}>
            <p className={classes.stepSentence}>
              <strong>{c.name}</strong> {row.reading}{" "}
              <strong>{c.from === 0 ? seed.name : (graph.rows.get(`c:${c.from}`)?.candidate.name ?? "it")}</strong>
              {lines ? ` on line ${lines[0]}${lines[1] !== lines[0] ? `–${lines[1]}` : ""}` : ""}
            </p>
            {at.repository && at.path && lines ? (
              <Snippet repository={at.repository} path={at.path} lines={{ start: lines[0], end: lines[1] }} subject={subject} />
            ) : (
              <Quiet>This step was derived rather than read from a line{c.step.derived ? `: ${c.step.derived}` : "."}</Quiet>
            )}
            <dl className={inspectorClasses.details}>
              <dt>File</dt>
              <dd className={inspectorClasses.mono}>
                {at.repository}/{at.path}
              </dd>
              <dt>Kind</dt>
              <dd>{typeNoun(c.type)}</dd>
              {c.unit && (
                <>
                  <dt>Unit</dt>
                  <dd>{unitLabel(c.unit)}</dd>
                </>
              )}
              {(c.boundaries ?? []).length > 0 && (
                <>
                  <dt>Crosses</dt>
                  <dd>{(c.boundaries ?? []).map(boundaryLabel).join(", ")}</dd>
                </>
              )}
            </dl>
            {crossing && (
              <section className={inspectorClasses.section}>
                <h3 className={inspectorClasses.sectionTitle}>Through a package</h3>
                <dl className={inspectorClasses.crossing}>
                  <dt>Package</dt>
                  <dd>{crossing.package}</dd>
                  <dt>From</dt>
                  <dd>{crossing.from_repository}</dd>
                  <dt>Into</dt>
                  <dd>{crossing.to_repository}</dd>
                  <dt>Provided by</dt>
                  <dd>{crossing.provided_by}</dd>
                  <dt>Declared by</dt>
                  <dd>
                    {crossing.declared_by}
                    {crossing.constraint ? ` (${crossing.constraint})` : ""}
                  </dd>
                  {crossing.imported_at && (
                    <>
                      <dt>Imported at</dt>
                      <dd>{crossing.imported_at}</dd>
                    </>
                  )}
                </dl>
              </section>
            )}
            {stepEvidence(c.step).map((section) => (
              <section key={section.title} className={inspectorClasses.section}>
                <h3 className={inspectorClasses.sectionTitle}>{section.title}</h3>
                <dl className={inspectorClasses.crossing}>
                  {section.fields.map(([label, value]) => (
                    <span key={label} style={{ display: "contents" }}>
                      <dt>{label}</dt>
                      <dd>{value}</dd>
                    </span>
                  ))}
                </dl>
              </section>
            ))}
            {http && (
              <section className={inspectorClasses.section}>
                <h3 className={inspectorClasses.sectionTitle}>Over HTTP</h3>
                <dl className={inspectorClasses.crossing}>
                  {Object.entries(http)
                    .filter(([, v]) => typeof v === "string" && v)
                    .map(([k, v]) => (
                      <span key={k} style={{ display: "contents" }}>
                        <dt>{k.replace(/_/g, " ")}</dt>
                        <dd>{String(v)}</dd>
                      </span>
                    ))}
                </dl>
              </section>
            )}
          </div>
        ),
      },
      {
        value: "chain",
        label: "Chain",
        count: chain.length,
        panel: (
          <ol className={classes.chain}>
            <li className={classes.chainStep}>
              <EntityBadge entityType={seed.type ?? "file"} />
              <span>
                <strong>{seed.name}</strong>
                <span className={inspectorClasses.rowDetail}>what changes</span>
              </span>
            </li>
            {chain.map((step) => (
              <li key={step.id} className={classes.chainStep}>
                <EntityBadge entityType={step.candidate.type} />
                <button type="button" className={classes.chainButton} data-current={step.id === row.id || undefined} onClick={() => onPick(step.id)}>
                  <strong>{step.candidate.name}</strong>
                  <span className={inspectorClasses.rowDetail}>
                    {step.reading}
                    {step.line !== null ? ` · line ${step.line}` : ""} · {step.candidate.path ?? step.candidate.repository}
                  </span>
                </button>
              </li>
            ))}
          </ol>
        ),
      },
    ];
    const alt = (c.alt_paths ?? []).length;
    return (
      <Inspector
        label={`${c.name}, inspected`}
        icon={<EntityBadge entityType={c.type} size={18} />}
        title={c.name}
        subtitle={`${typeNoun(c.type)} · ${stepsAway(c.distance).toLowerCase()}${alt ? ` · reached ${plural(alt, "other way")} too` : ""}`}
        onClose={onClose}
        actions={
          <div className={classes.actionRow}>
            <Button size="xs" color="atlas.7" leftSection={<ArrowSquareOutIcon size={14} />} onClick={() => onOpen(at, row.line)} disabled={!at.path}>
              Open {at.path ? at.path.slice(at.path.lastIndexOf("/") + 1) : "file"}
              {row.line !== null ? ` at ${row.line}` : ""}
            </Button>
            <Button size="xs" variant="default" leftSection={<FlowArrowIcon size={14} />} onClick={() => onTraceFrom(c)}>
              Trace from {c.name}
            </Button>
          </div>
        }
        tab={tab === "chain" ? "chain" : "step"}
        onTab={onTab}
        tabs={tabs}
      />
    );
  }

  const shown = card ? graph.columns.flatMap((col) => col.cards).find((k) => k.id === card) : null;
  if (shown) {
    return (
      <Inspector
        label={`${shown.name}, inspected`}
        icon={shown.path ? <PathBadge path={shown.path} file size={18} /> : <EntityBadge entityType={shown.rows[0]?.candidate.type} size={18} />}
        title={shown.name}
        subtitle={`${shown.repository ?? "shared"}${shown.path ? ` / ${shown.path.includes("/") ? shown.path.slice(0, shown.path.lastIndexOf("/")) : "root"}` : ""}`}
        onClose={onClose}
        actions={
          shown.path ? (
            <Button size="xs" color="atlas.7" leftSection={<ArrowSquareOutIcon size={14} />} onClick={() => onOpen(shown, shown.rows[0]?.line ?? null)}>
              Open {shown.name}
            </Button>
          ) : undefined
        }
        tab="steps"
        onTab={onTab}
        tabs={[
          {
            value: "steps",
            label: "Steps here",
            count: shown.rows.length,
            panel: (
              <ul className={inspectorClasses.rows}>
                {shown.rows.map((r) => (
                  <li key={r.id} className={inspectorClasses.rowItem}>
                    <button type="button" className={inspectorClasses.row} onClick={() => onPick(r.id)}>
                      <EntityBadge entityType={r.candidate.type} />
                      <span className={inspectorClasses.rowText}>
                        <span className={inspectorClasses.rowLabel}>{r.candidate.name}</span>
                        <span className={inspectorClasses.rowDetail}>
                          {r.reading}
                          {r.line !== null ? ` · line ${r.line}` : ""}
                        </span>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            ),
          },
        ]}
      />
    );
  }

  // what changes, and the walk as a whole
  const units = Object.entries(impact.omitted?.by_unit ?? {}).sort((a, b) => (b[1] ?? 0) - (a[1] ?? 0));
  const tests = (impact.tests ?? []).length;
  const packages = impact.packages ?? [];
  const hubs = impact.hubs ?? [];
  const unresolved = impact.unresolved ?? [];
  const decisions = decisionsOf(impact);
  const cameFrom = origin?.path ? `${origin.repository}/${origin.path}` : null;
  const reach = reachOf(impact);
  return (
    <Inspector
      label={`${seed.name}, inspected`}
      icon={<EntityBadge entityType={seed.type ?? "file"} size={18} />}
      title={seed.name}
      subtitle={`${typeNoun(seed.type)}${seed.repository ? ` · ${seed.repository}` : ""}`}
      actions={
        seed.path ? (
          <Button size="xs" variant="default" leftSection={<ArrowSquareOutIcon size={14} />} onClick={() => onOpen(seed, seed.lines?.[0] ?? null)}>
            Open where it is declared
          </Button>
        ) : undefined
      }
      tab="summary"
      onTab={onTab}
      tabs={[
        {
          value: "summary",
          label: "Summary",
          panel: (
            <div className={inspectorClasses.sections}>
              <dl className={inspectorClasses.details}>
                <dt>Declared</dt>
                <dd className={inspectorClasses.mono}>
                  {seed.path ? `${seed.repository}/${seed.path}${seed.lines ? `:${seed.lines[0]}` : ""}` : seed.term}
                </dd>
                {seed.unit && (
                  <>
                    <dt>Unit</dt>
                    <dd>{unitLabel(seed.unit)}</dd>
                  </>
                )}
                <dt>Reaches</dt>
                <dd>
                  {reach.outside === 0 ? "Nothing outside it" : plural(reach.outside, "thing")}
                  {impact.summary.units ? ` in ${plural(impact.summary.units, "unit")}` : ""}
                  {impact.summary.complete === false ? ", and possibly more" : ""}
                </dd>
                {reach.within > 0 && (
                  <>
                    <dt>Inside it</dt>
                    <dd>{plural(reach.within, "thing")} the walk starts from</dd>
                  </>
                )}
                <dt>Shown</dt>
                <dd>
                  {formatCount(graph.listed)}
                  {tests ? `, ${plural(tests, "test")} among them` : ""}
                </dd>
                {cameFrom && (
                  <>
                    <dt>Asked from</dt>
                    <dd className={inspectorClasses.mono}>{cameFrom}</dd>
                  </>
                )}
              </dl>
              {units.length > 0 && (
                <section className={inspectorClasses.section}>
                  <h3 className={inspectorClasses.sectionTitle}>Not shown, by unit</h3>
                  <ul className={inspectorClasses.rows}>
                    {units.slice(0, 8).map(([unit, count]) => (
                      <li key={unit} className={classes.summaryRow}>
                        <span>{unitLabel(unit)}</span>
                        <span>{formatCount(count ?? 0)}</span>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
              {packages.length > 0 && (
                <section className={inspectorClasses.section}>
                  <h3 className={inspectorClasses.sectionTitle}>
                    Depend on it through a package <span className={inspectorClasses.sectionCount}>{packages.length}</span>
                  </h3>
                  <ul className={inspectorClasses.rows}>
                    {packages.slice(0, 8).map((p) => (
                      <li key={`${p.dependent}-${p.declared_at}`} className={classes.summaryRow}>
                        <span>
                          {unitLabel(p.dependent)}
                          {p.crosses_repository && <span className={classes.summaryNote}> · another repository</span>}
                        </span>
                        <span className={classes.summaryNote}>
                          {typeof p.exact_consumers === "number" ? `${formatCount(p.exact_consumers)} reached` : p.exact_consumers}
                        </span>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
              {hubs.length > 0 && <Quiet>{plural(hubs.length, "hub")} reached: depended on too widely to list one by one, so counted instead.</Quiet>}
              {unresolved.length > 0 && <Quiet>{plural(unresolved.length, "importer")} could be named only as a file, not as the declaration involved.</Quiet>}
              {decisions.shown.length > 0 && (
                <section className={inspectorClasses.section}>
                  <h3 className={inspectorClasses.sectionTitle}>
                    Links not made where it went <span className={inspectorClasses.sectionCount}>{decisions.shown.length + decisions.more}</span>
                  </h3>
                  <ul className={inspectorClasses.rows}>
                    {decisions.shown.map((d) => (
                      <li key={`${d.family}-${d.subject}`} className={classes.summaryRow}>
                        <span className={inspectorClasses.rowText}>
                          <span className={inspectorClasses.rowLabel}>{d.subject.slice(d.subject.indexOf(":") + 1)}</span>
                          <span className={inspectorClasses.rowDetail}>{d.at}</span>
                        </span>
                        <span className={classes.summaryNote}>{d.decision}</span>
                      </li>
                    ))}
                  </ul>
                  {decisions.more > 0 && <Quiet>{plural(decisions.more, "more decision")} not listed.</Quiet>}
                  <Quiet>A decision says why a link was not made or is qualified. It is not a breakage.</Quiet>
                </section>
              )}
            </div>
          ),
        },
      ]}
    />
  );
}
