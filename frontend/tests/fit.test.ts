import { describe, expect, it } from "vitest";

import { fitViewport, fitZoom, FIT_PADDING } from "@/lib/graph/fit";
import type { Box } from "@/lib/model/graph";

/**
 * How a level is fitted: the whole extent in view, or, past a smallest readable
 * zoom, its left part at that zoom. The anchor says where that part sits
 * vertically: the middle (how every level was always held) or the top, as a page
 * is read from.
 */

const pane = { width: 1440, height: 810 };
const LEFT = parseFloat(FIT_PADDING.left);
const TOP = parseFloat(FIT_PADDING.top);

/** Fits whole at 100%. */
const small: Box = { x: 100, y: 50, width: 400, height: 300 };
/** Far larger than the pane: fitted whole it would be under the zoom floor. */
const large: Box = { x: 30, y: 20, width: 20000, height: 9000 };

describe("fitViewport", () => {
  it("is the plain fit when nothing asks for a smallest zoom, whatever the anchor", () => {
    const plain = fitViewport(large, pane);
    expect(fitViewport(large, pane, { anchor: "start" })).toEqual(plain);
    expect(fitViewport(large, pane, { anchor: "middle" })).toEqual(plain);
  });

  it("gives an extent that already fits the plain fit, at either anchor", () => {
    const plain = fitViewport(small, pane);
    expect(plain.zoom).toBeGreaterThanOrEqual(0.75);
    for (const anchor of ["middle", "start"] as const) {
      expect(fitViewport(small, pane, { minFit: 0.75, anchor }), anchor).toEqual(plain);
    }
    expect(fitViewport(small, pane, { minFit: 0.75 })).toEqual(plain);
  });

  it("holds an extent that is too large at the smallest zoom, its top left at the padding when anchored at the start", () => {
    for (const minFit of [0.75, 0.5]) {
      expect(fitZoom(large, pane), "the plain fit is under the floor").toBeLessThan(minFit);
      const view = fitViewport(large, pane, { minFit, anchor: "start" });
      expect(view.zoom).toBe(minFit);
      expect(view.x).toBeCloseTo(LEFT - large.x * minFit, 9);
      expect(view.y).toBeCloseTo(TOP - large.y * minFit, 9);
      // the extent's top left lands on the padding
      expect(large.x * view.zoom + view.x).toBeCloseTo(LEFT, 9);
      expect(large.y * view.zoom + view.y).toBeCloseTo(TOP, 9);
    }
  });

  it("keeps the middle anchor as it was: the extent's middle on the pane's middle", () => {
    const minFit = 0.75;
    const view = fitViewport(large, pane, { minFit });
    expect(view).toEqual(fitViewport(large, pane, { minFit, anchor: "middle" }));
    expect(view.zoom).toBe(minFit);
    expect(view.x).toBeCloseTo(LEFT - large.x * minFit, 9);
    expect(view.y).toBeCloseTo(pane.height / 2 - (large.y + large.height / 2) * minFit, 9);
  });

  it("differs between the anchors only vertically", () => {
    const middle = fitViewport(large, pane, { minFit: 0.75, anchor: "middle" });
    const start = fitViewport(large, pane, { minFit: 0.75, anchor: "start" });
    expect(start.x).toBe(middle.x);
    expect(start.zoom).toBe(middle.zoom);
    expect(start.y).not.toBe(middle.y);
  });

  it("is deterministic", () => {
    const options = { minFit: 0.75, anchor: "start" } as const;
    expect(fitViewport(large, pane, options)).toEqual(fitViewport(large, pane, options));
  });
});
