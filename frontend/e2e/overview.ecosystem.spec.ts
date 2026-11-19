import type { Locator, Page } from "@playwright/test";

import type { RepositoriesResponse, TopologyOverviewResponse } from "@/lib/api/generated";
import { CARD_CAP, defaultExpanded, layoutDistricts, summaryId, visibleCards } from "@/lib/graph/districtLayout";
import { districts } from "@/lib/graph/districts";
import { notDrawn, notDrawnText } from "@/lib/graph/overviewCards";
import { overviewView } from "@/lib/graph/overviewView";
import { buildOverview } from "@/lib/model/graph";
import { ecosystem300 } from "@/tests/fixtures/ecosystem";

import { expect, ready, test, wholeMap } from "./fixtures";

/**
 * The overview at the size it has to hold: the 300-repository ecosystem fixture,
 * served in place of the live index's repositories and links (the one mocked case
 * of the overview, as a real workspace of this size is not at hand). Nothing is
 * selected: the inspector would ask the live API about repositories it has never
 * heard of. The screenshots are the evidence; the assertions are that every
 * district is on the map and that what is called readable is.
 */

/** Every node's centre on screen, read in one go once nothing has moved for a moment. */
async function settled(page: Page): Promise<void> {
  const read = () =>
    page.locator(".react-flow__node").evaluateAll((nodes) =>
      JSON.stringify(
        nodes.map((node) => {
          const box = node.getBoundingClientRect();
          return [(node as HTMLElement).dataset.id, Math.round(box.x + box.width / 2), Math.round(box.y + box.height / 2)];
        }),
      ),
    );
  let last = await read();
  await expect
    .poll(
      async () => {
        const now = await read();
        const still = now === last && now !== "[]";
        last = now;
        return still;
      },
      { intervals: [250] },
    )
    .toBe(true);
}

type Rect = { x: number; y: number; width: number; height: number };

/** Whether two boxes share any area. */
function overlaps(a: Rect, b: Rect): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

test("the overview holds an ecosystem of 300 repositories", async ({ page, problems }, testInfo) => {
  test.setTimeout(180_000);
  const ecosystem = ecosystem300();
  const slugs = ecosystem.repositories.map((r) => r.slug);

  await page.route("**/api/ra/topology/overview", async (route) => {
    const response = await route.fetch();
    const body = (await response.json()) as { ok: true; result: TopologyOverviewResponse };
    body.result = {
      ...body.result,
      links: ecosystem.links,
      repositories: slugs,
      ambiguous: [],
      ambiguous_total: 0,
      unsettled_inclusions: [],
      unsettled_inclusions_total: 0,
    };
    await route.fulfill({ response, json: body });
  });
  await page.route("**/api/ra/repositories", async (route) => {
    const response = await route.fetch();
    const body = (await response.json()) as { ok: true; result: RepositoriesResponse };
    const [template] = body.result.repositories ?? [];
    if (template === undefined) throw new Error("the live index lists no repository to copy");
    body.result.repositories = ecosystem.repositories.map((r) => ({
      ...template,
      slug: r.slug,
      name: r.name,
      indexed_files: r.files,
      languages: r.languages,
      branch: r.branch,
      head_sha: r.headSha,
    }));
    await route.fulfill({ response, json: body });
  });

  const edges = buildOverview(ecosystem.repositories, ecosystem.links).edges;
  const ds = districts(ecosystem.repositories, edges);
  const fit = () => page.getByRole("button", { name: "Fit to screen", exact: true }).click();
  const shot = async (name: string) => {
    await settled(page);
    await page.screenshot({ path: testInfo.outputPath(name) });
  };

  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/");
  await ready(page);
  await settled(page);

  // every district is on the map, open as a frame or collapsed as a summary
  const pane = await page
    .locator(".react-flow")
    .evaluate((el) => ({ width: (el as HTMLElement).offsetWidth, height: (el as HTMLElement).offsetHeight }));
  await expect(page.locator("[data-overview]")).toHaveAttribute("data-pane", `${pane.width}x${pane.height}`);
  const expected = defaultExpanded(ds, pane);
  const present = await page
    .locator(".react-flow__node-district, .react-flow__node-districtSummary")
    .evaluateAll((all) => all.map((n) => (n as HTMLElement).dataset.id!).sort());
  expect(present).toEqual(ds.map((d) => (expected.expanded.has(d.id) ? d.id : summaryId(d.id))).sort());
  // and readable means readable
  await expect(page.locator("[data-overview]")).toHaveAttribute("data-readable", String(expected.readable));
  if (expected.readable) {
    const zoom = page.getByLabel("Zoom level");
    await expect(zoom).toHaveText(/^\d+%$/);
    expect(parseInt((await zoom.textContent())!, 10)).toBeGreaterThanOrEqual(75);
    expect(visibleCards(ds, expected.expanded)).toBeLessThanOrEqual(CARD_CAP);
  }
  await shot("ecosystem-default.png");

  const summaries = page.locator(".react-flow__node-districtSummary");
  await expect(summaries.first()).toBeVisible();
  await summaries.first().hover();
  await shot("ecosystem-hover.png");

  // a tree of inclusions opens in place, then the whole map
  await page.getByRole("button", { name: /^Expand t1-00/ }).first().click();
  // the summary that was pointed at is gone, and no mouse-leave came: nothing may stay in focus
  await page.getByLabel("Zoom level").hover();
  await expect(page.locator('.react-flow__edges g[data-kind][data-emphasis="dim"]')).toHaveCount(0);
  await expect(page.locator('.react-flow__edges g[data-emphasis="lit"]')).toHaveCount(0);
  await expect.poll(() => page.locator(".react-flow__node-district").count()).toBeGreaterThanOrEqual(1);
  await shot("ecosystem-tree-open.png");
  await fit();
  await shot("ecosystem-tree-fit.png");

  await wholeMap(page);
  await shot("ecosystem-all.png");

  await page.getByRole("button", { name: "Collapse all districts", exact: true }).click();
  await fit();
  await shot("ecosystem-collapsed.png");
  await expect(page.locator('.react-flow__edges g[data-kind][data-emphasis="dim"]')).toHaveCount(0);
  await expect(page.locator('.react-flow__edges g[data-emphasis="lit"]')).toHaveCount(0);

  // a narrower window: nothing spills past the viewport, nothing is drawn over anything else
  await page.setViewportSize({ width: 1024, height: 900 });
  await settled(page);
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
    .toBe(true);
  const boxOf = async (locator: Locator): Promise<Rect> => {
    const box = await locator.boundingBox();
    if (box === null) throw new Error("an element that has to be on the page has no box");
    return box;
  };
  const totals = await boxOf(page.getByLabel("Workspace totals"));
  const boxes = new Map<string, Rect>();
  for (const [name, locator] of [
    ["the level title", page.getByRole("heading", { name: "All repositories", exact: true })],
    ["Expand all districts", page.getByRole("button", { name: "Expand all districts", exact: true })],
    ["Collapse all districts", page.getByRole("button", { name: "Collapse all districts", exact: true })],
  ] as const) {
    const box = await boxOf(locator);
    boxes.set(name, box);
    expect(overlaps(box, totals), `${name} is drawn over the workspace totals`).toBe(false);
  }
  // the two buttons stay side by side: one never wraps under the other
  expect(
    Math.abs(boxes.get("Expand all districts")!.y - boxes.get("Collapse all districts")!.y),
    "Expand all and Collapse all are on one line",
  ).toBeLessThanOrEqual(1);
  // the legend is shown above the small-screen breakpoint, so 1024px has it
  const legend = await boxOf(page.getByRole("list", { name: "Legend", exact: true }));
  // everything is collapsed, so the status note says what is not drawn, and it ends before the legend begins
  const left1024 = notDrawnText(
    notDrawn(overviewView({ districts: ds, edges, expanded: new Set(), shelf: layoutDistricts(ds, new Set()).shelf })),
    0,
  );
  expect(left1024, "something is left out of the collapsed map").not.toBeNull();
  const note = page.getByTestId("status-note");
  await expect(note).toHaveText(left1024!);
  const noteBox = await boxOf(note);
  expect(noteBox.x + noteBox.width, "the status note runs under the legend").toBeLessThanOrEqual(legend.x);
  const zoom = await boxOf(page.getByLabel("Zoom level"));
  expect(zoom.x).toBeGreaterThanOrEqual(0);
  expect(zoom.y).toBeGreaterThanOrEqual(0);
  expect(zoom.x + zoom.width).toBeLessThanOrEqual(1024);
  expect(zoom.y + zoom.height).toBeLessThanOrEqual(900);
  await shot("ecosystem-1024.png");
  expect(problems).toEqual([]);
});
