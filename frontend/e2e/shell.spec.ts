import AxeBuilder from "@axe-core/playwright";

import { formatCount } from "@/lib/model/format";

import { ask, expect, ready, test } from "./fixtures";

const WORKSPACE = { scope: "global" } as const;

function literal(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

for (const scheme of ["dark", "light"] as const) {
  test.describe(`${scheme} theme`, () => {
    test.use({ colorScheme: scheme });

    test("renders the live index", async ({ page, request, problems }, testInfo) => {
      const { repositories = [] } = await ask(request, "repositories", { context: WORKSPACE });
      await page.goto("/");
      await ready(page);

      await expect(page.locator("html")).toHaveAttribute("data-mantine-color-scheme", scheme);
      const list = page.getByRole("list", { name: "Repositories" });
      for (const repository of repositories) {
        await expect(list.getByRole("button", { name: new RegExp(`^${literal(repository.name)} `) })).toBeVisible();
      }
      const totals = page.getByLabel("Workspace totals");
      const files = repositories.reduce((sum, r) => sum + (r.indexed_files ?? 0), 0);
      const symbols = repositories.reduce((sum, r) => sum + (r.symbols ?? 0), 0);
      await expect(totals).toContainText(formatCount(files));
      await expect(totals).toContainText(formatCount(symbols));

      await page.screenshot({ path: testInfo.outputPath(`shell-${scheme}.png`) });
      expect(problems).toEqual([]);
    });

    test("has no serious accessibility violations", async ({ page, problems }) => {
      await page.goto("/");
      await ready(page);
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
      const serious = results.violations
        .filter((v) => v.impact === "serious" || v.impact === "critical")
        .map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
      expect(serious).toEqual([]);
      expect(problems).toEqual([]);
    });
  });
}

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

test("search finds a repository and opens it; Back returns", async ({ page, request, problems }) => {
  const { repositories = [] } = await ask(request, "repositories", { context: WORKSPACE });
  test.skip(repositories.length === 0, "nothing is indexed");
  const target = repositories[0]!;

  await page.goto("/");
  await ready(page);
  await page.keyboard.press("ControlOrMeta+K");
  await page.getByRole("textbox", { name: "Search repositories" }).fill(target.name);
  await page.keyboard.press("Enter");

  await expect(page).toHaveURL(new RegExp(`\\?repo=${literal(encodeURIComponent(target.slug))}$`));
  const inspector = page.getByRole("complementary", { name: "Inspector" });
  await expect(inspector.getByRole("heading", { level: 2 })).toHaveText(target.name);
  await expect(page.getByRole("navigation", { name: "Location" })).toContainText(target.name);

  await page.goBack();
  await expect(page).toHaveURL(/\/$/);
  await expect(inspector.getByRole("heading", { level: 2 })).toHaveText("Workspace");
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
    test(`fit ${width}px without scrolling sideways`, async ({ page, problems }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.goto("/");
      await ready(page);
      const { scrollWidth, innerWidth } = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        innerWidth: window.innerWidth,
      }));
      expect(scrollWidth).toBeLessThanOrEqual(innerWidth);

      const inspector = page.getByRole("complementary", { name: "Inspector" });
      const toggle = page.getByRole("button", { name: "Show details" });
      if (width >= 1024) {
        await expect(inspector).toBeInViewport();
        await expect(toggle).toBeHidden();
      } else {
        await expect(inspector).not.toBeInViewport();
        await toggle.click();
        await expect(inspector).toBeInViewport();
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
