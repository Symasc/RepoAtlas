import AxeBuilder from "@axe-core/playwright";

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

for (const scheme of ["dark", "light"] as const) {
  test.describe(`${scheme} theme`, () => {
    test.use({ colorScheme: scheme });

    test("draws every repository and the workspace's totals", async ({ page, request, problems }, testInfo) => {
      const { repositories = [] } = await ask(request, "repositories", { context: WORKSPACE });
      await page.goto("/");
      await ready(page);

      await expect(page.locator("html")).toHaveAttribute("data-mantine-color-scheme", scheme);
      for (const repository of repositories) {
        await expect(page.getByRole("button", { name: `Open ${repository.name}`, exact: true })).toBeVisible();
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

    test("has no serious accessibility violations, closed or opened", async ({ page, request, problems }) => {
      const { repositories = [] } = await ask(request, "repositories", { context: WORKSPACE });
      for (const url of ["/", `/?repo=${encodeURIComponent(largest(repositories).slug)}`]) {
        await page.goto(url);
        await ready(page);
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

test("opening a repository shows what is in it, and Back climbs out", async ({ page, request, problems }, testInfo) => {
  const { repositories = [] } = await ask(request, "repositories", { context: WORKSPACE });
  const target = largest(repositories);
  const root = await ask(request, "topology/children", {
    context: { scope: "repository", repository: target.slug },
    path: "",
    cross_repository: true,
  });
  const directories = (root.entries ?? []).filter((e) => e.kind === "directory");

  await page.goto("/");
  await ready(page);
  await page.getByRole("button", { name: `Open ${target.name}`, exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`\\?repo=${literal(encodeURIComponent(target.slug))}$`));

  const location = page.getByRole("navigation", { name: "Location" });
  await expect(location).toContainText(target.name);
  for (const directory of directories) {
    await expect(page.getByRole("button", { name: new RegExp(`^Open ${literal(directory.name)}, `) })).toBeVisible();
  }
  if ((root.entries ?? []).some((e) => e.kind === "file")) {
    await expect(page.getByText("Files", { exact: true })).toBeVisible();
  }
  await page.screenshot({ path: testInfo.outputPath("opened.png") });

  test.skip(directories.length === 0, "the repository has no directories to open");
  const first = directories.sort((a, b) => a.name.localeCompare(b.name))[0]!;
  await page.getByRole("button", { name: new RegExp(`^Open ${literal(first.name)}, `) }).click();
  await expect(page).toHaveURL(new RegExp(`path=${literal(encodeURIComponent(first.path))}$`));
  await expect(page.getByRole("button", { name: `${first.name}, current directory` })).toBeVisible();
  await expect(location).toContainText(first.name);

  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`\\?repo=${literal(encodeURIComponent(target.slug))}$`));
  await page.goBack();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("button", { name: `Open ${target.name}`, exact: true })).toBeVisible();
  expect(problems).toEqual([]);
});

test("zooms in, out and back to fit", async ({ page, problems }) => {
  await page.goto("/");
  await ready(page);
  const level = page.getByLabel("Zoom level");
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
  await expect(page.getByRole("button", { name: `${target.name}, repository root` })).toBeVisible();
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

test.describe("narrower windows", () => {
  for (const width of [1280, 1024, 768, 390]) {
    test(`fit ${width}px without scrolling sideways`, async ({ page, request, problems }) => {
      const { repositories = [] } = await ask(request, "repositories", { context: WORKSPACE });
      await page.setViewportSize({ width, height: 900 });
      await page.goto("/");
      await ready(page);
      const { scrollWidth, innerWidth } = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        innerWidth: window.innerWidth,
      }));
      expect(scrollWidth).toBeLessThanOrEqual(innerWidth);
      // fitted to the window: every repository is on screen
      for (const repository of repositories) {
        await expect(page.getByRole("button", { name: `Open ${repository.name}`, exact: true })).toBeInViewport();
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
