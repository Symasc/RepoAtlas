import AxeBuilder from "@axe-core/playwright";
import type { Locator, Page } from "@playwright/test";

import { ask, expect, ready, test } from "./fixtures";

/**
 * External services on the topology corpus (the one `connections.linked.spec.ts` uses): `http-refused`
 * makes a request to `https://elsewhere.example`, which nothing identifies, and `http-pair.example`
 * is published by the source without that route. An operator classifies the unidentified host as an
 * external service, sees which requests would be shown as a known external dependency before saving,
 * finds the tag while the decision still reads http-service-unmatched, edits it, and revokes it, which
 * restores the Map affordance. A classification joins nothing; the index is left as it was found.
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

const elsewhere = (drawer: Locator) =>
  drawer.getByRole("list", { name: "Origins" }).getByRole("listitem", { name: ELSEWHERE });

// a failed test must not leave a classification behind for the next one
test.afterEach(async ({ request }) => {
  const left = await ask(request, "http_connections", { context: GLOBAL });
  for (const service of left.service_declarations ?? []) {
    await ask(request, "service_declarations/delete", { context: GLOBAL, id: service.id });
  }
});

test("classifying previews first, shows the tag while the decision stays unmatched, and revoking restores Map", async ({
  page,
  request,
  problems,
}) => {
  const before = await ask(request, "http_connections", { context: GLOBAL });
  expect(before.service_declarations ?? []).toEqual([]);
  expect(before.summary.known_external ?? 0).toBe(0);

  const drawer = await openWorklist(page);
  const group = elsewhere(drawer);
  await expect(group.getByRole("button", { name: `Map ${ELSEWHERE} to a service` })).toBeVisible();
  await expect(group.getByRole("list", { name: "Requests" })).toContainText("unresolved service identity");
  await group.getByRole("button", { name: `Classify ${ELSEWHERE} as external service` }).click();
  const form = group.getByRole("form", { name: `Classify ${ELSEWHERE} as external service` });

  // nothing is stored until the preview has said what would happen
  const save = form.getByRole("button", { name: "Save classification" });
  await expect(save).toBeDisabled();
  await expect(form).toContainText("Give the service a label.");
  await form.getByRole("textbox", { name: "Label" }).fill("Partner API");
  await form.getByRole("textbox", { name: "Provider" }).fill("Example Partner");
  await form.getByRole("textbox", { name: "Category" }).fill("partner");
  await form.getByRole("button", { name: "Add authentication target" }).click();
  await form.getByRole("combobox", { name: "Authenticates repository 1" }).click();
  await page.getByRole("option", { name: /^http-api/ }).click();
  await form.getByRole("textbox", { name: "Environment of target 1" }).fill("prod");

  const preview = form.getByRole("region", { name: "Preview" });
  await expect(preview).toContainText("would be shown as a known external dependency");
  await expect(preview).toContainText("their decision stays http-service-unmatched; nothing is joined");
  await expect(preview).toContainText("unresolved service identity → known external dependency");
  await expect(save).toBeEnabled();
  expect((await ask(request, "http_connections", { context: GLOBAL })).service_declarations ?? []).toEqual([]);

  const results = await new AxeBuilder({ page }).include('[role="dialog"]').withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  expect(results.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => v.id)).toEqual([]);

  await save.click();
  await expect(group.getByRole("status")).toContainText("Classified as external service #");

  // stored, applied at once, the operator's statement: no sync, and not one decision changed
  const stored = await ask(request, "http_connections", { context: GLOBAL });
  const [service] = stored.service_declarations ?? [];
  expect(service).toMatchObject({ state: "applied", basis: "declared" });
  expect(service!.fields).toMatchObject({
    origin: ELSEWHERE,
    label: "Partner API",
    provider: "Example Partner",
    authenticates: [{ repository: "http-api", environment: "prod" }],
  });
  expect(stored.summary.known_external).toBeGreaterThan(0);
  expect(stored.summary.unresolved).toBe(before.summary.unresolved);
  expect(stored.summary.by_status).toEqual(before.summary.by_status);
  expect(stored.summary.linked_by_declaration).toBe(before.summary.linked_by_declaration);

  // the tag appears while the decision still reads http-service-unmatched
  await expect(group).toContainText("known external dependency: Partner API");
  await expect(group).toContainText("http-service-unmatched");
  await expect(group.getByRole("list", { name: "Requests" })).toContainText("known external dependency");
  await expect(group.getByRole("list", { name: "Requests" })).toContainText("http-service-unmatched");
  await expect(group.getByRole("list", { name: "Requests" })).not.toContainText("joined by declaration");
  await expect(drawer.getByLabel("Summary")).toContainText("known external");
  await expect(drawer.getByLabel("Summary")).toContainText("unresolved identity");

  // authentication is the operator's statement, never read from the source
  const authenticates = group.getByRole("list", { name: "Authenticates" });
  await expect(authenticates).toContainText("http-api (prod)");
  await expect(authenticates).toContainText("operator-declared");
  await expect(authenticates).not.toContainText("source-backed");

  // Map is replaced by the reason, and Classify is gone
  await expect(group.getByRole("button", { name: /Map .* to a service/ })).toHaveCount(0);
  await expect(group.getByRole("button", { name: /Classify .* as external service/ })).toHaveCount(0);
  await expect(group).toContainText(
    `Classified as external service #${service!.id}; revoke it to map this origin to an indexed service.`,
  );

  // the identity filter narrows the worklist to the known external requests, and to the others
  const origins = drawer.getByRole("list", { name: "Origins" });
  await drawer.getByRole("combobox", { name: "Identity" }).click();
  await page.getByRole("option", { name: "Known external dependency" }).click();
  await expect(origins.locator("> li")).toHaveCount(1);
  await expect(elsewhere(drawer)).toBeVisible();
  await drawer.getByRole("combobox", { name: "Identity" }).click();
  await page.getByRole("option", { name: "Unresolved service identity" }).click();
  await expect(elsewhere(drawer)).toHaveCount(0);

  // its own listing: the origin is text, never a link, and the words are the operator's
  await drawer.getByRole("tab", { name: /Your declarations/ }).click();
  const item = drawer.getByRole("listitem", { name: `External service ${service!.id}` });
  await expect(item).toContainText("Partner API");
  await expect(item).toContainText(ELSEWHERE);
  await expect(item).toContainText("Example Partner");
  await expect(item).toContainText("http-api (prod)");
  await expect(item).toContainText("operator-declared");
  await expect(item.getByRole("link")).toHaveCount(0);

  // an edit keeps the origin and changes the words
  await item.getByRole("button", { name: `Edit external service ${service!.id}` }).click();
  const edit = item.getByRole("form", { name: `Edit external service ${service!.id}` });
  await expect(edit.getByRole("textbox", { name: "Label" })).toHaveValue("Partner API");
  await edit.getByRole("textbox", { name: "Label" }).fill("Partner API v2");
  await expect(edit.getByRole("region", { name: "Preview" })).toContainText("would be shown as a known external dependency");
  await edit.getByRole("button", { name: "Save changes" }).click();
  await expect(item.getByRole("status")).toContainText(`Updated external service #${service!.id}`);
  const edited = await ask(request, "http_connections", { context: GLOBAL });
  expect(edited.service_declarations?.[0]?.fields).toMatchObject({ origin: ELSEWHERE, label: "Partner API v2" });
  await item.getByRole("button", { name: "Close" }).click();

  // revoking stops the classification, and the history keeps the actions and the reason
  await item.getByRole("button", { name: `Revoke external service ${service!.id}` }).click();
  const revoke = item.getByRole("form", { name: `Revoke external service ${service!.id}` });
  await revoke.getByRole("textbox", { name: /Why/ }).fill("end of test");
  await revoke.getByRole("button", { name: `Confirm revoking external service ${service!.id}` }).click();
  await expect(drawer.getByText("No origin is classified as an external service.")).toBeVisible();

  const after = await ask(request, "http_connections", { context: GLOBAL });
  expect(after.service_declarations ?? []).toEqual([]);
  expect(after.summary.known_external ?? 0).toBe(0);
  expect(after.summary.unresolved).toBe(before.summary.unresolved);
  expect(after.summary.by_status).toEqual(before.summary.by_status);

  await drawer.getByRole("tab", { name: "History" }).click();
  const audit = drawer.getByRole("region", { name: "Audit of external services" });
  await expect(audit).toContainText(`declared external service #${service!.id} ${ELSEWHERE}`);
  await expect(audit).toContainText(`changed external service #${service!.id}`);
  await expect(audit).toContainText(`revoked external service #${service!.id}`);
  await expect(audit).toContainText("end of test");

  // the worklist offers Map again, and nothing says it is classified
  const reopened = elsewhere(await openWorklist(page));
  await expect(reopened.getByRole("button", { name: `Map ${ELSEWHERE} to a service` })).toBeVisible();
  await expect(reopened.getByRole("button", { name: `Classify ${ELSEWHERE} as external service` })).toBeVisible();
  await expect(reopened).not.toContainText("Classified as external service");
  await expect(reopened).not.toContainText("known external dependency");
  expect(problems).toEqual([]);
});

test("an origin the source publishes, and a base it does not state, offer no classification", async ({
  page,
  problems,
}) => {
  const drawer = await openWorklist(page);
  const origins = drawer.getByRole("list", { name: "Origins" });

  const published = origins.getByRole("listitem", { name: "https://http-pair.example" });
  await expect(published).toContainText("never overrides");
  await expect(published.getByRole("button", { name: /Classify .* as external service/ })).toHaveCount(0);

  const unstated = origins.getByRole("listitem", { name: /base not stated/ });
  await expect(unstated.getByRole("button", { name: /Classify .* as external service/ })).toHaveCount(0);
  expect(problems).toEqual([]);
});
