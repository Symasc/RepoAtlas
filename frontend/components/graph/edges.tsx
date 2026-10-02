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
import type { Box, Family, LinkData, Relation } from "@/lib/model/graph";

import classes from "./Graph.module.css";

/**
 * How a line stands at the moment: at rest, lit (it touches what is pointed at
 * or selected), dim (something else is), or the selected connection itself.
 */
export type Emphasis = "rest" | "lit" | "dim" | "selected";

/** `float`: border to border; `side`: side to side; `lane`: side to side, met by a frame as a lane. */
export type RouteKind = "float" | "side" | "lane";

export type LinkEdgeData = {
  /** What this line draws: the whole link, or the part of it of one relationship. */
  link: LinkData;
  /** The relationship it is, which picks its colour. */
  relation: Relation;
  /** The link it draws (part of): what is selected, pointed at and explained. */
  base: string;
  /** The whole link, on the one line of it that carries its count; else `null`. */
  badge: LinkData | null;
  emphasis: Emphasis;
  /** Show the count even at rest: a small neighbourhood can afford it. */
  labelled: boolean;
  /** Pixels across the line, to separate families drawn between the same pair. */
  offset: number;
  route: RouteKind;
  scope: "detail" | "aggregate" | null;
};

export type GraphEdge = Edge<LinkEdgeData, "link">;

export const FAMILY_NOUN: Record<Family, string> = {
  code: "code",
  http: "HTTP",
  dependency: "package dependency",
  config: "configuration",
};

/** A relationship, named as a reader would: the legend's words. */
export const RELATION_LABEL: Record<Relation, string> = {
  imports: "Imports",
  calls: "Calls",
  uses: "Uses",
  other: "Other code",
  http: "HTTP",
  dependency: "Package dependency",
  config: "Config & env",
};

const RELATION_NOUN: Record<Relation, string> = {
  imports: "import",
  calls: "call",
  uses: "use",
  other: "code",
  http: "HTTP",
  dependency: "package dependency",
  config: "configuration",
};

export function describeLink(link: LinkData, relation?: Relation): string {
  const types = link.types.map((t) => `${t.type.toLowerCase().replace(/_/g, " ")} ${formatCount(t.count)}`);
  const packages = link.packages.length > 0 ? `; packages: ${link.packages.slice(0, 5).join(", ")}` : "";
  const noun = relation ? RELATION_NOUN[relation] : FAMILY_NOUN[link.family];
  return `${formatCount(link.count)} ${noun} ${link.count === 1 ? "relationship" : "relationships"}${types.length ? ` (${types.join(", ")})` : ""}${packages}`;
}

/** A pattern in proportion to the line's weight: HTTP dashed, configuration dotted. */
const DASH: Partial<Record<Family, (width: number) => string>> = {
  http: (w) => `${w * 2.6} ${w * 1.6}`,
  config: (w) => `0.1 ${w * 2.2}`,
};

function boxOf(node: InternalNode): Box {
  return {
    x: node.internals.positionAbsolute.x,
    y: node.internals.positionAbsolute.y,
    width: node.width ?? node.measured.width ?? 0,
    height: node.height ?? node.measured.height ?? 0,
  };
}

/**
 * One relationship, drawn as a transit map draws a line: its colour is the
 * relationship, its pattern the family (code solid, HTTP dashed, a package
 * dependency double, configuration dotted), its weight one of three by count.
 * A band of canvas beneath it keeps crossings and parallels apart; a dot marks
 * where it leaves, the arrow where it arrives.
 */
function LinkEdge({ id, source, target, data }: EdgeProps<GraphEdge>) {
  const from = useInternalNode(source);
  const to = useInternalNode(target);
  if (!from || !to || !data) return null;
  const { link, relation, badge, emphasis, labelled, offset } = data;
  const s = boxOf(from);
  const t = boxOf(to);
  let route: Route;
  if (data.route === "float") {
    route = floating(s, t, offset);
  } else {
    const lane = data.route === "lane" ? (from.type === "frame" ? "source" : to.type === "frame" ? "target" : null) : null;
    route = sideways(s, t, offset, lane);
  }
  const width = strokeFor(link.count) + (emphasis === "selected" ? 1 : 0);
  const head = arrowhead(route.end, route.towards, 4 + width * 1.2);
  const casing = (link.family === "dependency" ? width + 3 : width) + 3;
  return (
    <>
      <g
        className={classes.link}
        data-family={link.family}
        data-relation={relation}
        data-kind={link.kind}
        data-link={data.base}
        data-emphasis={emphasis}
      >
        <title>{describeLink(link, relation)}</title>
        {emphasis === "selected" && <path d={route.path} className={classes.halo} style={{ strokeWidth: width + 9 }} />}
        <path d={route.path} className={classes.casing} style={{ strokeWidth: casing }} />
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
          <BaseEdge id={id} path={route.path} className={classes.linkStroke} style={{ strokeWidth: width, strokeDasharray: DASH[link.family]?.(width) }} />
        )}
        <circle cx={route.start.x} cy={route.start.y} r={width / 2 + 1.5} className={classes.origin} />
        <path d={head} className={classes.arrow} data-part="arrow" />
      </g>
      {badge && (labelled || emphasis === "lit" || emphasis === "selected") && emphasis !== "dim" && (
        <EdgeLabelRenderer>
          <span
            className={classes.edgeLabel}
            data-family={badge.family}
            data-relation={relation}
            data-emphasis={emphasis}
            style={{ transform: `translate(-50%, -50%) translate(${route.label.x}px, ${route.label.y}px)` }}
          >
            {badge.label ? (badge.count > 1 ? `${badge.label} · ${compactCount(badge.count)}` : badge.label) : compactCount(badge.count)}
          </span>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

export const edgeTypes: EdgeTypes = {
  link: memo(LinkEdge),
};
