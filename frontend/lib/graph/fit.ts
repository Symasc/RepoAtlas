import { getViewportForBounds, type Viewport } from "@xyflow/react";

import type { Box } from "@/lib/model/graph";

/**
 * How a level is fitted to the canvas. Pure, so the zoom a layout is shown at is
 * measured in unit tests rather than only seen in a browser.
 */

export const MIN_ZOOM = 0.12;
export const MAX_ZOOM = 2.5;
/** Fitting never magnifies past 100%: a small level stays at its own size. */
export const FIT_MAX = 1;
/** A margin round what is fitted: the toolbar and status bar sit outside the canvas. */
export const FIT_PADDING = { top: "36px", bottom: "36px", left: "40px", right: "40px" } as const;

export interface FitOptions {
  /**
   * The smallest zoom a level is fitted at: past it the view stays readable
   * from the extent's left edge, and the rest is a pan away.
   */
  minFit?: number | undefined;
  /**
   * Where a level held at `minFit` sits vertically: its middle on the screen's
   * middle (the default), or its top edge at the top of the screen, as a page
   * is read from. An extent that fits whole is never moved by it.
   */
  anchor?: "middle" | "start" | undefined;
}

/** The whole extent in view, or, where that would shrink it under `minFit`, its left part at `minFit`. */
export function fitViewport(extent: Box, viewport: { width: number; height: number }, options: FitOptions = {}): Viewport {
  const { width, height } = viewport;
  const { minFit, anchor = "middle" } = options;
  const whole = getViewportForBounds(extent, width, height, MIN_ZOOM, FIT_MAX, FIT_PADDING);
  if (minFit === undefined || whole.zoom >= minFit) return whole;
  return {
    x: parseFloat(FIT_PADDING.left) - extent.x * minFit,
    y:
      anchor === "start"
        ? parseFloat(FIT_PADDING.top) - extent.y * minFit
        : height / 2 - (extent.y + extent.height / 2) * minFit,
    zoom: minFit,
  };
}

/** The zoom `fitViewport` settles on. */
export function fitZoom(extent: Box, viewport: { width: number; height: number }, options: FitOptions = {}): number {
  return fitViewport(extent, viewport, options).zoom;
}
