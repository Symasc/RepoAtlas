import type { Page } from "@playwright/test";

import { ASPECT } from "@/lib/graph/layout";

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

function busiest(links: readonly { source: string; target: string }[]): [string, number] {
  const counts = new Map<string, number>();
  for (const link of links) {
    counts.set(link.source, (counts.get(link.source) ?? 0) + 1);
    counts.set(link.target, (counts.get(link.target) ?? 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0]!;
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

  // a dependency is a double line; HTTP is dashed; code is solid
  const strokes = (kind: string) => lines(page, kind).first().locator("path.react-flow__edge-path");
  await expect(strokes("dependency")).toHaveCount(2);
  const dash = (kind: string) => strokes(kind).first().evaluate((path) => getComputedStyle(path).strokeDasharray);
  expect(await dash("http")).not.toBe("none");
  expect(await dash("observed")).toBe("none");
  // every line says which way it runs
  await expect(lines(page, "observed").first().locator("path:not(.react-flow__edge-path):not(.react-flow__edge-interaction)")).toHaveCount(1);

  await page.screenshot({ path: testInfo.outputPath("linked-overview.png") });
  expect(problems).toEqual([]);
});

/** Every node's centre on screen, read in one go once nothing has moved for a moment. */
async function settled(page: Page): Promise<Record<string, { x: number; y: number }>> {
  const read = () =>
    page.locator(".react-flow__node").evaluateAll((nodes) =>
      Object.fromEntries(
        nodes.map((node) => {
          const box = node.getBoundingClientRect();
          return [(node as HTMLElement).dataset.id!, { x: box.x + box.width / 2, y: box.y + box.height / 2 }];
        }),
      ),
    );
  let last = JSON.stringify(await read());
  await expect
    .poll(async () => {
      const now = JSON.stringify(await read());
      const still = now === last && now !== "{}";
      last = now;
      return still;
    }, { intervals: [250] })
    .toBe(true);
  return JSON.parse(last) as Record<string, { x: number; y: number }>;
}

function literal(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

test("the repository with the most partners anchors the map, its partners closest", async ({ page, request, problems }) => {
  const overview = await ask(request, "topology/overview", { context: WORKSPACE });
  const links = overview.links ?? [];
  // most distinct partners, then most links, then by name
  const partnersOf = new Map<string, Set<string>>();
  const weight = new Map<string, number>();
  for (const l of links) {
    for (const [a, b] of [[l.source, l.target], [l.target, l.source]] as const) {
      partnersOf.set(a, (partnersOf.get(a) ?? new Set()).add(b));
      weight.set(a, (weight.get(a) ?? 0) + l.count);
    }
  }
  const anchor = [...partnersOf.keys()].sort(
    (a, b) => partnersOf.get(b)!.size - partnersOf.get(a)!.size || weight.get(b)! - weight.get(a)! || a.localeCompare(b),
  )[0]!;
  const partners = new Set(links.flatMap((l) => (l.source === anchor ? [l.target] : l.target === anchor ? [l.source] : [])));
  const others = new Set(links.flatMap((l) => [l.source, l.target]).filter((s) => s !== anchor && !partners.has(s)));

  await page.goto("/");
  await ready(page);
  const centres = await settled(page);
  const middle = centres[`repo:${anchor}`]!;
  // rings are ellipses: measured in ring units, a partner of a partner sits
  // on a ring further out, and another component apart from them all
  const distance = (name: string) => {
    const c = centres[`repo:${name}`]!;
    return Math.hypot((c.x - middle.x) / ASPECT, c.y - middle.y);
  };
  const near = Math.max(...[...partners].map(distance));
  for (const other of others) expect(distance(other), other).toBeGreaterThan(near);
  expect(problems).toEqual([]);
});

test("unlinked repositories are drawn too, in their own group", async ({ page, request, problems }) => {
  const overview = await ask(request, "topology/overview", { context: WORKSPACE });
  const linked = new Set((overview.links ?? []).flatMap((l) => [l.source, l.target]));
  const { repositories = [] } = await ask(request, "repositories", { context: WORKSPACE });
  const independent = repositories.filter((r) => !linked.has(r.slug));
  expect(independent.length).toBeGreaterThan(0);

  await page.goto("/");
  await ready(page);
  await expect(page.getByText(`No links recorded with the others · ${independent.length}`)).toBeVisible();
  for (const repository of independent) {
    await expect(page.getByRole("button", { name: `Select ${repository.name}`, exact: true })).toBeVisible();
  }
  expect(problems).toEqual([]);
});

test("pointing at a repository lights its links and fades the rest; selecting holds it", async ({ page, request, problems }) => {
  const overview = await ask(request, "topology/overview", { context: WORKSPACE });
  const links = overview.links ?? [];
  const [name, touching] = busiest(links);

  await page.goto("/");
  await ready(page);
  await page.getByRole("button", { name: `Select ${name}`, exact: true }).hover();
  await expect(page.locator('.react-flow__edges g[data-emphasis="lit"]')).toHaveCount(touching);
  await expect(page.locator('.react-flow__edges g[data-kind][data-emphasis="dim"]')).toHaveCount(links.length - touching);
  // count badges on the lit lines
  await expect(page.locator(".react-flow__edgelabel-renderer span")).toHaveCount(touching);

  await page.getByRole("button", { name: `Select ${name}`, exact: true }).click();
  await expect(page.getByRole("complementary", { name: `${name}, inspected` })).toBeVisible();
  // the pointer leaving does not undo a selection
  await page.mouse.move(2, 400);
  await expect(page.locator('.react-flow__edges g[data-emphasis="lit"]')).toHaveCount(touching);
  expect(problems).toEqual([]);
});

test("an opened repository draws a card's own links only when it is selected", async ({ page, request, problems }, testInfo) => {
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
  const card = (root.entries ?? []).find((e) => e.kind === "directory" && (root.links ?? []).some((l) => l.child === e.path));

  await page.goto(`/?repo=${encodeURIComponent(source)}`);
  await ready(page);
  for (const peer of peers) {
    await expect(page.getByRole("button", { name: `Select ${peer}`, exact: true })).toBeVisible();
  }
  // at rest: the level's totals, as lanes from its frame; no card's own lines
  await expect(page.locator('[data-testid*="=>"]').first()).toBeVisible();
  await expect(page.locator('.react-flow__edges g[data-emphasis="lit"]')).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("linked-opened.png") });

  if (card) {
    await page.getByRole("button", { name: new RegExp(`^Select ${literal(card.name)}, `) }).click();
    await expect(page.locator('.react-flow__edges g[data-emphasis="lit"]').first()).toBeVisible();
    await expect(page.locator('[data-testid*="=>"]')).toHaveCount(0);
    await page.keyboard.press("Escape");
  }

  await page.getByRole("button", { name: `Select ${peers[0]}`, exact: true }).click();
  await page.getByRole("button", { name: `Open ${peers[0]}`, exact: true }).first().click();
  await expect(page).toHaveURL(new RegExp(`\\?repo=${encodeURIComponent(peers[0]!)}$`));
  await expect(page.getByRole("navigation", { name: "Location" })).toContainText(peers[0]!);
  expect(problems).toEqual([]);
});

test("evidence for a link between repositories reads the lines that assert it", async ({ page, request, problems }) => {
  const overview = await ask(request, "topology/overview", { context: WORKSPACE });
  const link = (overview.links ?? []).find((l) => (l.examples ?? []).length > 0)!;

  await page.goto(`/?sel=${encodeURIComponent(`repo:${link.source}`)}`);
  await ready(page);
  const panel = page.getByRole("complementary", { name: `${link.source}, inspected` });
  await panel.locator("button[aria-pressed]").filter({ hasText: link.target }).first().click();
  await expect(panel.getByRole("tab", { name: "Evidence", selected: true })).toBeVisible();
  await expect(panel.locator("article").first()).toBeVisible();
  await expect(panel.locator("article pre").first()).toBeVisible();
  await expect(page.locator('.react-flow__edges g[data-emphasis="selected"]')).toHaveCount(1);
  expect(problems).toEqual([]);
});
