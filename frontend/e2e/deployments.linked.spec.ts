import AxeBuilder from "@axe-core/playwright";

import { ask, expect, ready, test } from "./fixtures";

/**
 * A repository's Deployments tab: what its source states, and the links a person
 * records. The linked corpus states no deployment, so this records one, reads it
 * back through the API, edits it and removes it, leaving the index as it was.
 */
test("a deployment link is recorded, shown, edited and removed from the inspector", async ({ page, request, problems }) => {
  const overview = await ask(request, "topology/overview", { context: { scope: "global" } });
  const name = (overview.links ?? [])[0]!.source;

  await page.goto("/");
  await ready(page);
  await page.getByRole("button", { name: `Select ${name}`, exact: true }).click();
  const panel = page.getByRole("complementary", { name: `${name}, inspected` });
  await panel.getByRole("tab", { name: "Deployments" }).click();
  await expect(panel.getByRole("region", { name: "Your links" })).toBeVisible();

  await panel.getByRole("button", { name: "Add a link" }).click();
  const form = panel.getByRole("form", { name: "Add a deployment link" });
  await form.getByLabel("Label", { exact: false }).fill("staging");
  await form.getByLabel("URL").fill("https://web-staging.example.test/");
  await form.getByLabel("Conditions").fill("colour=blue");
  await form.getByRole("button", { name: "Add link" }).click();
  // a condition it cannot read is named, and nothing is sent
  await expect(form.getByRole("alert")).toContainText("colour");
  await form.getByLabel("Conditions").fill("branch=develop");
  await form.getByRole("button", { name: "Add link" }).click();

  const links = panel.getByRole("region", { name: "Your links" });
  const item = links.getByRole("listitem").filter({ has: page.getByRole("button", { name: "Remove staging" }) });
  await expect(item.getByText("https://web-staging.example.test/")).toBeVisible();
  await expect(item.getByText("unverified", { exact: true })).toBeVisible();
  const stored = await ask(request, "deployment_targets", { context: { scope: "repository", repository: name } });
  expect((stored.links ?? []).map((l) => [l.label, l.url, l.verification])).toEqual([
    ["staging", "https://web-staging.example.test/", "unverified"],
  ]);

  const results = await new AxeBuilder({ page }).include(`[aria-label="${name}, inspected"]`).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  expect(results.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => v.id)).toEqual([]);

  await links.getByRole("button", { name: "Edit staging" }).click();
  const edit = panel.getByRole("form", { name: "Edit the deployment link" });
  await edit.getByLabel("I confirmed this deployment myself").check();
  await edit.getByRole("button", { name: "Save link" }).click();
  await expect(item.getByText("confirmed by you", { exact: true })).toBeVisible();

  await links.getByRole("button", { name: "Remove staging" }).click();
  await expect(links.getByText("https://web-staging.example.test/")).toHaveCount(0);
  const after = await ask(request, "deployment_targets", { context: { scope: "repository", repository: name } });
  expect(after.links ?? []).toEqual([]);
  expect(problems).toEqual([]);
});
