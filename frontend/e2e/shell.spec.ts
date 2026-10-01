import AxeBuilder from "@axe-core/playwright";
import type { APIRequestContext, Page } from "@playwright/test";

import type { RepositoryView } from "@/lib/api/generated";
import { formatCount } from "@/lib/model/format";

import { ask, expect, ready, test } from "./fixtures";

const WORKSPACE = { scope: "global" } as const;

function literal(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** The repository with the most files: the richest one to open. */
function largest(repositories: RepositoryView[]): RepositoryView {
  return [...repositories].sort((a, b) => (b.indexed_files ?? 0) - (a.indexed_files ?? 0))[0]!;
}

function root(request: APIRequestContext, slug: string) {
  return ask(request, "topology/children", { context: { scope: "repository", repository: slug }, path: "", cross_repository: true });
}

/** A file at the root of `slug` with the most links, to open at file depth. */
async function linkedFile(request: APIRequestContext, slug: string): Promise<string | null> {
  const level = await root(request, slug);
  const files = new Set((level.entries ?? []).filter((e) => e.kind === "file").map((e) => e.path));
  const counts = new Map<string, number>();
  for (const link of level.links ?? []) {
    if (files.has(link.child)) counts.set(link.child, (counts.get(link.child) ?? 0) + link.count);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? [...files][0] ?? null;
}

function inspector(page: Page, name: string) {
  return page.getByRole("complementary", { name: `${name}, inspected` });
}

for (const scheme of ["dark", "light"] as const) {
  test.describe(`${scheme} theme`, () => {
    test.use({ colorScheme: scheme });

    test("draws every repository and the workspace's totals", async ({ page, request, problems }, testInfo) => {
      const { repositories = [] } = await ask(request, "repositories", { context: WORKSPACE });
      await page.goto("/");
      await ready(page);

      await expect(page.locator("html")).toHaveAttribute("data-mantine-color-scheme", scheme);
      for (const repository of repositories) {
        await expect(page.getByRole("button", { name: `Select ${repository.name}`, exact: true })).toBeVisible();
      }
      const totals = page.getByLabel("Workspace totals");
      await expect(totals).toContainText(formatCount(repositories.length));
      await expect(totals).toContainText(
        formatCount(repositories.reduce((sum, r) => sum + (r.indexed_files ?? 0), 0)),
      );
      await expect(page.getByRole("list", { name: "Legend" })).toContainText("Package dependency");

      await page.screenshot({ path: testInfo.outputPath(`overview-${scheme}.png`) });
      expect(problems).toEqual([]);
    });

    test("has no serious accessibility violations at any depth", async ({ page, request, problems }) => {
      const { repositories = [] } = await ask(request, "repositories", { context: WORKSPACE });
      const target = largest(repositories);
      const slug = encodeURIComponent(target.slug);
      const level = await root(request, target.slug);
      const directory = (level.entries ?? []).find((e) => e.kind === "directory");
      const file = await linkedFile(request, target.slug);
      const urls = [
        "/",
        `/?sel=${encodeURIComponent(`repo:${target.slug}`)}`,
        `/?repo=${slug}`,
        ...(directory ? [`/?repo=${slug}&sel=${encodeURIComponent(`dir:${directory.path}`)}`] : []),
        ...(file ? [`/?repo=${slug}&path=${encodeURIComponent(file)}`] : []),
      ];
      for (const url of urls) {
        await page.goto(url);
        await ready(page);
        await expect(page.locator(".react-flow__node").first()).toBeVisible();
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

test("a first press selects a repository, a second opens it, and Back climbs out onto it", async ({ page, request, problems }, testInfo) => {
  const { repositories = [] } = await ask(request, "repositories", { context: WORKSPACE });
  const target = largest(repositories);
  const slug = literal(encodeURIComponent(target.slug));
  const level = await root(request, target.slug);
  const directories = (level.entries ?? []).filter((e) => e.kind === "directory");

  await page.goto("/");
  await ready(page);
  await page.getByRole("button", { name: `Select ${target.name}`, exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`\\?sel=repo${literal("%3A")}${slug}$`));
  const panel = inspector(page, target.name);
  await expect(panel).toBeVisible();
  await expect(panel.getByRole("button", { name: `Open ${target.name}` })).toBeVisible();

  await page.getByRole("button", { name: `Open ${target.name}`, exact: true }).first().click();
  await expect(page).toHaveURL(new RegExp(`\\?repo=${slug}$`));
  const location = page.getByRole("navigation", { name: "Location" });
  await expect(location).toContainText(target.name);
  for (const directory of directories) {
    await expect(page.getByRole("button", { name: new RegExp(`^Select ${literal(directory.name)}, `) })).toBeVisible();
  }
  await page.screenshot({ path: testInfo.outputPath("opened.png") });

  test.skip(directories.length === 0, "the repository has no directories to open");
  const first = directories.sort((a, b) => a.name.localeCompare(b.name))[0]!;
  await page.getByRole("button", { name: new RegExp(`^Select ${literal(first.name)}, `) }).click();
  await expect(inspector(page, first.name)).toBeVisible();
  await page.getByRole("button", { name: `Open ${first.name}`, exact: true }).first().click();
  await expect(page).toHaveURL(new RegExp(`path=${literal(encodeURIComponent(first.path))}$`));
  await expect(location).toContainText(first.name);

  // Back lands on what was gone into, still selected
  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`\\?repo=${slug}&sel=dir${literal("%3A")}`));
  await expect(inspector(page, first.name)).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`\\?sel=repo${literal("%3A")}${slug}$`));
  await expect(page.getByRole("button", { name: `Open ${target.name}`, exact: true }).first()).toBeVisible();

  // Escape clears the selection
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: `Select ${target.name}`, exact: true })).toBeVisible();
  expect(problems).toEqual([]);
});

test("the location trail climbs out onto the step it came from", async ({ page, request, problems }) => {
  const { repositories = [] } = await ask(request, "repositories", { context: WORKSPACE });
  const target = largest(repositories);
  const directory = ((await root(request, target.slug)).entries ?? []).find((e) => e.kind === "directory");
  test.skip(directory === undefined, "the repository has no directories");

  await page.goto(`/?repo=${encodeURIComponent(target.slug)}&path=${encodeURIComponent(directory!.path)}`);
  await ready(page);
  await page.getByRole("navigation", { name: "Location" }).getByRole("button", { name: target.name }).click();
  await expect(page).toHaveURL(new RegExp(`sel=dir${literal("%3A")}${literal(encodeURIComponent(directory!.path))}$`));
  await expect(inspector(page, directory!.name)).toBeVisible();
  await page.getByRole("navigation", { name: "Location" }).getByRole("button", { name: "All repositories" }).click();
  await expect(page).toHaveURL(new RegExp(`\\?sel=repo${literal("%3A")}`));
  await expect(inspector(page, target.name)).toBeVisible();
  expect(problems).toEqual([]);
});

test("a file shows its neighbourhood, its source and the evidence for a connection", async ({ page, request, problems }, testInfo) => {
  const { repositories = [] } = await ask(request, "repositories", { context: WORKSPACE });
  const target = largest(repositories);
  const file = await linkedFile(request, target.slug);
  test.skip(file === null, "the repository has no files at its root");
  const name = file!.split("/").pop()!;
  const detail = await ask(request, "topology/children", {
    context: { scope: "repository", repository: target.slug },
    path: file!,
    cross_repository: true,
  });

  await page.goto(`/?repo=${encodeURIComponent(target.slug)}&path=${encodeURIComponent(file!)}`);
  await ready(page);
  const panel = inspector(page, name);
  await expect(panel).toBeVisible();
  await expect(page.getByRole("button", { name: `${name}, this file` })).toBeVisible();

  await panel.getByRole("tab", { name: "Code" }).click();
  await expect(panel).toContainText(/\d+ (of \d+ )?lines/);
  await panel.getByRole("tab", { name: "Details" }).click();
  await expect(panel).toContainText(file!);

  if ((detail.links ?? []).some((l) => l.peer.path !== file)) {
    await panel.getByRole("tab", { name: /^Connections/ }).click();
    await panel.locator("button[aria-pressed]").first().click();
    await expect(panel.getByRole("tab", { name: "Evidence", selected: true })).toBeVisible();
    await expect(panel).toContainText(/relationship/);
    await expect(page.locator('.react-flow__edges g[data-emphasis="selected"]')).toHaveCount(1);
  }
  await page.screenshot({ path: testInfo.outputPath("file.png") });
  expect(problems).toEqual([]);
});

test("zooms in, out and back to fit", async ({ page, problems }) => {
  await page.goto("/");
  await ready(page);
  const level = page.getByLabel("Zoom level");
  await expect(page.locator(".react-flow__node").first()).toBeVisible();
  const before = await level.textContent();
  await page.getByRole("button", { name: "Zoom in" }).click();
  await expect(level).not.toHaveText(before ?? "");
  await page.getByRole("button", { name: "Fit to screen" }).click();
  await expect(level).toHaveText(before ?? "");
  expect(problems).toEqual([]);
});

test("lists every repository's freshness and closes on Escape", async ({ page, request, problems }) => {
  const status = await ask(request, "status", { context: WORKSPACE, depth: "live", include_drift: false });
  await page.goto("/");
  await ready(page);

  const trigger = page.getByRole("button", { name: /^Index freshness/ });
  await trigger.click();
  const dialog = page.getByRole("dialog", { name: "Index freshness" });
  await expect(dialog).toBeVisible();
  for (const repository of status.freshness.repositories ?? []) {
    await expect(dialog).toContainText(repository.repository_name || repository.repository_slug);
  }
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
  expect(problems).toEqual([]);
});

test("search finds a repository and opens it", async ({ page, request, problems }) => {
  const { repositories = [] } = await ask(request, "repositories", { context: WORKSPACE });
  test.skip(repositories.length === 0, "nothing is indexed");
  const target = repositories[0]!;

  await page.goto("/");
  await ready(page);
  await page.keyboard.press("ControlOrMeta+K");
  await page.getByRole("textbox", { name: "Search repositories" }).fill(target.name);
  await page.keyboard.press("Enter");

  await expect(page).toHaveURL(new RegExp(`\\?repo=${literal(encodeURIComponent(target.slug))}$`));
  await expect(page.getByRole("navigation", { name: "Location" })).toContainText(target.name);
  expect(problems).toEqual([]);
});

test("a chosen theme survives a reload and hydrates cleanly", async ({ page, problems }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/");
  await ready(page);
  await page.getByRole("button", { name: "Switch to light theme" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-mantine-color-scheme", "light");

  await page.reload();
  await ready(page);
  await expect(page.locator("html")).toHaveAttribute("data-mantine-color-scheme", "light");
  await expect(page.getByRole("button", { name: "Switch to dark theme" })).toBeVisible();
  expect(problems).toEqual([]);
});

test("moves without animation when the reader asks for less motion", async ({ page, request, problems }) => {
  const { repositories = [] } = await ask(request, "repositories", { context: WORKSPACE });
  const target = largest(repositories);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`/?sel=${encodeURIComponent(`repo:${target.slug}`)}`);
  await ready(page);
  await page.getByRole("button", { name: `Open ${target.name}`, exact: true }).first().click();
  await expect(page).toHaveURL(new RegExp(`\\?repo=${literal(encodeURIComponent(target.slug))}$`));
  await expect(page.locator(".react-flow__node-frame")).toBeVisible();
  expect(problems).toEqual([]);
});

test.describe("narrower windows", () => {
  for (const width of [1280, 1024, 768, 390]) {
    test(`fit ${width}px without scrolling sideways`, async ({ page, request, problems }) => {
      const { repositories = [] } = await ask(request, "repositories", { context: WORKSPACE });
      await page.setViewportSize({ width, height: 900 });
      await page.goto("/");
      await ready(page);
      await expect(page.locator(".react-flow__node").first()).toBeVisible();
      const { scrollWidth, innerWidth } = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        innerWidth: window.innerWidth,
      }));
      expect(scrollWidth).toBeLessThanOrEqual(innerWidth);
      // fitted to the window: every repository is on screen
      for (const repository of repositories) {
        await expect(page.getByRole("button", { name: `Select ${repository.name}`, exact: true })).toBeInViewport();
      }
      expect(problems).toEqual([]);
    });
  }
});

test("explains a stopped API and how to start it", async ({ page }) => {
  // the one mocked case: the API this suite runs cannot also be stopped
  await page.route("**/api/ra/**", (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({
        ok: false,
        error: {
          code: "API_NOT_RUNNING",
          category: "context",
          message: "RepoAtlas's local API is not running.",
          retryable: false,
        },
      }),
    }),
  );
  await page.goto("/");
  const alert = page.getByRole("alert").filter({ hasText: "RepoAtlas isn't serving" });
  await expect(alert).toBeVisible();
  await expect(alert).toContainText("repoatlas serve");
  await expect(page.getByText("Not connected")).toBeVisible();
});
