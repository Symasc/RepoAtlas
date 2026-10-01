"use client";

import {
  BaseEdge,
  EdgeLabelRenderer,
  useInternalNode,
  type Edge,
  type EdgeProps,
  type EdgeTypes,
  type InternalNode,
} from "@xyflow/react";
import { memo } from "react";

import { arrowhead, floating, sideways, type Route } from "@/lib/graph/geometry";
import { strokeFor } from "@/lib/graph/layout";
import { compactCount, formatCount } from "@/lib/model/format";
import type { Box, Family, LinkData } from "@/lib/model/graph";

import classes from "./Graph.module.css";

/**
 * How a line stands at the moment: at rest, lit (it touches what is pointed at
 * or selected), dim (something else is), or the selected connection itself.
 */
export type Emphasis = "rest" | "lit" | "dim" | "selected";

/** `float`: border to border; `side`: side to side; `lane`: side to side, met by a frame as a lane. */
export type RouteKind = "float" | "side" | "lane";

export type LinkEdgeData = {
  link: LinkData;
  emphasis: Emphasis;
  /** Show the count even at rest: a small neighbourhood can afford it. */
  labelled: boolean;
  /** Pixels across the line, to separate families drawn between the same pair. */
  offset: number;
  route: RouteKind;
  /** For `float`: how far the line bows, as a fraction of its length. */
  curve: number;
  scope: "detail" | "aggregate" | null;
};

export type GraphEdge = Edge<LinkEdgeData, "link">;

export const FAMILY_NOUN: Record<Family, string> = {
  code: "code",
  http: "HTTP",
  dependency: "package dependency",
  config: "configuration",
};

export function describeLink(link: LinkData): string {
  const types = link.types.map((t) => `${t.type.toLowerCase().replace(/_/g, " ")} ${formatCount(t.count)}`);
  const packages = link.packages.length > 0 ? `; packages: ${link.packages.slice(0, 5).join(", ")}` : "";
  return `${formatCount(link.count)} ${FAMILY_NOUN[link.family]} ${link.count === 1 ? "relationship" : "relationships"}${types.length ? ` (${types.join(", ")})` : ""}${packages}`;
}

function boxOf(node: InternalNode): Box {
  return {
    x: node.internals.positionAbsolute.x,
    y: node.internals.positionAbsolute.y,
    width: node.width ?? node.measured.width ?? 0,
    height: node.height ?? node.measured.height ?? 0,
  };
}

/**
 * One family of relationship, in its own stroke: code solid, HTTP dashed, a
 * package dependency double, configuration dotted. Width grows with the count,
 * logarithmically; the arrow says which way it runs.
 */
function LinkEdge({ id, source, target, data }: EdgeProps<GraphEdge>) {
  const from = useInternalNode(source);
  const to = useInternalNode(target);
  if (!from || !to || !data) return null;
  const { link, emphasis, labelled, offset } = data;
  const s = boxOf(from);
  const t = boxOf(to);
  let route: Route;
  if (data.route === "float") {
    route = floating(s, t, offset, data.curve);
  } else {
    const lane = data.route === "lane" ? (from.type === "frame" ? "source" : to.type === "frame" ? "target" : null) : null;
    route = sideways(s, t, offset, lane);
  }
  const width = strokeFor(link.count) + (emphasis === "selected" ? 1 : 0);
  const head = arrowhead(route.end, route.towards, 5 + width * 1.6);
  return (
    <>
      <g className={classes.link} data-family={link.family} data-kind={link.kind} data-emphasis={emphasis}>
        <title>{describeLink(link)}</title>
        {emphasis === "selected" && <path d={route.path} className={classes.halo} style={{ strokeWidth: width + 9 }} />}
        {link.family === "dependency" ? (
          <>
            <BaseEdge id={id} path={route.path} className={classes.linkOuter} style={{ strokeWidth: width + 3 }} />
            <BaseEdge
              path={route.path}
              className={classes.linkInner}
              style={{ strokeWidth: Math.max(width - 0.75, 1) }}
              interactionWidth={0}
            />
          </>
        ) : (
          <BaseEdge id={id} path={route.path} className={classes.linkStroke} style={{ strokeWidth: width }} />
        )}
        <path d={head} className={classes.arrow} />
      </g>
      {(labelled || emphasis === "lit" || emphasis === "selected") && emphasis !== "dim" && (
        <EdgeLabelRenderer>
          <span
            className={classes.edgeLabel}
            data-family={link.family}
            data-emphasis={emphasis}
            style={{ transform: `translate(-50%, -50%) translate(${route.label.x}px, ${route.label.y}px)` }}
          >
            {link.label ? (link.count > 1 ? `${link.label} · ${compactCount(link.count)}` : link.label) : compactCount(link.count)}
          </span>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

export const edgeTypes: EdgeTypes = {
  link: memo(LinkEdge),
};
