import type { Page } from "@playwright/test";

import { ask, expect, ready, test } from "./fixtures";

/**
 * The linked corpus (tests/topology_corpus.py): repositories that import from,
 * depend on and call each other. Every expectation is read from its API first,
 * so these check that the canvas draws what the index holds, kind by kind.
 */
const WORKSPACE = { scope: "global" } as const;

/** Drawn link lines of one kind; the legend's samples carry no emphasis. */
function lines(page: Page, kind: string) {
  return page.locator(`.react-flow__edges g[data-kind="${kind}"][data-emphasis]`);
}

test("the overview draws one line per link and kind, each in its own stroke", async ({ page, request, problems }, testInfo) => {
  const overview = await ask(request, "topology/overview", { context: WORKSPACE });
  const links = overview.links ?? [];
  expect(links.length).toBeGreaterThan(0);

  await page.goto("/");
  await ready(page);
  for (const kind of ["observed", "http", "dependency"] as const) {
    const expected = links.filter((l) => l.kind === kind && l.source !== l.target).length;
    await expect(lines(page, kind)).toHaveCount(expected);
  }

  // a dependency is a double line; HTTP is dashed; code is solid (React Flow adds
  // an invisible, wider path to each edge for the pointer: those are not counted)
  const strokes = (kind: string) => lines(page, kind).first().locator("path:not(.react-flow__edge-interaction)");
  await expect(strokes("dependency")).toHaveCount(2);
  const dash = (kind: string) => strokes(kind).first().evaluate((path) => getComputedStyle(path).strokeDasharray);
  expect(await dash("http")).not.toBe("none");
  expect(await dash("observed")).toBe("none");

  await page.screenshot({ path: testInfo.outputPath("linked-overview.png") });
  expect(problems).toEqual([]);
});

test("unlinked repositories are drawn too", async ({ page, request, problems }) => {
  const overview = await ask(request, "topology/overview", { context: WORKSPACE });
  const linked = new Set((overview.links ?? []).flatMap((l) => [l.source, l.target]));
  const { repositories = [] } = await ask(request, "repositories", { context: WORKSPACE });
  const independent = repositories.filter((r) => !linked.has(r.slug));
  expect(independent.length).toBeGreaterThan(0);

  await page.goto("/");
  await ready(page);
  for (const repository of independent) {
    await expect(page.getByRole("button", { name: `Open ${repository.name}`, exact: true })).toBeVisible();
  }
  expect(problems).toEqual([]);
});

test("hovering a repository lights its links and dims the rest", async ({ page, request, problems }) => {
  const overview = await ask(request, "topology/overview", { context: WORKSPACE });
  const counts = new Map<string, number>();
  for (const link of overview.links ?? []) {
    counts.set(link.source, (counts.get(link.source) ?? 0) + 1);
    counts.set(link.target, (counts.get(link.target) ?? 0) + 1);
  }
  const [busiest, touching] = [...counts.entries()].sort((a, b) => b[1] - a[1])[0]!;

  await page.goto("/");
  await ready(page);
  await page.getByRole("button", { name: `Open ${busiest}`, exact: true }).hover();
  await expect(page.locator('.react-flow__edges g[data-emphasis="lit"]')).toHaveCount(touching);
  await expect(page.locator('.react-flow__edges g[data-kind][data-emphasis="dim"]')).toHaveCount(
    (overview.links ?? []).length - touching,
  );
  expect(problems).toEqual([]);
});

test("an opened repository shows the repositories it links to, and opens them", async ({ page, request, problems }, testInfo) => {
  const overview = await ask(request, "topology/overview", { context: WORKSPACE });
  const source = (overview.links ?? [])[0]!.source;
  const root = await ask(request, "topology/children", {
    context: { scope: "repository", repository: source },
    path: "",
    cross_repository: true,
  });
  const peers = [
    ...new Set((root.links ?? []).filter((l) => l.peer.kind === "repository").map((l) => l.peer.repository!)),
  ];
  expect(peers.length).toBeGreaterThan(0);

  await page.goto(`/?repo=${encodeURIComponent(source)}`);
  await ready(page);
  for (const peer of peers) {
    await expect(page.getByRole("button", { name: `Open ${peer}`, exact: true })).toBeVisible();
  }
  await page.screenshot({ path: testInfo.outputPath("linked-opened.png") });

  await page.getByRole("button", { name: `Open ${peers[0]}`, exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`\\?repo=${encodeURIComponent(peers[0]!)}$`));
  await expect(page.getByRole("navigation", { name: "Location" })).toContainText(peers[0]!);
  expect(problems).toEqual([]);
});
