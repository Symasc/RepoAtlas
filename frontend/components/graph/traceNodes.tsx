"use client";

import { GlobeSimpleIcon, PackageIcon } from "@phosphor-icons/react";
import type { Node, NodeProps, NodeTypes } from "@xyflow/react";
import { memo } from "react";

import { TRACE_ROWS } from "@/lib/graph/layout";
import { formatCount } from "@/lib/model/format";
import { familyOfRelation } from "@/lib/model/graph";
import { relationOfRow, SEED_ID, type TraceCard, type TraceSeedView } from "@/lib/model/trace";

import { RepoSwatch, territory } from "@/components/common/RepoSwatch";

import { EntityBadge, LineGlyph, PathBadge } from "./glyphs";
import { Anchors, stop, type NodeActions } from "./nodes";
import classes from "./Trace.module.css";

/**
 * A trace's nodes: what changes, the files each step lands in with the line it
 * lands on, the repositories they stand in, and the step each column is.
 */

export interface TraceActions extends NodeActions {
  /** Chooses one step: a row in a card. */
  pick: (row: string) => void;
}

export interface SnippetLine {
  number: number;
  text: string;
}

export type TraceSeedData = {
  seed: TraceSeedView;
  hue: number | null;
  snippet: SnippetLine[] | null;
  actions: TraceActions;
  selected?: boolean;
};
export type TraceCardData = {
  card: TraceCard;
  hue: number | null;
  /** The row chosen in this card, if one is. */
  picked: string | null;
  /** The repository the trace was asked from: others are marked as elsewhere. */
  home: string | null;
  actions: TraceActions;
  selected?: boolean;
};
export type TraceFrameData = { repository: string; hue: number | null; home: boolean };
export type TraceLabelData = { text: string; detail?: string };

export type TraceNode =
  | Node<TraceSeedData, "traceSeed">
  | Node<TraceCardData, "traceCard">
  | Node<TraceFrameData, "traceFrame">
  | Node<TraceLabelData, "traceLabel">;

const TYPE_NOUN: Record<string, string> = {
  function: "Function",
  method: "Method",
  class: "Class",
  symbol: "Symbol",
  module: "Module",
  package: "Package",
  file: "File",
  env_var: "Environment variable",
  config: "Configuration key",
  http_route: "HTTP route",
  http_endpoint: "HTTP endpoint",
  http_deployment: "Deployment",
};

export function typeNoun(type: string | null | undefined): string {
  return TYPE_NOUN[type ?? ""] ?? (type ? type.replace(/_/g, " ") : "Directory");
}

function TraceSeedNode({ data }: NodeProps<Node<TraceSeedData, "traceSeed">>) {
  const { seed, snippet, actions, hue, selected = false } = data;
  const where = seed.path ? `${seed.path}${seed.lines ? `:${seed.lines[0]}` : ""}` : null;
  return (
    <div className={classes.seed} style={territory(hue)} data-selected={selected || undefined} data-id={SEED_ID}>
      <Anchors />
      <button
        type="button"
        className={`${classes.cover} nodrag`}
        onClick={stop(() => actions.activate(SEED_ID))}
        aria-label={`${seed.name}, what changes`}
      />
      <span className={classes.kicker}>What changes</span>
      <span className={classes.seedHead}>
        <EntityBadge entityType={seed.type ?? "file"} size={20} />
        <span className={classes.seedText}>
          <span className={classes.seedName}>{seed.name}</span>
          <span className={classes.meta}>
            {typeNoun(seed.type)}
            {seed.repository ? ` · ${seed.repository}` : ""}
          </span>
        </span>
      </span>
      {where && <span className={classes.where}>{where}</span>}
      {snippet && snippet.length > 0 && (
        <pre className={classes.snippet} aria-hidden>
          {snippet.map((line) => (
            <span key={line.number} className={classes.snippetLine}>
              <span className={classes.snippetNumber}>{line.number}</span>
              {line.text || " "}
            </span>
          ))}
        </pre>
      )}
    </div>
  );
}

function TraceCardNode({ id, data }: NodeProps<Node<TraceCardData, "traceCard">>) {
  const { card, picked, home, actions, selected = false } = data;
  const shown = card.rows.slice(0, TRACE_ROWS);
  const more = card.rows.length - shown.length;
  const dir = card.path && card.path.includes("/") ? card.path.slice(0, card.path.lastIndexOf("/")) : null;
  const elsewhere = card.repository !== null && home !== null && card.repository !== home;
  return (
    <div
      className={classes.card}
      style={territory(data.hue)}
      data-selected={selected || undefined}
      data-test={card.test || undefined}
      data-elsewhere={elsewhere || undefined}
      data-id={id}
    >
      <Anchors />
      <button
        type="button"
        className={`${classes.cardHeader} nodrag`}
        onClick={stop(() => actions.activate(id))}
        aria-label={`Select ${card.name}${card.repository ? ` in ${card.repository}` : ""}${dir ? `/${dir}` : ""}`}
      >
        {card.path ? <PathBadge path={card.path} file /> : <EntityBadge entityType={card.rows[0]?.candidate.type} />}
        <span className={classes.cardText}>
          <span className={classes.cardName}>{card.name}</span>
          <span className={classes.meta}>{dir ?? (card.repository ? "repository root" : "shared across the index")}</span>
        </span>
        {card.test && <span className={classes.tag}>Test</span>}
      </button>
      <ul className={classes.rows}>
        {shown.map((row) => (
          <li key={row.id}>
            <button
              type="button"
              className={`${classes.row} nodrag`}
              data-picked={row.id === picked || undefined}
              onClick={stop(() => actions.pick(row.id))}
              aria-label={`${row.candidate.name}${row.line !== null ? `, line ${row.line}` : ""}, ${row.reading}`}
              aria-pressed={row.id === picked}
            >
              <EntityBadge entityType={row.candidate.type} size={14} />
              <span className={classes.rowName}>{row.candidate.name}</span>
              {row.crossing === "package" && <PackageIcon size={13} className={classes.crossing} aria-hidden />}
              {row.crossing === "http" && <GlobeSimpleIcon size={13} className={classes.crossing} aria-hidden />}
              <span className={classes.reading}>
                <LineGlyph family={familyOfRelation(relationOfRow(row))} relation={relationOfRow(row)} width={12} />
                {row.reading}
              </span>
              {row.line !== null && <span className={classes.lineNo}>L{row.line}</span>}
            </button>
          </li>
        ))}
        {more > 0 && (
          <li>
            <button type="button" className={`${classes.more} nodrag`} onClick={stop(() => actions.activate(id))}>
              +{formatCount(more)} more here
            </button>
          </li>
        )}
      </ul>
    </div>
  );
}

function TraceFrameNode({ data }: NodeProps<Node<TraceFrameData, "traceFrame">>) {
  return (
    <div className={classes.frame} style={territory(data.hue)} data-home={data.home || undefined}>
      <span className={classes.frameHead}>
        <RepoSwatch hue={data.hue} />
        <span className={classes.frameName}>{data.repository}</span>
        {!data.home && <span className={classes.frameNote}>another repository</span>}
      </span>
    </div>
  );
}

function TraceLabelNode({ data }: NodeProps<Node<TraceLabelData, "traceLabel">>) {
  return (
    <div className={classes.label}>
      <span>{data.text}</span>
      {data.detail && <span className={classes.labelDetail}>{data.detail}</span>}
    </div>
  );
}

export const traceNodeTypes: NodeTypes = {
  traceSeed: memo(TraceSeedNode),
  traceCard: memo(TraceCardNode),
  traceFrame: memo(TraceFrameNode),
  traceLabel: memo(TraceLabelNode),
};
