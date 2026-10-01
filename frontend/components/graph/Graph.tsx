"use client";

import "@xyflow/react/dist/base.css";

import { ActionIcon, Popover, Tooltip, useComputedColorScheme } from "@mantine/core";
import { CornersOutIcon, InfoIcon, MinusIcon, PlusIcon } from "@phosphor-icons/react";
import {
  getViewportForBounds,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  useStore,
  useViewport,
  type Viewport,
} from "@xyflow/react";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type FocusEvent,
  type ReactNode,
} from "react";

import type { Box, Family } from "@/lib/model/graph";
import { useAnimationsOff } from "@/lib/useMotion";

import { RepoSwatch } from "@/components/common/RepoSwatch";

import { edgeTypes, FAMILY_NOUN, type Emphasis, type GraphEdge } from "./edges";
import { EntityBadge, LineGlyph, PathBadge } from "./glyphs";
import classes from "./Graph.module.css";
import { BACKDROP, type GraphNode } from "./nodes";
import { nodeTypes } from "./registry";
import { easeIn, easeInOut, easeOut, useRegisterExit, type Arrival, type Direction } from "./transition";

interface Props {
  nodes: GraphNode[];
  edges: GraphEdge[];
  /** Changes when the view moves to another level. */
  level: string;
  label: string;
  selected: string | null;
  /** The connection the inspector is showing: drawn as the selected path. */
  selectedEdge?: string | null;
  onSelect: (id: string | null) => void;
  /** A press on a node, outside any button of its own. */
  onActivate: (id: string) => void;
  arrival: Arrival;
  /** What the finished layout covers: fitted even while nodes are still moving there. */
  extent: Box | null;
  /** Changes when the layout re-centres within a level: the view glides to fit it. */
  refit?: string;
  /**
   * The smallest zoom a level is fitted at on arrival: past it the view stays
   * readable from the extent's left edge, and the rest is a pan away.
   */
  minFit?: number;
  families: readonly Family[];
  /** The toolbar's start: where the reader is. */
  start?: ReactNode;
  /** The toolbar's end: what the level holds, and its switches. */
  end?: ReactNode;
  /** What is on screen but incomplete, in a line under the toolbar. */
  notice?: ReactNode;
}

export function Graph(props: Props) {
  return (
    <ReactFlowProvider>
      <Canvas {...props} />
    </ReactFlowProvider>
  );
}

const MIN_ZOOM = 0.12;
const MAX_ZOOM = 2.5;
/** Fitting never magnifies past 100%: a small level stays at its own size. */
const FIT_MAX = 1;
/** A margin round what is fitted: the toolbar and status bar sit outside the canvas. */
const PADDING = { top: "36px", bottom: "36px", left: "40px", right: "40px" } as const;

function shapeOf(box: Box): string {
  return `${Math.round(box.x)},${Math.round(box.y)},${Math.round(box.width)},${Math.round(box.height)}`;
}

function boxIn(nodes: readonly GraphNode[], id: string): Box | null {
  const node = nodes.find((n) => n.id === id);
  if (!node || node.width === undefined || node.height === undefined) return null;
  return { x: node.position.x, y: node.position.y, width: node.width, height: node.height };
}

/** `viewport` scaled by `factor` about the middle of the screen. */
/** The whole extent in view, or, where that would shrink it under `minFit`, its left part at `minFit`. */
function fitted(extent: Box, width: number, height: number, minFit: number | undefined): Viewport {
  const whole = getViewportForBounds(extent, width, height, MIN_ZOOM, FIT_MAX, PADDING);
  if (minFit === undefined || whole.zoom >= minFit) return whole;
  return {
    x: parseFloat(PADDING.left) - extent.x * minFit,
    y: height / 2 - (extent.y + extent.height / 2) * minFit,
    zoom: minFit,
  };
}

function about(viewport: Viewport, factor: number, width: number, height: number): Viewport {
  return {
    x: width / 2 - (width / 2 - viewport.x) * factor,
    y: height / 2 - (height / 2 - viewport.y) * factor,
    zoom: viewport.zoom * factor,
  };
}

function Canvas({
  nodes,
  edges,
  level,
  label,
  selected,
  selectedEdge = null,
  onSelect,
  onActivate,
  arrival,
  extent,
  refit,
  minFit,
  families,
  start,
  end,
  notice,
}: Props) {
  const scheme = useComputedColorScheme("dark", { getInitialValueInEffect: true });
  const reduced = useAnimationsOff();
  const { setViewport, getViewport } = useReactFlow();
  const width = useStore((s) => s.width);
  const height = useStore((s) => s.height);
  const [hovered, setHovered] = useState<string | null>(null);

  // What is selected, else what is pointed at, is in focus: its lines are lit,
  // the things it touches stay, and everything else fades. Layout never moves
  // for it. A thing's own lines are drawn only while it is in focus; a level's
  // totals stand in for them the rest of the time. A selection holds until it
  // is cleared, so the view never changes under a pointer that merely passes.
  const focus = selected ?? hovered;
  const related = useMemo(() => {
    if (focus === null) return null;
    const set = new Set([focus]);
    for (const e of edges) {
      if (e.source === focus) set.add(e.target);
      if (e.target === focus) set.add(e.source);
    }
    return set;
  }, [edges, focus]);

  const shownNodes = useMemo<GraphNode[]>(
    () =>
      nodes.map((node) => {
        const isSelected = node.id === selected;
        // the selection never fades, even while something else is pointed at
        const faded =
          related !== null && !BACKDROP.has(node.type ?? "") && node.id !== focus && !related.has(node.id) && !isSelected;
        const className = faded ? classes.isFaded : undefined;
        if (!isSelected && className === undefined) return node;
        return { ...node, className, data: isSelected ? { ...node.data, selected: true } : node.data } as GraphNode;
      }),
    [nodes, related, focus, selected],
  );

  const shownEdges = useMemo<GraphEdge[]>(
    () =>
      edges.map((edge) => {
        const scope = edge.data?.scope ?? null;
        const touching = focus !== null && (edge.source === focus || edge.target === focus);
        let emphasis: Emphasis = "rest";
        let hidden = false;
        if (focus === null) hidden = scope === "detail";
        else if (scope === "aggregate") hidden = true;
        else if (touching) emphasis = "lit";
        else if (scope === "detail") hidden = true;
        else emphasis = "dim";
        if (selectedEdge !== null && edge.id === selectedEdge) {
          emphasis = "selected";
          hidden = false;
        }
        const raised = emphasis === "lit" || emphasis === "selected";
        return { ...edge, hidden, zIndex: raised ? 1000 : 0, data: { ...edge.data!, emphasis } };
      }),
    [edges, focus, selectedEdge],
  );

  // keyboard focus inside a node counts as pointing at it
  const onFocus = useCallback((event: FocusEvent<HTMLDivElement>) => {
    const node = (event.target as HTMLElement).closest<HTMLElement>(".react-flow__node");
    const id = node?.dataset.id ?? null;
    setHovered(id !== null && !node?.classList.contains("react-flow__node-frame") ? id : null);
  }, []);
  const onBlur = useCallback(() => setHovered(null), []);
  // Escape lets go of the selection wherever focus is (after Back it rests on
  // the page, not in the canvas), unless a field or an open dialog owns it
  useEffect(() => {
    if (selected === null) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest('input, textarea, select, [contenteditable="true"], [role="dialog"]')) return;
      onSelect(null);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onSelect, selected]);

  // -- moving between levels --------------------------------------------------------

  // hidden until a level's first view is set, shown once it is, and fading
  // while the canvas leaves for another
  const [shownLevel, setShownLevel] = useState<string | null>(null);
  const [leaving, setLeaving] = useState(false);
  const phase = leaving ? "leaving" : shownLevel === level ? "shown" : "hidden";

  // arriving: start where the reader's eye was (inside what was opened, or on
  // the child just left) and settle on the whole level
  const entered = useRef<string | null>(null);
  const fittedFor = useRef({ refit, width, height });
  const fittedExtent = useRef<string | null>(null);
  /** The reader has moved the view (a wheel, a drag) since it was last fitted. */
  const moved = useRef(false);
  const pending = useRef<number | null>(null);
  useLayoutEffect(() => {
    if (entered.current === level) return;
    if (nodes.length === 0 || extent === null || width === 0 || height === 0) return;
    entered.current = level;
    fittedFor.current = { refit, width, height };
    fittedExtent.current = shapeOf(extent);
    moved.current = false;
    const target = fitted(extent, width, height, minFit);
    let start = target;
    let duration = arrival.direction === "out" ? 480 : 420;
    if (!reduced && arrival.direction === "in") start = about(target, 0.78, width, height);
    if (!reduced && arrival.direction === "out") {
      const from = arrival.from ? boxIn(nodes, arrival.from) : null;
      start = from ? getViewportForBounds(from, width, height, MIN_ZOOM, MAX_ZOOM, 0.6) : about(target, 1.35, width, height);
    }
    const focus = !reduced && arrival.focus ? boxIn(nodes, arrival.focus) : null;
    if (focus) {
      start = getViewportForBounds(focus, width, height, MIN_ZOOM, MAX_ZOOM, 0.35);
      duration = 760;
    }
    void setViewport(start);
    if (pending.current !== null) cancelAnimationFrame(pending.current);
    pending.current = requestAnimationFrame(() => {
      pending.current = null;
      setLeaving(false);
      setShownLevel(level);
      if (start !== target) void setViewport(target, { duration, ease: focus ? easeInOut : easeOut });
    });
  }, [level, nodes, extent, width, height, reduced, arrival, refit, minFit, setViewport]);
  useEffect(() => () => {
    if (pending.current !== null) cancelAnimationFrame(pending.current);
  }, []);

  // re-centring within a level (the overview's anchor changed), the canvas
  // changing size (the inspector opening beside it), or the level growing (a
  // card's preview arriving): glide to the new fit; growth alone leaves the view
  // where it is if the reader has moved it themselves
  useEffect(() => {
    const last = fittedFor.current;
    if (entered.current !== level || extent === null || width === 0) return;
    const recentred = refit !== last.refit;
    const resized = width !== last.width || height !== last.height;
    const grown = shapeOf(extent) !== fittedExtent.current;
    if (!recentred && !resized && !grown) return;
    fittedFor.current = { refit, width, height };
    fittedExtent.current = shapeOf(extent);
    if (!recentred && !resized && moved.current) return;
    moved.current = false;
    const target = fitted(extent, width, height, minFit);
    void setViewport(target, { duration: reduced ? 0 : recentred ? 560 : 320, ease: easeInOut });
  }, [level, refit, extent, width, height, reduced, minFit, setViewport]);

  // leaving: zoom into what is being opened, or back out, as the view fades
  const register = useRegisterExit();
  const exit = useCallback(
    async (direction: Direction, via: string | null) => {
      if (reduced || direction === "none") return;
      setLeaving(true);
      const box = via ? boxIn(nodes, via) : null;
      if (direction === "in" && box) {
        await setViewport(getViewportForBounds(box, width, height, MIN_ZOOM, 3, 0.4), { duration: 300, ease: easeIn });
      } else if (direction === "out") {
        await setViewport(about(getViewport(), 0.7, width, height), { duration: 260, ease: easeIn });
      } else {
        await new Promise((resolve) => setTimeout(resolve, 150));
      }
    },
    [reduced, nodes, setViewport, getViewport, width, height],
  );
  useEffect(() => register?.(exit), [register, exit]);

  return (
    <div className={classes.view}>
      {(start || end) && <Toolbar start={start} end={end} />}
      {notice && <Notice>{notice}</Notice>}
      <div className={classes.stage} data-phase={phase}>
        <ReactFlow
          className={classes.flow}
          aria-label={label}
          nodes={shownNodes}
          edges={shownEdges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          colorMode={scheme}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          nodesFocusable={false}
          edgesFocusable={false}
          zoomOnDoubleClick={false}
          onNodeMouseEnter={(_, node) => {
            if (!BACKDROP.has(node.type ?? "")) setHovered(node.id);
          }}
          onNodeMouseLeave={() => setHovered(null)}
          onNodeClick={(_, node) => {
            if (!BACKDROP.has(node.type ?? "") && node.type !== "anchor") onActivate(node.id);
          }}
          onPaneClick={() => onSelect(null)}
          onMoveStart={(event) => {
            // a move the reader makes, not one the canvas animates
            if (event) moved.current = true;
          }}
          onFocus={onFocus}
          onBlur={onBlur}
          minZoom={MIN_ZOOM}
          maxZoom={MAX_ZOOM}
          onlyRenderVisibleElements
          proOptions={{ hideAttribution: false }}
        />
      </div>
      <div className={classes.statusBar}>
        <ZoomControls extent={extent} reduced={reduced} />
        <Legend families={families} />
      </div>
    </div>
  );
}

/** What is on screen but incomplete, said plainly in a line under the toolbar. */
export function Notice({ children, role }: { children: ReactNode; role?: "status" }) {
  return (
    <div className={classes.notice} role={role}>
      {children}
    </div>
  );
}

/** The bar above a view: where the reader is at the start, what the level holds at the end. */
export function Toolbar({ start, end }: { start?: ReactNode; end?: ReactNode }) {
  return (
    <div className={classes.toolbar}>
      <div className={classes.toolbarStart}>{start}</div>
      {end && <div className={classes.toolbarEnd}>{end}</div>}
    </div>
  );
}

function ZoomControls({ extent, reduced }: { extent: Box | null; reduced: boolean }) {
  const { zoomIn, zoomOut, setViewport } = useReactFlow();
  const { zoom } = useViewport();
  const width = useStore((s) => s.width);
  const height = useStore((s) => s.height);
  const duration = reduced ? 0 : 200;
  const fit = () => {
    if (extent) void setViewport(getViewportForBounds(extent, width, height, MIN_ZOOM, FIT_MAX, PADDING), { duration });
  };
  return (
    <div className={classes.zoom} role="group" aria-label="Zoom">
      <Tooltip label="Zoom out">
        <ActionIcon size="sm" onClick={() => void zoomOut({ duration })} aria-label="Zoom out">
          <MinusIcon size={13} />
        </ActionIcon>
      </Tooltip>
      <span className={classes.zoomValue} aria-label="Zoom level">
        {Math.round(zoom * 100)}%
      </span>
      <Tooltip label="Zoom in">
        <ActionIcon size="sm" onClick={() => void zoomIn({ duration })} aria-label="Zoom in">
          <PlusIcon size={13} />
        </ActionIcon>
      </Tooltip>
      <Tooltip label="Fit to screen">
        <ActionIcon size="sm" onClick={fit} aria-label="Fit to screen">
          <CornersOutIcon size={13} />
        </ActionIcon>
      </Tooltip>
    </div>
  );
}

const LEGEND_LABEL: Record<Family, string> = {
  code: "Code",
  http: "HTTP",
  dependency: "Package dependency",
  config: "Config & env",
};

/** What each stroke means. Colour is never the only difference: each has its own pattern. */
function Legend({ families }: { families: readonly Family[] }) {
  return (
    <div className={classes.legendBar}>
      <ul className={classes.legend} aria-label="Legend">
        {families.map((family) => (
          <li key={family} className={classes.legendItem} title={`${FAMILY_NOUN[family]} relationships`}>
            <LineGlyph family={family} width={24} />
            {LEGEND_LABEL[family]}
          </li>
        ))}
        <li className={classes.legendItem}>
          <LineGlyph family="selected" width={24} />
          Selected
        </li>
      </ul>
      <Marks />
    </div>
  );
}

/** The marks things carry, on demand: the canvas shows them, this names them. */
function Marks() {
  const rows: { mark: ReactNode; label: string }[] = [
    { mark: <RepoSwatch hue={1} />, label: "Repository, in its own colour" },
    { mark: <PathBadge path="src" />, label: "Directory" },
    { mark: <PathBadge path="index.ts" />, label: "File" },
    { mark: <EntityBadge entityType="package" />, label: "Package" },
    { mark: <EntityBadge entityType="http_route" />, label: "HTTP route or endpoint" },
    { mark: <EntityBadge entityType="http_deployment" />, label: "Deployment" },
    { mark: <EntityBadge entityType="env_var" />, label: "Environment variable" },
    { mark: <EntityBadge entityType="config" />, label: "Configuration key" },
    { mark: <EntityBadge entityType="module" />, label: "External module" },
    { mark: <EntityBadge entityType="command" />, label: "Command" },
  ];
  return (
    <Popover position="top-end" shadow="md" trapFocus returnFocus>
      <Popover.Target>
        <Tooltip label="What the marks mean">
          <ActionIcon size="sm" variant="subtle" aria-label="What the marks mean">
            <InfoIcon size={15} />
          </ActionIcon>
        </Tooltip>
      </Popover.Target>
      <Popover.Dropdown>
        <ul className={classes.marks} aria-label="What the marks mean">
          {rows.map((row) => (
            <li key={row.label}>
              <span className={classes.markCell}>{row.mark}</span>
              {row.label}
            </li>
          ))}
        </ul>
      </Popover.Dropdown>
    </Popover>
  );
}
