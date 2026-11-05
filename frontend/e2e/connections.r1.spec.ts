import AxeBuilder from "@axe-core/playwright";
import type { APIRequestContext, Locator, Page } from "@playwright/test";

import { ask, expect, ready, test } from "./fixtures";

/**
 * Provider evidence in the HTTP Connections preview (R1-UI), on the R1 corpus
 * (tests/r1_e2e_corpus.py), through the real pipeline:
 *
 * - `buyer2` requests `GET /orders/42` and `GET /nothing/here` from `https://orders.example.test`.
 *   `pyprov` declares `/orders/{oid}` where nothing mounts it, `twinprov` declares two unmounted
 *   routes of that shape, and `emptyprov` recognizes no route at all.
 * - `buyer3` requests `/health` and `/shop/items/42` from `https://nest.example.test`. `nestok`
 *   serves both, one of them composed (no declaration line), `nestprov` imports `RouterModule`
 *   (its routes are withheld), and `fhelp` has a file at the Fastify helper-call cap.
 *
 * Nothing here declares anything: the preview is read-only, and what it adds is words. Every state
 * is read without a mock, and every unproven match is said to be a hypothesis.
 */
const GLOBAL = { scope: "global" } as const;
const ORDERS = "https://orders.example.test";
const NEST = "https://nest.example.test";
const HINTS = "Possible matches (not proven)";
/** What the interface must never say of a route the index merely did not prove. */
const IMPLIES_ABSENCE = /does not exist|no such route exists|\babsent\b/;

/** The worklist's groups, summary and declarations, as the API states them. */
async function worklist(request: APIRequestContext) {
  const listed = await ask(request, "http_connections", { context: GLOBAL });
  return { groups: listed.groups ?? [], summary: listed.summary, declarations: listed.declarations ?? [] };
}

/** Opens the Map form of one origin's group. Nothing is chosen. */
async function openMap(page: Page, origin: string) {
  await page.goto("/");
  await ready(page);
  await page.getByRole("button", { name: "unresolved HTTP requests" }).click();
  const drawer = page.getByRole("dialog", { name: "HTTP connections" });
  await expect(drawer).toBeVisible();
  const group = drawer.getByRole("list", { name: "Origins" }).getByRole("listitem", { name: origin, exact: true });
  await group.getByRole("button", { name: `Map ${origin} to a service` }).click();
  const form = group.getByRole("form", { name: `Map ${origin} to a service` });
  await expect(form).toBeVisible();
  await expect(form.getByRole("button", { name: /^Declare/ })).toBeDisabled();
  return { drawer, group, form };
}

/** Chooses the service in an open Map form. */
async function choose(page: Page, form: Locator, repository: string) {
  await form.getByRole("combobox", { name: "Service", exact: true }).click();
  await page.getByRole("option", { name: new RegExp(`^${repository}\\b`) }).click();
}

/** The row of one request in a preview, by the path it requests. */
function rowOf(preview: Locator, path: string) {
  return preview.getByRole("listitem").filter({ hasText: path });
}

async function accessible(page: Page) {
  const results = await new AxeBuilder({ page }).include('[role="dialog"]').withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  return results.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => v.id);
}

test("S1: a declared route nothing mounts is a hypothesis under its request, and the decision is unchanged", async ({
  page,
  request,
  problems,
}, testInfo) => {
  const before = await worklist(request);
  expect(before.declarations).toEqual([]);

  const { form } = await openMap(page, ORDERS);
  const declare = form.getByRole("button", { name: /^Declare/ });
  await choose(page, form, "pyprov");
  const preview = form.getByRole("region", { name: "Preview" });
  await expect(preview).toContainText("2 no matching route");
  await expect(preview).toContainText("pyprov serves 2 routes this index holds");

  // the request pyprov declares a route for: the same outcome as without the hint, and one hypothesis
  const orders = rowOf(preview, "/orders/42");
  await expect(orders).toHaveCount(1);
  await expect(orders).toContainText("no matching route");
  await expect(orders.locator('[data-good="true"]')).toHaveCount(0);
  const hints = orders.getByRole("list", { name: HINTS });
  await expect(hints).toHaveCount(1);
  const hint = hints.getByRole("listitem");
  await expect(hint).toHaveCount(1);
  await expect(hint).toContainText("not proven");
  await expect(hint).toContainText("GET /orders/{oid}");
  await expect(hint).toContainText("svc/orders.py");
  await expect(hint).toContainText("registration not proven");
  await expect(hint).toContainText("python-not-mounted");
  await expect(orders).toContainText("Possible matches — hypotheses, not connections");
  await expect(orders).toContainText("Nothing is joined through these; the decision above is unchanged.");
  // styled apart, and offering nothing to click
  await expect(orders.locator('[data-hypothesis="true"]')).toHaveCount(1);
  await expect(hints.getByRole("link")).toHaveCount(0);
  await expect(hints.getByRole("button")).toHaveCount(0);

  // the request nothing declares: no hint, and no claim that pyprov has no such route
  const nothing = rowOf(preview, "/nothing/here");
  await expect(nothing).toHaveCount(1);
  await expect(nothing).toContainText("no matching route");
  await expect(nothing.getByRole("list", { name: HINTS })).toHaveCount(0);
  await expect(nothing.locator('[data-hypothesis="true"]')).toHaveCount(0);
  await expect(nothing).toContainText(
    "No route this declaration can reach in pyprov, and no unproven declaration the index recognized there, matches this request.",
  );
  // pyprov also serves a route built from a prefix, which no declaration can reach: it may answer the request
  await expect(nothing).toContainText("could not be compared (no-declaration-line)");
  await expect(nothing).toContainText("this is not proof that pyprov has no such route");
  await expect(preview).toContainText(
    "'no matching route' means no route this index proves pyprov serves matches the request. It is not proof that pyprov has no such route.",
  );

  // what the index recognized in pyprov and what became of it
  const evidence = preview.getByRole("region", { name: "Provider evidence" });
  await expect(evidence).toContainText("8 route declarations recognized");
  await expect(evidence).toContainText("2 served");
  await expect(evidence).toContainText(
    "3 declarations: found in source; no proven application registers it (registration not proven)",
  );
  await expect(evidence).toContainText(
    "2 declarations: found in source; the application or router it belongs to could not be identified",
  );
  await expect(evidence).toContainText("1 declaration: found in source; its method or full path could not be resolved");

  // the words, and no success styling anywhere in the preview
  await expect(preview).not.toContainText(IMPLIES_ABSENCE);
  await expect(preview.locator('[data-good="true"]')).toHaveCount(0);

  // decisions: the preview cannot declare, and nothing was stored or changed by reading it
  await expect(declare).toBeDisabled();
  await preview.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("s1-hypothesis-under-request.png") });
  expect(await accessible(page)).toEqual([]);

  const after = await worklist(request);
  expect(after.declarations).toEqual([]);
  expect(JSON.stringify(after.groups)).toBe(JSON.stringify(before.groups));
  expect(JSON.stringify(after.summary)).toBe(JSON.stringify(before.summary));
  expect(problems).toEqual([]);
});

test("S2: a repository with no recognized route says so, as no proof, and has no hint", async ({ page, request, problems }, testInfo) => {
  const before = await worklist(request);
  const { form } = await openMap(page, ORDERS);
  await choose(page, form, "emptyprov");

  const preview = form.getByRole("region", { name: "Preview" });
  // the backend's own requirement, unchanged
  await expect(form.getByLabel("What must change first")).toContainText(
    "emptyprov serves no route this index holds: it is not an indexed service",
  );
  const evidence = preview.getByRole("region", { name: "Provider evidence" });
  await expect(evidence).toContainText("0 route declarations recognized");
  await expect(evidence).toContainText("No route declaration was recognized in emptyprov's indexed source");
  await expect(evidence).toContainText("this is not proof it serves none");
  await expect(preview.getByRole("list", { name: HINTS })).toHaveCount(0);
  await expect(preview.locator('[data-hypothesis="true"]')).toHaveCount(0);
  await expect(preview).not.toContainText(IMPLIES_ABSENCE);
  await expect(form.getByRole("button", { name: /^Declare/ })).toBeDisabled();

  await preview.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("s2-no-recognized-route.png") });
  expect(await accessible(page)).toEqual([]);
  expect(JSON.stringify((await worklist(request)).groups)).toBe(JSON.stringify(before.groups));
  expect(problems).toEqual([]);
});

test("S3: a served route with no declaration line is stated as out of this declaration's reach", async ({ page, request, problems }, testInfo) => {
  const before = await worklist(request);
  const { form } = await openMap(page, NEST);
  await choose(page, form, "nestok");

  const preview = form.getByRole("region", { name: "Preview" });
  // unchanged by the diagnostics: one request a route with lines answers, one a composed route does
  await expect(preview).toContainText("1 would link");
  await expect(preview).toContainText("1 no matching route");
  const evidence = preview.getByRole("region", { name: "Provider evidence" });
  await expect(evidence).toContainText(
    /1 route nestok serves is built from a prefix and states no single source line, so a declaration cannot reach it \(no-declaration-line\)\./,
  );
  // the request that matched nothing says a route it could not compare may answer it, and no longer says
  // the index recognizes no route there
  const unmatched = preview.getByRole("listitem").filter({ hasText: "no matching route" });
  await expect(unmatched).toHaveCount(1);
  await expect(unmatched).toContainText("could not be compared (no-declaration-line)");
  await expect(unmatched).not.toContainText("No route this index recognizes");
  // nothing is declared unrecognized here, so no hypothesis is offered
  await expect(preview.getByRole("list", { name: HINTS })).toHaveCount(0);
  await expect(preview).not.toContainText(IMPLIES_ABSENCE);

  await preview.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("s3-no-declaration-line.png") });
  expect(await accessible(page)).toEqual([]);
  expect(JSON.stringify((await worklist(request)).groups)).toBe(JSON.stringify(before.groups));
  expect(problems).toEqual([]);
});

test("S4: several unproven matches are all listed, each one a hypothesis", async ({ page, problems }) => {
  const { form } = await openMap(page, ORDERS);
  await choose(page, form, "twinprov");

  const preview = form.getByRole("region", { name: "Preview" });
  const orders = rowOf(preview, "/orders/42");
  await expect(orders).toHaveCount(1);
  await expect(orders).toContainText("no matching route");
  const hints = orders.getByRole("list", { name: HINTS });
  await expect(hints).toHaveCount(1);
  const items = hints.getByRole("listitem");
  await expect(items).toHaveCount(2);
  for (const item of await items.all()) await expect(item).toContainText("not proven");
  await expect(hints).toContainText("tw/a.py");
  await expect(hints).toContainText("tw/b.py");
  await expect(preview.locator('[data-good="true"]')).toHaveCount(0);
  await expect(preview).not.toContainText(IMPLIES_ABSENCE);
  await expect(form.getByRole("button", { name: /^Declare/ })).toBeDisabled();
  expect(problems).toEqual([]);
});

test("S5: a framework rule and an analysis limit are each named, with their codes", async ({ page, problems }) => {
  // a framework constraint: NestJS's RouterModule withholds the application's routes
  const withheld = await openMap(page, NEST);
  await choose(page, withheld.form, "nestprov");
  const framework = withheld.form.getByRole("region", { name: "Preview" }).getByRole("region", { name: "Provider evidence" });
  await expect(framework).toContainText("found in source; withheld by a framework rule RepoAtlas applies");
  await expect(framework).toContainText("nestjs-router-module");
  await expect(framework).not.toContainText(IMPLIES_ABSENCE);
  await withheld.form.getByRole("button", { name: "Cancel" }).click();
  await expect(withheld.form).toHaveCount(0);

  // an analysis bound: a file at the Fastify helper-call cap
  const bounded = await openMap(page, NEST);
  await choose(page, bounded.form, "fhelp");
  const preview = bounded.form.getByRole("region", { name: "Preview" });
  const limits = preview.getByRole("region", { name: "Provider evidence" });
  await expect(limits).toContainText("found in source; an analysis limit was reached before it could be proven");
  await expect(limits).toContainText("Analysis limits were reached in fhelp; these counts may be incomplete.");
  await expect(limits.getByRole("list", { name: "Analysis limits reached" })).toContainText("fastify-helper-calls-per-file");
  await expect(preview).not.toContainText(IMPLIES_ABSENCE);
  expect(problems).toEqual([]);
});
