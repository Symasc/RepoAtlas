import type { Locator, Page } from "@playwright/test";

import type { RepositoriesResponse, TopologyOverviewResponse } from "@/lib/api/generated";
import { CARD_CAP, defaultExpanded, layoutDistricts, summaryId, visibleCards } from "@/lib/graph/districtLayout";
import { districts, wholeId } from "@/lib/graph/districts";
import { districtLinks, isolate, wholeLabel } from "@/lib/graph/isolate";
import { liftEdges } from "@/lib/graph/lift";
import { isolationText, notDrawn, notDrawnText, summaryFacts, summaryLabel } from "@/lib/graph/overviewCards";
import { resolveOverview, type ResolvedOverview } from "@/lib/graph/overviewState";
import { overviewView } from "@/lib/graph/overviewView";
import { buildOverview, repositoryId } from "@/lib/model/graph";
import { readOverviewState, type OverviewState } from "@/lib/model/location";
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

/** Serves the ecosystem in place of the live index's repositories and links. */
async function serveEcosystem(page: Page, ecosystem: ReturnType<typeof ecosystem300>): Promise<void> {
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
}

test("the overview holds an ecosystem of 300 repositories", async ({ page, problems }, testInfo) => {
  test.setTimeout(180_000);
  const ecosystem = ecosystem300();
  await serveEcosystem(page, ecosystem);

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
  // the two buttons that open and close every district stay side by side as the window narrows
  for (const width of [1024, 900, 760]) {
    await page.setViewportSize({ width, height: 900 });
    await settled(page);
    const open = await boxOf(page.getByRole("button", { name: "Expand all districts" }));
    const close = await boxOf(page.getByRole("button", { name: "Collapse all districts" }));
    expect(Math.abs(open.y - close.y), `Expand all and Collapse all share a row at ${width}px`).toBeLessThan(2);
  }
  expect(problems).toEqual([]);
});

test("isolating a district shows it and the districts linked to it, counts the rest, and Show all brings the map back", async ({
  page,
  problems,
}, testInfo) => {
  test.setTimeout(240_000);
  const ecosystem = ecosystem300();
  await serveEcosystem(page, ecosystem);

  // what the model says the page should hold
  const edges = buildOverview(ecosystem.repositories, ecosystem.links).edges;
  const ds = districts(ecosystem.repositories, edges);
  const adjacency = districtLinks(ds, edges);
  const names = new Map(ecosystem.repositories.map((r) => [repositoryId(r.slug), r.name]));
  const token = (id: string) => id.slice("district:".length);
  const WHOLE = "district:tree:t1-00";
  const FIRST = `${WHOLE}#1`;
  const parts = ds.filter((d) => wholeId(d) === WHOLE);
  expect(ds.map((d) => d.id), "the first inclusion tree is cut into parts").toContain(FIRST);
  expect(parts.length).toBeGreaterThanOrEqual(2);
  expect(parts[0]!.id).toBe(FIRST);
  const iso = isolate(ds, adjacency, WHOLE)!;
  expect(iso.label).toBe(wholeLabel(parts[0]!));
  expect(iso.hidden.length, "isolating hides something").toBeGreaterThan(0);
  const shownIds = new Set([...iso.core, ...iso.neighbours]);
  expect(liftEdges(edges, ds, new Set(), shownIds).outside.size, "something is counted outside").toBeGreaterThan(0);
  const partTokens = parts.map((d) => token(d.id)).sort();
  const laterTokens = partTokens.filter((t) => t !== token(FIRST));

  const query = (pairs: [string, string][]) => `?${new URLSearchParams(pairs).toString()}`;
  const search = () => page.evaluate(() => window.location.search);
  const entries = () => page.evaluate(() => window.history.length);
  const nodeIds = (selector: string) =>
    page.locator(selector).evaluateAll((all) => all.map((n) => (n as HTMLElement).dataset.id!).sort());
  const lineGroups = page.locator(".react-flow__edges g[data-kind][data-emphasis]");
  const overviewEl = page.locator("[data-overview]");
  const notice = page.getByRole("status").filter({ hasText: "Isolated:" });
  const fit = async () => {
    await settled(page);
    await page.getByRole("button", { name: "Fit to screen", exact: true }).click();
  };
  const shot = async (name: string) => {
    await settled(page);
    await page.screenshot({ path: testInfo.outputPath(name) });
  };
  const measured = async () => {
    const pane = await page
      .locator(".react-flow")
      .evaluate((el) => ({ width: (el as HTMLElement).offsetWidth, height: (el as HTMLElement).offsetHeight }));
    await expect(overviewEl).toHaveAttribute("data-pane", `${pane.width}x${pane.height}`);
    return pane;
  };
  const resolveAs = (state: OverviewState, pane: { width: number; height: number }) =>
    resolveOverview({ districts: ds, state, pane, pinned: [], links: adjacency });
  /** The map holds exactly the districts the model shows, open as frames or collapsed as summaries, and the repositories of the open ones. */
  const expectMap = async (r: ResolvedOverview) => {
    const open = r.shown.filter((d) => r.expanded.has(d.id));
    const shut = r.shown.filter((d) => !r.expanded.has(d.id));
    await expect.poll(() => nodeIds(".react-flow__node-district")).toEqual(open.map((d) => d.id).sort());
    await expect.poll(() => nodeIds(".react-flow__node-districtSummary")).toEqual(shut.map((d) => summaryId(d.id)).sort());
    await expect.poll(() => nodeIds(".react-flow__node-repository")).toEqual(open.flatMap((d) => d.members).sort());
  };

  await page.emulateMedia({ reducedMotion: "reduce" });

  // (1) one part of the tree, stated in the address as it is written there
  await page.goto("/?expand=tree%3At1-00%231");
  await ready(page);
  await settled(page);
  let pane = await measured();
  await fit();
  await expectMap(resolveAs({ expand: [FIRST], isolate: null }, pane));
  await expect.poll(() => nodeIds(".react-flow__node-district")).toEqual([FIRST]);
  expect(await search()).toBe("?expand=tree%3At1-00%231");
  await expect(overviewEl).not.toHaveAttribute("data-isolate", /.*/);
  const before = await entries();

  // (2) Isolate writes the whole and every part open, and is an entry to come back to
  await page.getByRole("button", { name: `Isolate ${iso.label}`, exact: true }).click();
  const isolated = query([
    ["isolate", token(WHOLE)],
    ["expand", partTokens.join(",")],
  ]);
  await expect.poll(search).toBe(isolated);
  expect(await entries(), "Isolate pushes an entry").toBe(before + 1);
  await expect(overviewEl).toHaveAttribute("data-isolate", WHOLE);

  // (3) the map is the core, then the districts linked to it; nothing of the rest
  pane = await measured();
  const r = resolveAs(readOverviewState(new URLSearchParams(isolated)), pane);
  expect(r.shown.map((d) => d.id)).toEqual([...iso.core, ...iso.neighbours]);
  await fit();
  await expectMap(r);

  // (4) the lines are those between what is shown
  const view = overviewView({ districts: ds, edges, expanded: r.expanded, shelf: r.shelf, visible: r.shown });
  expect(view.edges.length).toBeGreaterThan(0);
  await expect
    .poll(async () => new Set(await lineGroups.evaluateAll((gs) => gs.map((g) => g.getAttribute("data-link")))))
    .toEqual(new Set(view.edges.map((e) => e.id)));

  // (5) what goes to districts not shown is counted in the status
  const left = notDrawnText(notDrawn(view), 0);
  expect(left, "something goes to districts not shown").toContain("to districts not shown");
  await expect(page.getByTestId("status-note")).toHaveText(left!);

  // (6) a collapsed district says the same of itself
  const shut = r.shown.filter((d) => !r.expanded.has(d.id));
  expect(shut.some((d) => view.outside.has(d.id)), "a collapsed district with links to districts not shown").toBe(true);
  for (const d of shut) {
    await expect(page.locator(`.react-flow__node-districtSummary[data-id="${summaryId(d.id)}"]`)).toContainText(
      summaryLabel(summaryFacts(view, d, names)),
    );
  }

  // (7) the notice says what is shown, and has the way back
  await expect(notice).toHaveText(isolationText(iso));
  await expect(page.getByRole("button", { name: "Show all districts", exact: true })).toBeVisible();
  await shot("ecosystem-isolated.png");

  // (8) the isolated district is not offered for isolation again
  await expect(page.getByRole("button", { name: `Isolate ${iso.label}`, exact: true })).toHaveCount(0);

  // (9) closing a part is a replace: the isolation stays, and the part leaves `expand`
  await page.getByRole("button", { name: `Collapse ${parts[0]!.label}`, exact: true }).click();
  const closedOne = query([
    ["isolate", token(WHOLE)],
    ["expand", laterTokens.join(",")],
  ]);
  await expect.poll(search).toBe(closedOne);
  expect(await entries(), "Collapse replaces the entry").toBe(before + 1);

  // (10) Show all districts removes `isolate`, keeps `expand`, and is an entry of its own
  await page.getByRole("button", { name: "Show all districts", exact: true }).click();
  const everything = query([["expand", laterTokens.join(",")]]);
  await expect.poll(search).toBe(everything);
  expect(await entries(), "Show all pushes an entry").toBe(before + 2);
  await expect(notice).toHaveCount(0);
  await expect(overviewEl).not.toHaveAttribute("data-isolate", /.*/);
  pane = await measured();
  const whole = resolveAs(readOverviewState(new URLSearchParams(everything)), pane);
  expect(whole.shown.map((d) => d.id)).toEqual(ds.map((d) => d.id));
  await fit();
  await expectMap(whole);

  // (11) Back is the isolation as it was left, and Back again is the map the page opened on
  await page.goBack();
  await expect.poll(search).toBe(closedOne);
  await expect(overviewEl).toHaveAttribute("data-isolate", WHOLE);
  pane = await measured();
  await fit();
  await expectMap(resolveAs(readOverviewState(new URLSearchParams(closedOne)), pane));
  await page.goBack();
  await expect.poll(search).toBe("?expand=tree%3At1-00%231");
  await expect(overviewEl).not.toHaveAttribute("data-isolate", /.*/);
  pane = await measured();
  await fit();
  await expectMap(resolveAs({ expand: [FIRST], isolate: null }, pane));
  await expect.poll(() => nodeIds(".react-flow__node-district")).toEqual([FIRST]);

  // (12) an address that isolates and says nothing of what is open opens the core, with the default
  await page.goto("/?isolate=tree%3At1-00");
  await ready(page);
  await settled(page);
  await expect(overviewEl).toHaveAttribute("data-isolate", WHOLE);
  // the notice narrows the canvas, and the default follows the canvas: wait for the map to stop moving
  await expect(notice).toBeVisible();
  await settled(page);
  pane = await measured();
  const stated = resolveAs(readOverviewState(new URLSearchParams("isolate=tree%3At1-00")), pane);
  expect(stated.isolation).toEqual(iso);
  for (const id of iso.core) expect(stated.expanded.has(id), id).toBe(true);
  await fit();
  await expectMap(stated);
  await expect(notice).toHaveText(isolationText(iso));
  // no two districts or summaries overlap unless one is inside the other
  const clashes = await page
    .locator(".react-flow__node-district, .react-flow__node-districtSummary")
    .evaluateAll((nodes) => {
      const boxes = nodes.map((n) => ({ id: n.getAttribute("data-id") ?? "", r: n.getBoundingClientRect() }));
      const inside = (a: DOMRect, b: DOMRect) =>
        a.left >= b.left - 1 && a.right <= b.right + 1 && a.top >= b.top - 1 && a.bottom <= b.bottom + 1;
      const out: string[] = [];
      for (let i = 0; i < boxes.length; i++) {
        for (let j = i + 1; j < boxes.length; j++) {
          const a = boxes[i]!.r;
          const b = boxes[j]!.r;
          const across = a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1;
          if (across && !inside(a, b) && !inside(b, a)) out.push(`${boxes[i]!.id} / ${boxes[j]!.id}`);
        }
      }
      return out;
    });
  expect(clashes, "districts drawn over one another").toEqual([]);
  await shot("ecosystem-isolated-address.png");

  // (13) an address that names no district is the whole map, with no notice
  await page.goto("/?isolate=nope");
  await ready(page);
  await settled(page);
  pane = await measured();
  const nothing = resolveAs(readOverviewState(new URLSearchParams("isolate=nope")), pane);
  expect(nothing.isolation).toBeNull();
  expect(nothing.shown.map((d) => d.id)).toEqual(ds.map((d) => d.id));
  await expect(notice).toHaveCount(0);
  await expect(overviewEl).not.toHaveAttribute("data-isolate", /.*/);
  await fit();
  await expectMap(nothing);

  // (14) and nothing went wrong in the page
  expect(problems).toEqual([]);
});
