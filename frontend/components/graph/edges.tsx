"use client";

import {
  BaseEdge,
  EdgeLabelRenderer,
  getBezierPath,
  getSmoothStepPath,
  Position,
  type Edge,
  type EdgeProps,
  type EdgeTypes,
} from "@xyflow/react";
import { memo } from "react";

import { formatCount } from "@/lib/model/format";
import type { LinkData } from "@/lib/model/graph";
import { strokeFor } from "@/lib/graph/layout";

import classes from "./Graph.module.css";

/** Hover state decides emphasis: lit when touching the hovered node, dim when not. */
export type Emphasis = "rest" | "lit" | "dim";

export type LinkEdgeData = {
  link: LinkData;
  emphasis: Emphasis;
  labelled: boolean;
  /** Pixels across the line's direction, to separate parallel kinds. */
  offset: number;
  /** See ViewEdge.scope: a card's own line, or a level's total. */
  scope: "detail" | "aggregate" | null;
};
export type TreeEdgeData = { emphasis: Emphasis };

export type GraphEdge = Edge<LinkEdgeData, "link"> | Edge<TreeEdgeData, "tree">;

export const KIND_NOUN: Record<LinkData["kind"], string> = {
  observed: "code",
  http: "HTTP",
  dependency: "dependency",
};

function describe(link: LinkData): string {
  const types = link.types.map((t) => `${t.type.toLowerCase().replace(/_/g, " ")} ${formatCount(t.count)}`);
  const packages = link.packages.length > 0 ? `; packages: ${link.packages.slice(0, 5).join(", ")}` : "";
  return `${formatCount(link.count)} ${KIND_NOUN[link.kind]} relationships${types.length ? ` (${types.join(", ")})` : ""}${packages}`;
}

/**
 * One relationship kind, drawn in its own stroke: code solid, HTTP dashed,
 * package dependency as a double line. Width grows with the count, logarithmically.
 */
function LinkEdge(props: EdgeProps<Edge<LinkEdgeData, "link">>) {
  const { id, data, sourcePosition, targetPosition } = props;
  const offset = data?.offset ?? 0;
  const across = (position: Position) => position === Position.Left || position === Position.Right;
  const sourceX = props.sourceX + (across(sourcePosition) ? 0 : offset);
  const sourceY = props.sourceY + (across(sourcePosition) ? offset : 0);
  const targetX = props.targetX + (across(targetPosition) ? 0 : offset);
  const targetY = props.targetY + (across(targetPosition) ? offset : 0);
  const [path, labelX, labelY] = getBezierPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition });
  if (!data) return null;
  const { link, emphasis, labelled } = data;
  const width = strokeFor(link.count);
  return (
    <>
      <g className={classes.link} data-kind={link.kind} data-emphasis={emphasis}>
        <title>{describe(link)}</title>
        {link.kind === "dependency" ? (
          <>
            <BaseEdge id={id} path={path} className={classes.linkOuter} style={{ strokeWidth: width + 3 }} />
            <BaseEdge
              path={path}
              className={classes.linkInner}
              style={{ strokeWidth: Math.max(width - 0.5, 1) }}
              interactionWidth={0}
            />
          </>
        ) : (
          <BaseEdge id={id} path={path} className={classes.linkStroke} style={{ strokeWidth: width }} />
        )}
      </g>
      {(labelled || emphasis === "lit") && (
        <EdgeLabelRenderer>
          <span
            className={classes.edgeLabel}
            data-kind={link.kind}
            style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
          >
            {formatCount(link.count)}
          </span>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

/** Containment: where a child sits, never a relationship. */
function TreeEdge(props: EdgeProps<Edge<TreeEdgeData, "tree">>) {
  const { id, data, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition } = props;
  const [path] = getSmoothStepPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
    borderRadius: 10,
  });
  return (
    <g className={classes.tree} data-emphasis={data?.emphasis ?? "rest"}>
      <BaseEdge id={id} path={path} className={classes.treeStroke} />
      <circle cx={targetX} cy={targetY} r={2.5} className={classes.treeDot} />
    </g>
  );
}

export const edgeTypes: EdgeTypes = {
  link: memo(LinkEdge),
  tree: memo(TreeEdge),
};
