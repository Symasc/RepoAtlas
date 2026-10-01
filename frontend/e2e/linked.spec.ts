import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import type { ImpactCandidate, ImpactResponse } from "@/lib/api/generated";
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

// -- reading a file's source and tracing a name in it --------------------------------

const POLICY = { repository: "app", path: "src/policy.ts" } as const;

/** A trace row's name, as the canvas reads it out. */
function rowName(c: ImpactCandidate): string {
  const line = Array.isArray(c.step.lines) ? c.step.lines[0] : null;
  return `${c.name}${typeof line === "number" ? `, line ${line}` : ""}, ${c.step.reading}`;
}

/** Repositories a trace stands in besides the seed's own. */
function otherRepositories(impact: ImpactResponse): number {
  const all = [...(impact.candidates ?? []), ...(impact.tests ?? [])].map((c) => c.repository);
  return new Set(all.filter((r): r is string => !!r && r !== impact.seed.repository)).size;
}

async function referencedElsewhere(request: Parameters<typeof ask>[0]) {
  const read = await ask(request, "topology/references", {
    context: { scope: "repository", repository: POLICY.repository },
    path: POLICY.path,
    cross_repository: true,
  });
  const use = (read.references ?? []).find((r) => r.target.repository_slug && r.target.repository_slug !== POLICY.repository && r.target.type !== "module");
  expect(use, "policy.ts uses a declaration from another repository").toBeTruthy();
  return use!.target;
}

test("a name in the source traces what a change to it reaches, file by file and across repositories", async ({ page, request, problems }, testInfo) => {
  const target = await referencedElsewhere(request);
  const impact = await ask(request, "impact", { context: WORKSPACE, seed: target.key, presentation: "full", max_candidates: 100 });
  const direct = (impact.candidates ?? []).filter((c) => c.distance === 1);
  const here = direct.find((c) => c.repository === POLICY.repository && c.path === POLICY.path);
  expect(here, "the trace comes back through policy.ts").toBeTruthy();
  const at = (here!.step.lines as number[])[0]!;

  await page.goto(`/?repo=${POLICY.repository}&path=${encodeURIComponent(POLICY.path)}`);
  await ready(page);
  const name = page
    .getByRole("region", { name: "Source of policy.ts" })
    .getByRole("button", { name: new RegExp(`^${literal(target.name ?? "")}, \\w+ in ${literal(target.repository_slug!)}$`) })
    .first();

  // a first press selects what the name stands for, a second follows its chain
  await name.click();
  await expect(page).toHaveURL(new RegExp(`sel=${literal(encodeURIComponent(target.key))}$`));
  await expect(page.getByRole("complementary", { name: `${target.name}, inspected` }).getByRole("button", { name: `Trace ${target.name}` })).toBeVisible();
  await name.click();
  await expect(page).toHaveURL(new RegExp(`trace=${literal(encodeURIComponent(target.key))}`));

  await expect(page.getByRole("button", { name: `${target.name}, what changes` })).toBeVisible();
  for (const c of direct) await expect(page.getByRole("button", { name: rowName(c) })).toBeVisible();
  await expect(page.getByText(new RegExp(`Crosses into ${otherRepositories(impact)} other repositor`))).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("trace.png") });

  // a step reads where it was asserted, and the package it crossed
  await page.getByRole("button", { name: rowName(here!) }).click();
  const step = page.getByRole("complementary", { name: `${here!.name}, inspected` });
  await expect(step).toContainText(`${here!.name} ${here!.step.reading} ${target.name} on line ${at}`);
  if (here!.step.crossing) await expect(step).toContainText(here!.step.crossing.package);
  await expect(step.getByRole("group", { name: `${POLICY.path}, lines ${at} to ${at}` })).toBeVisible();

  // a second press opens the file at that line, and Back climbs out the same way
  await step.getByRole("button", { name: `Open policy.ts at ${at}` }).click();
  await expect(page).toHaveURL(new RegExp(`path=${literal(encodeURIComponent(POLICY.path))}&sel=line${literal("%3A")}${at}$`));
  await expect(page.getByRole("region", { name: "Source of policy.ts" })).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`trace=.*&sel=c${literal("%3A")}${here!.id}$`));
  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`path=${literal(encodeURIComponent(POLICY.path))}&sel=${literal(encodeURIComponent(target.key))}$`));
  expect(problems).toEqual([]);
});

test("search finds a file by name and opens its source", async ({ page, request, problems }) => {
  const { files = [] } = await ask(request, "topology/files", { context: WORKSPACE, query: "policy" });
  const file = files[0]!;
  expect(file).toMatchObject({ repository: POLICY.repository, path: POLICY.path });

  await page.goto("/");
  await ready(page);
  await page.keyboard.press("ControlOrMeta+K");
  await page.getByRole("textbox", { name: "Search repositories and files" }).fill("policy");
  await expect(page.getByRole("button", { name: new RegExp(`^${literal(file.name)}`) })).toBeVisible();
  // the files arrive after the last keystroke: Enter still takes the first
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`\\?repo=${file.repository}&path=${literal(encodeURIComponent(file.path))}$`));
  await expect(page.getByRole("region", { name: `Source of ${file.name}` }).locator("pre")).toBeVisible();
  expect(problems).toEqual([]);
});

test("a directory traces into the repositories that use what it holds", async ({ page, request, problems }) => {
  const impact = await ask(request, "impact", { context: WORKSPACE, seed: "contracts:src/", presentation: "full", max_candidates: 100 });
  expect(impact.seed.status).toBe("resolved");
  expect(otherRepositories(impact)).toBeGreaterThan(1);

  await page.goto("/?repo=contracts");
  await ready(page);
  await page.getByRole("button", { name: /^Select src,/ }).click();
  await page.getByRole("button", { name: "Trace src" }).click();
  await expect(page).toHaveURL(/\?repo=contracts&trace=dir%3Asrc$/);
  await expect(page.getByText(new RegExp(`Crosses into ${otherRepositories(impact)} other repositories`))).toBeVisible();
  for (const c of (impact.candidates ?? []).filter((c) => c.distance === 1).slice(0, 3)) {
    await expect(page.getByRole("button", { name: rowName(c) }).first()).toBeVisible();
  }
  expect(problems).toEqual([]);
});

test("the path a level states goes straight to any step of it", async ({ page, problems }) => {
  const frame = page.locator(".react-flow__node-frame");
  const step = (where: typeof frame, name: string) => where.getByRole("button", { name, exact: true });

  // a directory's frame: every step but the one the reader is in
  await page.goto("/?repo=auth-kit&path=src%2Fauth");
  await ready(page);
  await expect(frame).toContainText("auth-kit / src/auth");
  await expect(step(frame, "auth")).toHaveCount(0);
  await step(frame, "src").click();
  await expect(page).toHaveURL(new RegExp(`\\?repo=auth-kit&path=src&sel=dir${literal("%3A")}${literal(encodeURIComponent("src/auth"))}$`));
  await step(frame, "auth-kit").click();
  await expect(page).toHaveURL(new RegExp(`\\?repo=auth-kit&sel=dir${literal("%3A")}src$`));

  // a file's own node, in Links: every step of the folder it is in
  await page.goto(`/?repo=${POLICY.repository}&path=${encodeURIComponent(POLICY.path)}`);
  await ready(page);
  await page.getByRole("radiogroup", { name: "Show" }).getByText("Links", { exact: true }).click();
  const anchor = page.locator(".react-flow__node-anchor");
  await expect(anchor.getByRole("button", { name: "policy.ts, this file" })).toBeVisible();
  await step(anchor, "src").click();
  await expect(page).toHaveURL(new RegExp(`\\?repo=${POLICY.repository}&path=src&sel=files$`));
  expect(problems).toEqual([]);
});

test("with variables included, search finds a name and opens on its trace", async ({ page, request, problems }) => {
  const { names = [] } = await ask(request, "topology/names", { context: WORKSPACE, query: "canEdit" });
  const found = names[0]!;
  expect(found.entity).toMatchObject({ repository_slug: POLICY.repository, path: POLICY.path });

  await page.goto("/");
  await ready(page);
  // off until it is chosen, and the choice outlasts a reload
  const beside = page.getByRole("banner").getByRole("switch", { name: "Include variables" });
  await expect(beside).not.toBeChecked();
  await beside.check();
  await expect(page.getByRole("banner").getByRole("button", { name: /^Search repositories, files and variables/ })).toBeVisible();
  // the switch keeps focus, and the shortcut still opens search
  await page.keyboard.press("ControlOrMeta+K");
  await expect(page.getByRole("dialog").getByRole("textbox", { name: "Search repositories, files and variables" })).toBeFocused();
  await page.keyboard.press("Escape");
  await page.reload();
  await ready(page);
  await expect(beside).toBeChecked();

  await page.keyboard.press("ControlOrMeta+K");
  const search = page.getByRole("dialog");
  await expect(search.getByRole("switch", { name: "Include variables" })).toBeChecked();
  await search.getByRole("textbox", { name: "Search repositories, files and variables" }).fill("canEdit");
  const action = search.getByRole("button", { name: new RegExp(`^${literal(found.qualified_name)}`) });
  await expect(action).toBeVisible();
  await action.click();
  await expect(page).toHaveURL(
    new RegExp(`\\?repo=${POLICY.repository}&path=${literal(encodeURIComponent(POLICY.path))}&trace=${literal(encodeURIComponent(found.entity.key))}$`),
  );
  await expect(page.getByRole("button", { name: `${found.entity.name}, what changes` })).toBeVisible();
  await expect(search).toHaveCount(0);

  // turned off inside the search itself, names leave it at once and the query stays
  await page.keyboard.press("ControlOrMeta+K");
  await search.getByRole("textbox", { name: "Search repositories, files and variables" }).fill("canEdit");
  await expect(action).toBeVisible();
  await search.getByRole("switch", { name: "Include variables" }).uncheck();
  await expect(action).toHaveCount(0);
  await expect(search.getByRole("textbox", { name: "Search repositories and files" })).toHaveValue("canEdit");
  await expect(beside).not.toBeChecked();
  expect(problems).toEqual([]);
});

for (const scheme of ["dark", "light"] as const) {
  test.describe(`${scheme} theme`, () => {
    test.use({ colorScheme: scheme });

    test("the source and a trace have no serious accessibility violations", async ({ page, request, problems }) => {
      const target = await referencedElsewhere(request);
      const file = `/?repo=${POLICY.repository}&path=${encodeURIComponent(POLICY.path)}`;
      for (const url of [file, `${file}&sel=${encodeURIComponent(target.key)}`, `${file}&trace=${encodeURIComponent(target.key)}`, `${file}&trace=${encodeURIComponent(target.key)}&sel=c%3A1`]) {
        await page.goto(url);
        await ready(page);
        await expect(url.includes("trace=") ? page.getByRole("button", { name: `${target.name}, what changes` }) : page.getByRole("region", { name: "Source of policy.ts" })).toBeVisible();
        const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
        const serious = results.violations
          .filter((v) => v.impact === "serious" || v.impact === "critical")
          .map((v) => `${url} ${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
        expect(serious).toEqual([]);
      }
      expect(problems).toEqual([]);
    });
  });
}
