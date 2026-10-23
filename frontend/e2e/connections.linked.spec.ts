import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import { ask, expect, ready, test } from "./fixtures";

/**
 * HTTP connections on the linked corpus: `http-refused` makes three requests the join cannot link
 * (a host nothing identifies, a service published without that route, a base the source does not
 * state), and `http-api` serves `GET /items/<item_id>`. An operator maps the unidentified host to
 * the service, sees what the sync would do before confirming, finds the declaration pending (it
 * takes effect at the next sync), revokes it, and leaves the index as it was.
 */
const GLOBAL = { scope: "global" } as const;
const ELSEWHERE = "https://elsewhere.example";

async function openWorklist(page: Page) {
  await page.goto("/");
  await ready(page);
  await page.getByRole("button", { name: "unresolved HTTP requests" }).click();
  const drawer = page.getByRole("dialog", { name: "HTTP connections" });
  await expect(drawer).toBeVisible();
  return drawer;
}

test("the header totals count the unresolved requests and open the worklist", async ({ page, request, problems }) => {
  const stored = await ask(request, "http_connections", { context: GLOBAL });
  await page.goto("/");
  await ready(page);
  const totals = page.getByLabel("Workspace totals");
  await expect(totals).toContainText(`${stored.summary.unresolved}`);

  await totals.getByRole("button", { name: "unresolved HTTP requests" }).click();
  const drawer = page.getByRole("dialog", { name: "HTTP connections" });
  await expect(drawer.getByLabel("Summary")).toContainText(`${stored.summary.unresolved} unresolved request`);

  // every unresolved request is listed: a group per target, whether or not it can be mapped
  const origins = drawer.getByRole("list", { name: "Origins" });
  await expect(origins.getByRole("listitem", { name: ELSEWHERE })).toBeVisible();
  await expect(origins.getByRole("listitem", { name: "https://http-pair.example" })).toBeVisible();
  await expect(origins.getByRole("listitem", { name: /\{host\} \(base not stated\)/ })).toBeVisible();
  expect(problems).toEqual([]);
});

test("a stated origin can be mapped; the others say why they cannot", async ({ page, problems }) => {
  const drawer = await openWorklist(page);
  const origins = drawer.getByRole("list", { name: "Origins" });

  const elsewhere = origins.getByRole("listitem", { name: ELSEWHERE });
  await expect(elsewhere).toContainText("no_identity");
  await expect(elsewhere).toContainText("http-service-unmatched");
  await expect(elsewhere.getByRole("button", { name: `Map ${ELSEWHERE} to a service` })).toBeVisible();

  // the source published this host: a declaration never overrides it, so it offers no mapping
  const published = origins.getByRole("listitem", { name: "https://http-pair.example" });
  await expect(published).toContainText("never overrides");
  await expect(published.getByRole("button", { name: /Map .* to a service/ })).toHaveCount(0);

  // a base the source does not state is listed with where it is written, and is not mappable
  const unstated = origins.getByRole("listitem", { name: /base not stated/ });
  await expect(unstated).toContainText("host_not_stated");
  await expect(unstated).toContainText("no origin to map");
  await expect(unstated.getByRole("button", { name: /Map .* to a service/ })).toHaveCount(0);
  expect(problems).toEqual([]);
});

test("mapping previews each request, stores a pending declaration, and revoking restores the index", async ({
  page,
  request,
  problems,
}) => {
  const before = await ask(request, "http_connections", { context: GLOBAL });
  expect(before.declarations ?? []).toEqual([]);

  const drawer = await openWorklist(page);
  const group = drawer.getByRole("list", { name: "Origins" }).getByRole("listitem", { name: ELSEWHERE });
  await group.getByRole("button", { name: `Map ${ELSEWHERE} to a service` }).click();
  const form = group.getByRole("form", { name: `Map ${ELSEWHERE} to a service` });

  // nothing is declared until the preview has said what would happen
  const declare = form.getByRole("button", { name: /^Declare/ });
  await expect(declare).toBeDisabled();
  await form.getByRole("combobox", { name: "Service" }).click();
  await page.getByRole("option", { name: /^http-api/ }).click();
  const preview = form.getByRole("region", { name: "Preview" });
  await expect(preview).toContainText("1 would link");
  await expect(preview).toContainText("/items/");
  await expect(preview).toContainText("app.py:8");
  await expect(declare).toBeEnabled();
  expect((await ask(request, "http_connections", { context: GLOBAL })).declarations ?? []).toEqual([]);

  await form.getByRole("textbox", { name: "Note" }).fill("the public items API");
  const results = await new AxeBuilder({ page }).include('[role="dialog"]').withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  expect(results.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => v.id)).toEqual([]);
  await declare.click();
  await expect(group.getByRole("status")).toContainText("takes effect at the next sync");

  // stored, marked declared, pending: the sync has not run
  const stored = await ask(request, "http_connections", { context: GLOBAL });
  const [declaration] = stored.declarations ?? [];
  expect(declaration).toMatchObject({ repository: "http-api", url: ELSEWHERE, state: "pending", verification: "unverified" });
  const joined = await ask(request, "http_connections", { context: GLOBAL, include_resolved: true });
  expect(joined.summary.linked_by_declaration).toBe(0);

  await drawer.getByRole("tab", { name: /Your declarations/ }).click();
  const item = drawer.getByRole("listitem", { name: `Declaration ${declaration!.id}` });
  await expect(item).toContainText("Pending");
  await expect(item).toContainText(ELSEWHERE);
  await item.getByRole("button", { name: `Revoke ${declaration!.id}` }).click();
  const revoke = item.getByRole("form", { name: `Revoke declaration ${declaration!.id}` });
  await revoke.getByRole("textbox", { name: /Why/ }).fill("end of test");
  await revoke.getByRole("button", { name: `Revoke declaration ${declaration!.id}` }).click();
  await expect(drawer.getByText("You have declared nothing.")).toBeVisible();

  // revoked, and the history keeps both actions and the reason
  const after = await ask(request, "http_connections", { context: GLOBAL });
  expect(after.declarations ?? []).toEqual([]);
  await drawer.getByRole("tab", { name: "History" }).click();
  const history = drawer.getByRole("region", { name: "History" });
  await expect(history).toContainText("revoked");
  await expect(history).toContainText("declared");
  await expect(history).toContainText("end of test");
  await expect(history).toContainText("the public items API");
  expect(problems).toEqual([]);
});

test("the worklist filters by decision and by text", async ({ page, problems }) => {
  const drawer = await openWorklist(page);
  const origins = drawer.getByRole("list", { name: "Origins" });
  await drawer.getByRole("textbox", { name: "Origin contains" }).fill("elsewhere");
  await expect(origins.locator("> li")).toHaveCount(1);
  await expect(origins.getByRole("listitem", { name: ELSEWHERE })).toBeVisible();
  await drawer.getByRole("textbox", { name: "Origin contains" }).fill("");
  await drawer.getByRole("combobox", { name: "Decision" }).click();
  await page.getByRole("option", { name: /http-base-unknown/ }).click();
  await expect(origins.locator("> li")).toHaveCount(1);
  await expect(origins.locator("> li").first()).toContainText("base not stated");
  expect(problems).toEqual([]);
});
