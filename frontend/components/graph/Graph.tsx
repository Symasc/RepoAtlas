"use client";

import "@xyflow/react/dist/base.css";

import { ActionIcon, Tooltip, useComputedColorScheme } from "@mantine/core";
import { useReducedMotion } from "@mantine/hooks";
import { CornersOutIcon, MinusIcon, PlusIcon } from "@phosphor-icons/react";
import {
  Background,
  BackgroundVariant,
  Panel,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  useViewport,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef, useState, type FocusEvent, type ReactNode } from "react";

import { edgeTypes, type Emphasis, type GraphEdge } from "./edges";
import classes from "./Graph.module.css";
import { nodeTypes, type GraphNode } from "./nodes";

interface Props {
  nodes: GraphNode[];
  edges: GraphEdge[];
  /** Changes when the view moves to another level: the canvas fits itself once per level. */
  level: string;
  label: string;
  /** Show the containment line in the legend (an opened repository draws it). */
  containment: boolean;
  topLeft?: ReactNode;
  topCenter?: ReactNode;
  topRight?: ReactNode;
}

export function Graph(props: Props) {
  return (
    <ReactFlowProvider>
      <Canvas {...props} />
    </ReactFlowProvider>
  );
}

const FIT = { padding: 0.14, maxZoom: 1 } as const;

function Canvas({ nodes, edges, level, label, containment, topLeft, topCenter, topRight }: Props) {
  const scheme = useComputedColorScheme("dark", { getInitialValueInEffect: true });
  const reduced = useReducedMotion();
  const { fitView } = useReactFlow();
  const [hovered, setHovered] = useState<string | null>(null);

  // Pointing at (or focusing) a node lights its edges and dims the rest; nothing
  // re-lays out. At rest every edge runs behind the cards; a lit one is lifted
  // above them. A card's own lines off the grid appear only while it is pointed
  // at, and the level's totals stand in for them the rest of the time.
  const shown = useMemo<GraphEdge[]>(
    () =>
      edges.map((edge) => {
        const touching = hovered !== null && (edge.source === hovered || edge.target === hovered);
        const scope = edge.type === "link" ? edge.data?.scope : null;
        let emphasis: Emphasis = hovered === null ? "rest" : touching ? "lit" : "dim";
        let hidden = false;
        if (scope === "detail") {
          hidden = !touching;
          emphasis = touching ? "lit" : "rest";
        }
        const zIndex = emphasis === "lit" && edge.type === "link" ? 1000 : 0;
        return { ...edge, hidden, zIndex, data: { ...edge.data, emphasis } } as GraphEdge;
      }),
    [edges, hovered],
  );

  // keyboard focus inside a node counts as pointing at it
  const onFocus = useCallback((event: FocusEvent<HTMLDivElement>) => {
    const node = (event.target as HTMLElement).closest<HTMLElement>(".react-flow__node");
    setHovered(node?.dataset.id ?? null);
  }, []);
  const onBlur = useCallback(() => setHovered(null), []);

  // fit once per level, when its nodes first arrive; later changes (a preview
  // filling a card) leave the viewport where the reader put it
  const fitted = useRef<string | null>(null);
  useEffect(() => {
    if (nodes.length === 0 || fitted.current === level) return;
    fitted.current = level;
    const frame = requestAnimationFrame(() => void fitView({ ...FIT, duration: reduced ? 0 : 280 }));
    return () => cancelAnimationFrame(frame);
  }, [level, nodes.length, fitView, reduced]);

  return (
    <ReactFlow
      className={classes.flow}
      aria-label={label}
      nodes={nodes}
      edges={shown}
      nodeTypes={nodeTypes}
      edgeTypes={edgeTypes}
      colorMode={scheme}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable={false}
      nodesFocusable={false}
      edgesFocusable={false}
      onNodeMouseEnter={(_, node) => setHovered(node.id)}
      onNodeMouseLeave={() => setHovered(null)}
      onFocus={onFocus}
      onBlur={onBlur}
      minZoom={0.15}
      maxZoom={1.75}
      onlyRenderVisibleElements
      proOptions={{ hideAttribution: false }}
    >
      <Background variant={BackgroundVariant.Dots} gap={24} size={1.4} />
      {topLeft && <Panel position="top-left">{topLeft}</Panel>}
      {topCenter && <Panel position="top-center">{topCenter}</Panel>}
      {topRight && <Panel position="top-right">{topRight}</Panel>}
      <Panel position="bottom-left">
        <ZoomControls reduced={reduced} />
      </Panel>
      <Panel position="bottom-center">
        <Legend containment={containment} />
      </Panel>
    </ReactFlow>
  );
}

function ZoomControls({ reduced }: { reduced: boolean }) {
  const { zoomIn, zoomOut, fitView } = useReactFlow();
  const { zoom } = useViewport();
  const duration = reduced ? 0 : 160;
  return (
    <div className={classes.zoom} role="group" aria-label="Zoom">
      <Tooltip label="Zoom out">
        <ActionIcon size="md" onClick={() => void zoomOut({ duration })} aria-label="Zoom out">
          <MinusIcon size={14} />
        </ActionIcon>
      </Tooltip>
      <span className={classes.zoomValue} aria-label="Zoom level">
        {Math.round(zoom * 100)}%
      </span>
      <Tooltip label="Zoom in">
        <ActionIcon size="md" onClick={() => void zoomIn({ duration })} aria-label="Zoom in">
          <PlusIcon size={14} />
        </ActionIcon>
      </Tooltip>
      <span className={classes.zoomDivider} aria-hidden />
      <Tooltip label="Fit to screen">
        <ActionIcon size="md" onClick={() => void fitView({ ...FIT, duration })} aria-label="Fit to screen">
          <CornersOutIcon size={14} />
        </ActionIcon>
      </Tooltip>
    </div>
  );
}

/** What each stroke means. Colour is never the only difference: each also has its own pattern. */
function Legend({ containment }: { containment: boolean }) {
  const items: { kind: string; label: string }[] = [
    { kind: "observed", label: "Code" },
    { kind: "http", label: "HTTP" },
    { kind: "dependency", label: "Package dependency" },
    ...(containment ? [{ kind: "tree", label: "Contains" }] : []),
  ];
  return (
    <ul className={classes.legend} aria-label="Legend">
      {items.map((item) => (
        <li key={item.kind} className={classes.legendItem}>
          <svg width="28" height="10" viewBox="0 0 28 10" aria-hidden className={classes.legendLine} data-kind={item.kind}>
            {item.kind === "dependency" ? (
              <>
                <line x1="1" y1="5" x2="27" y2="5" className={classes.legendOuter} />
                <line x1="1" y1="5" x2="27" y2="5" className={classes.legendInner} />
              </>
            ) : (
              <line x1="1" y1="5" x2="27" y2="5" className={classes.legendStroke} />
            )}
          </svg>
          {item.label}
        </li>
      ))}
    </ul>
  );
}
