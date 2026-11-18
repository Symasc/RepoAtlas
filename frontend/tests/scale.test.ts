import { describe, expect, it } from "vitest";

import { FIT_PADDING, fitZoom } from "@/lib/graph/fit";
import { bounds, constellation, GROUP_ID, type Constellation } from "@/lib/graph/layout";
import { buildOverview, repositoryId, type Box } from "@/lib/model/graph";

import { ecosystem300, FIXTURE_PARTS } from "./fixtures/ecosystem";

/**
 * The baseline of the current overview at 300 repositories. Nothing here is a
 * target: the numbers are reported (one `SCALE_BASELINE` line, one
 * `SCALE_CURVE` line), so the layout that replaces `constellation` is compared
 * against what it replaces. Only sanity is asserted, and never how long
 * anything takes.
 */

const VIEWPORT = { width: 1440, height: 810 } as const;
/** What the fit margin leaves of the canvas. */
const CANVAS = {
  width: VIEWPORT.width - parseFloat(FIT_PADDING.left) - parseFloat(FIT_PADDING.right),
  height: VIEWPORT.height - parseFloat(FIT_PADDING.top) - parseFloat(FIT_PADDING.bottom),
} as const;

/** Strict box intersection, with none of the layout's own clear margin (as the overlap checks in graph.test.ts). */
function intersects(a: Box, b: Box): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

const round = (n: number) => Math.round(n * 100) / 100;
const round4 = (n: number) => Math.round(n * 10000) / 10000;

/** The extent as OverviewLevel takes it: every box but the unlinked group's frame. */
function extentOf(shape: Constellation): { boxes: Box[]; extent: Box } {
  const boxes = [...shape.layout.entries()].filter(([id]) => id !== GROUP_ID).map(([, box]) => box);
  return { boxes, extent: bounds(boxes)! };
}

/**
 * `zoom` is the zoom the fit settles on (clamped); `wholeZoom` the one that
 * would put the whole extent in view, unclamped; `visibleShare` how much of the
 * extent's area the canvas shows at `zoom`, 1 once all of it is in view.
 */
function zooms(extent: Box) {
  const zoom = fitZoom(extent, VIEWPORT);
  const wholeZoom = Math.min(CANVAS.width / extent.width, CANVAS.height / extent.height);
  const visibleShare = Math.min(1, (CANVAS.width / zoom) * (CANVAS.height / zoom) / (extent.width * extent.height));
  return { zoom, wholeZoom, visibleShare };
}

function measure(shape: Constellation, ids: readonly string[], requested: string | null) {
  const { boxes, extent } = extentOf(shape);
  const repositories = ids.map((id) => shape.layout.get(id)!);
  let overlapping = 0;
  for (let i = 0; i < repositories.length; i++) {
    for (let j = i + 1; j < repositories.length; j++) {
      if (intersects(repositories[i]!, repositories[j]!)) overlapping += 1;
    }
  }
  const { zoom, wholeZoom, visibleShare } = zooms(extent);
  return {
    selected: requested,
    anchor: shape.anchor,
    boxes: boxes.length,
    extent: { width: round(extent.width), height: round(extent.height) },
    fitZoom: round4(zoom),
    wholeZoom: round4(wholeZoom),
    visibleShare: round4(visibleShare),
    repositoryWidthAtFit: round(repositories[0]!.width * zoom),
    rings: shape.rings.length,
    largestRingRy: round(Math.max(0, ...shape.rings.map((r) => r.ry))),
    overlappingPairs: overlapping,
  };
}

describe("the overview at 300 repositories (baseline of the current layout)", () => {
  const { repositories, links } = ecosystem300();
  const overview = buildOverview(repositories, links);
  const ids = repositories.map((r) => repositoryId(r.slug));
  const mesh = repositoryId(FIXTURE_PARTS.communities[2]![7]!);
  const alone = repositoryId(FIXTURE_PARTS.independent[0]!);

  it("lays out every repository, the same way twice, and reports the shape", { timeout: 120_000 }, () => {
    const first = constellation(repositories, overview.edges, null);
    const again = constellation(repositories, overview.edges, null);
    expect([...again.layout]).toEqual([...first.layout]);
    expect(again.rings).toEqual(first.rings);

    const scenarios: [string | null, Constellation][] = [
      [null, first],
      [mesh, constellation(repositories, overview.edges, mesh)],
      [alone, constellation(repositories, overview.edges, alone)],
    ];
    for (const [, shape] of scenarios) {
      for (const id of ids) {
        const box = shape.layout.get(id);
        expect(box, `${id} has a box`).toBeDefined();
        expect(box!.width).toBeGreaterThan(0);
        expect(box!.height).toBeGreaterThan(0);
      }
    }

    const report = scenarios.map(([requested, shape]) => measure(shape, ids, requested));
    for (const r of report) {
      expect(r.fitZoom).toBeGreaterThan(0);
      expect(r.fitZoom).toBeLessThanOrEqual(1);
      expect(r.wholeZoom).toBeGreaterThan(0);
      expect(r.visibleShare).toBeGreaterThan(0);
      expect(r.visibleShare).toBeLessThanOrEqual(1);
    }
    console.info(
      `SCALE_BASELINE ${JSON.stringify({
        repositories: repositories.length,
        links: links.length,
        edges: overview.edges.length,
        linked: overview.linked.length,
        independent: overview.independent.length,
        viewport: VIEWPORT,
        overlap: "strict box intersection among the 300 repository boxes, no margin",
        scenarios: report,
      })}`,
    );
  });

  it("reports how the fit falls as the mesh grows, one community at a time", { timeout: 120_000 }, () => {
    const curve = [1, 2, 4, 6].map((k) => {
      const members = new Set(FIXTURE_PARTS.communities.slice(0, k).flat());
      const subset = repositories.filter((r) => members.has(r.slug));
      const sub = buildOverview(
        subset,
        links.filter((l) => members.has(l.source) && members.has(l.target)),
      );
      const shape = constellation(subset, sub.edges, null);
      const { extent } = extentOf(shape);
      const { zoom, wholeZoom, visibleShare } = zooms(extent);
      expect(sub.linked).toHaveLength(members.size);
      expect(sub.independent).toHaveLength(0);
      expect(zoom).toBeGreaterThan(0);
      expect(zoom).toBeLessThanOrEqual(1);
      expect(wholeZoom).toBeGreaterThan(0);
      return {
        communities: k,
        linked: sub.linked.length,
        edges: sub.edges.length,
        wholeZoom: round4(wholeZoom),
        fitZoom: round4(zoom),
        visibleShare: round4(visibleShare),
      };
    });
    expect(curve.map((c) => c.linked)).toEqual([15, 30, 60, 88]);
    console.info(`SCALE_CURVE ${JSON.stringify({ viewport: VIEWPORT, canvas: CANVAS, curve })}`);
  });
});
