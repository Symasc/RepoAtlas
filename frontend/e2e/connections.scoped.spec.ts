import { execFileSync } from "node:child_process";

import AxeBuilder from "@axe-core/playwright";
import type { APIRequestContext, Locator, Page } from "@playwright/test";

import { PYTHON, REPOSITORY, SCOPED_ENV } from "../playwright.config";
import { ask, expect, ready, test } from "./fixtures";

/**
 * Scoped HTTP declarations on the scoped corpus (tests/scoped_e2e_corpus.py): `itemcaller` makes the
 * same request from `client.py` and from `other.py`, each on a module binding `BASE` the source never
 * states, and `itemservice` serves `GET /v1/items/{item_id}`. An operator declares which service
 * answers `BASE` in `client.py`: picks the scope and the service, reads the preview, declares, and the
 * request joins at the next sync, marked as the operator's statement. The same base text in `other.py`
 * is outside the scope and stays unresolved. Revoking restores the index at the next sync.
 *
 * The explorer cannot sync (the proxy refuses it), so the test runs `repoatlas sync` on the corpus's
 * own index, as an operator would at a terminal.
 */
const GLOBAL = { scope: "global" } as const;
const SERVICE = "itemservice";
const CALLER = "itemcaller";
const GROUP = "{BASE} (base not stated)";
const SCOPE_BUTTON = "Declare the base of {BASE} for a scope";
/** The stated-origin mapping, which a base the source does not state must never offer. */
const MAP_BUTTON = /Map .* to a service/;
const DOT = "·";

/** One ordinary sync of the whole scoped workspace, in a process of its own like the CLI. */
function sync(): void {
  execFileSync(PYTHON, ["-m", "repoatlas.cli.main", "sync"], {
    cwd: REPOSITORY,
    env: { ...process.env, ...SCOPED_ENV },
    stdio: "pipe",
    timeout: 120_000,
  });
}

async function openWorklist(page: Page) {
  await page.goto("/");
  await ready(page);
  await page.getByRole("button", { name: "unresolved HTTP requests" }).click();
  const drawer = page.getByRole("dialog", { name: "HTTP connections" });
  await expect(drawer).toBeVisible();
  return drawer;
}

function groupIn(drawer: Locator) {
  return drawer.getByRole("list", { name: "Origins" }).getByRole("listitem", { name: GROUP, exact: true });
}

/** What the API says of the worklist: its summary, the operator's declarations, the one unstated-base group. */
async function worklist(request: APIRequestContext) {
  const listed = await ask(request, "http_connections", { context: GLOBAL });
  const groups = (listed.groups ?? []).filter((g) => g.kind === "base-not-stated");
  expect(groups.map((g) => g.base_expression)).toEqual(["{BASE}"]);
  return { summary: listed.summary, declarations: listed.declarations ?? [], group: groups[0]! };
}

/** Every request of the group, joined or not, by file: where it is, what the join decided, and by which declaration. */
async function requestsOf(request: APIRequestContext, group: string) {
  const read = await ask(request, "http_connections", { context: GLOBAL, group, include_resolved: true });
  return (read.requests ?? [])
    .map((r) => ({ path: r.site.path, line: r.site.line_start ?? null, status: r.status, joinedBy: r.joined_by ?? null }))
    .sort((a, b) => a.path.localeCompare(b.path));
}

/** The service's handler, the seed of an impact that crosses into the caller. */
async function handlerOf(request: APIRequestContext) {
  const { names = [] } = await ask(request, "topology/names", { context: GLOBAL, query: "get_item" });
  const found = names.find((n) => n.entity.repository_slug === SERVICE && n.qualified_name === "get_item");
  expect(found, "the service's handler is indexed").toBeTruthy();
  return found!.entity;
}

/** The steps of a change to the handler that cross an HTTP request into a caller. */
async function crossingsInto(request: APIRequestContext, seed: string) {
  const impact = await ask(request, "impact", { context: GLOBAL, seed, presentation: "full", max_candidates: 100 });
  expect(impact.seed.status).toBe("resolved");
  return (impact.candidates ?? []).filter((c) => c.step.http);
}

test("a base the source does not state offers a scope, never a mapping, and nothing is preselected", async ({ page, request, problems }) => {
  const start = await worklist(request);
  expect(start.summary).toMatchObject({ unresolved: 2, base_not_stated: 2, linked_by_declaration: 0 });
  expect(start.declarations).toEqual([]);
  expect(start.group).toMatchObject({ requests: 2, callers: [CALLER], declarable: 0 });

  const drawer = await openWorklist(page);
  const group = groupIn(drawer);
  await expect(group).toContainText("2 requests");
  await expect(group).toContainText("host_not_stated");
  // the source states no origin, so there is none to map; the control is a scope, named as one
  await expect(group).toContainText("no origin to map");
  await expect(group.getByRole("button", { name: MAP_BUTTON })).toHaveCount(0);
  const scope = group.getByRole("button", { name: SCOPE_BUTTON });
  await expect(scope).toBeVisible();
  expect(await scope.getAttribute("aria-label")).not.toMatch(MAP_BUTTON);

  // the server offers the files that write the base; neither the scope nor the service is chosen for the operator
  await scope.click();
  const form = group.getByRole("form", { name: SCOPE_BUTTON });
  await expect(form.getByRole("radio", { name: new RegExp(`client\\.py ${DOT} whole file`) })).toBeVisible();
  await expect(form.getByRole("radio", { name: new RegExp(`other\\.py ${DOT} whole file`) })).toBeVisible();
  for (const radio of await form.getByRole("radio").all()) await expect(radio).not.toBeChecked();
  await expect(form.getByRole("combobox", { name: "Service", exact: true })).toHaveValue("");
  await expect(form.getByRole("button", { name: /^Declare for this scope/ })).toBeDisabled();
  await form.getByRole("button", { name: "Cancel" }).click();
  await expect(form).toHaveCount(0);

  expect((await worklist(request)).declarations).toEqual([]);
  expect(problems).toEqual([]);
});

test("a scope is previewed, declared, joins one file's request at the next sync, and revoking restores the index", async ({
  page,
  request,
  problems,
}) => {
  test.setTimeout(240_000);

  // -- before: both requests unresolved, no declaration, no crossing into the service's handler
  const start = await worklist(request);
  expect(start.summary).toMatchObject({ unresolved: 2, base_not_stated: 2, linked_by_declaration: 0 });
  expect(start.declarations).toEqual([]);
  const before = await requestsOf(request, start.group.key);
  expect(before.map((r) => [r.path, r.status, r.joinedBy])).toEqual([
    ["client.py", "http-base-unknown", null],
    ["other.py", "http-base-unknown", null],
  ]);
  const clientLine = before[0]!.line;
  expect(clientLine, "the request in client.py is placed").not.toBeNull();
  const handler = await handlerOf(request);
  expect(await crossingsInto(request, handler.key)).toEqual([]);

  // -- scope selection and preview: nothing is declarable until both are chosen and the preview has said what would happen
  const drawer = await openWorklist(page);
  const group = groupIn(drawer);
  await group.getByRole("button", { name: SCOPE_BUTTON }).click();
  const form = group.getByRole("form", { name: SCOPE_BUTTON });
  const declare = form.getByRole("button", { name: /^Declare for this scope/ });
  await expect(declare).toBeDisabled();
  await form.getByRole("radio", { name: new RegExp(`client\\.py ${DOT} whole file`) }).check();
  await expect(declare).toBeDisabled();
  await form.getByRole("combobox", { name: "Service", exact: true }).click();
  await page.getByRole("option", { name: new RegExp(`^${SERVICE}`) }).click();

  const preview = form.getByRole("region", { name: "Preview" });
  await expect(preview).toContainText("1 would link");
  // the same base text in the other file is listed, and said to be outside the scope
  await expect(preview).toContainText("1 outside the scope");
  const inside = preview.getByRole("listitem").filter({ hasText: "client.py" });
  await expect(inside).toContainText("would link");
  await expect(inside).toContainText(`client.py:${clientLine}`);
  await expect(inside).toContainText("/v1/items/");
  const outside = preview.getByRole("listitem").filter({ hasText: "other.py" });
  await expect(outside).toContainText("outside the scope");
  await expect(declare).toBeEnabled();
  expect((await worklist(request)).declarations).toEqual([]);

  const results = await new AxeBuilder({ page }).include('[role="dialog"]').withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  expect(results.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => v.id)).toEqual([]);

  // -- declaration: stored, marked declared and pending; the join has not run
  await form.getByRole("textbox", { name: "Note", exact: true }).fill("the items API, for client.py");
  await declare.click();
  const confirmed = group.getByRole("status");
  await expect(confirmed).toContainText("takes effect at the next sync");
  await expect(confirmed).toContainText(`${SERVICE} serves {BASE} in client.py`);
  await expect(drawer.getByText("1 declaration is pending")).toBeVisible();

  const pending = await worklist(request);
  expect(pending.declarations).toHaveLength(1);
  const declaration = pending.declarations[0]!;
  expect(declaration).toMatchObject({
    repository: SERVICE,
    state: "pending",
    verification: "unverified",
    callers: [CALLER],
    scope: { path: "client.py", base_shown: "{BASE}" },
  });
  expect(declaration.scope?.symbol ?? null).toBeNull();
  expect(declaration.url ?? null).toBeNull();
  expect(pending.summary).toMatchObject({ unresolved: 2, linked_by_declaration: 0 });
  expect(await requestsOf(request, pending.group.key)).toEqual(before);
  expect(await crossingsInto(request, handler.key)).toEqual([]);

  await drawer.getByRole("tab", { name: /Your declarations/ }).click();
  const stated = drawer.getByRole("listitem", { name: `Declaration ${declaration.id}` });
  await expect(stated).toContainText("Pending");
  await expect(stated).toContainText(["Scope: file client.py", "whole file", "base {BASE}", "no base path"].join(` ${DOT} `));
  await expect(stated).toContainText(`for ${CALLER}`);

  // -- the next sync: client.py's request joins the service's handler, other.py's does not
  sync();
  const joined = await worklist(request);
  expect(joined.group.key).toBe(start.group.key);
  expect(joined.summary).toMatchObject({ unresolved: 1, linked_by_declaration: 1 });
  expect(joined.declarations.map((d) => [d.id, d.state, d.joined])).toEqual([[declaration.id, "applied", 1]]);
  expect((await requestsOf(request, joined.group.key)).map((r) => [r.path, r.status, r.joinedBy])).toEqual([
    ["client.py", "linked", declaration.id],
    ["other.py", "http-base-unknown", null],
  ]);

  // -- impact: the crossing exists, and says the operator, not the source, identified the service.
  // The explorer's trace lists the step but does not render `operator_declared`, so that is read from the impact API.
  const crossings = await crossingsInto(request, handler.key);
  expect(crossings.map((c) => [c.repository, c.path, c.name])).toEqual([[CALLER, "client.py", "read_item"]]);
  const http = crossings[0]!.step.http!;
  expect(http).toMatchObject({
    operator_declared: true,
    identity_source: "deployment-link",
    provider_repository: SERVICE,
    consumer_repository: CALLER,
  });
  expect(http.declaration).toMatch(new RegExp(`^link ${declaration.id}: .*, unconfirmed$`));

  await page.goto(`/?repo=${SERVICE}&path=${encodeURIComponent(handler.path ?? "")}&trace=${encodeURIComponent(handler.key)}`);
  await ready(page);
  await expect(page.getByRole("button", { name: "get_item, what changes" })).toBeVisible();
  await expect(page.getByRole("button", { name: /^read_item, / }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: /^other_read, / })).toHaveCount(0);

  // -- the worklist after the sync: the declaration is applied; the request outside the scope is still there, unjoined
  const synced = await openWorklist(page);
  await expect(synced.getByLabel("Summary")).toContainText("1 unresolved request");
  await expect(synced.getByLabel("Summary")).toContainText("1 joined by your declarations");
  const remaining = groupIn(synced);
  await expect(remaining).toContainText("1 request");
  await expect(remaining).toContainText(`Declaration #${declaration.id}: ${SERVICE} at {BASE} in client.py`);
  await expect(remaining.getByRole("button", { name: /^Open other\.py line \d+$/ })).toBeVisible();
  await expect(remaining.getByRole("button", { name: /^Open client\.py line/ })).toHaveCount(0);
  await expect(remaining).not.toContainText("joined by declaration");

  await synced.getByRole("tab", { name: /Your declarations/ }).click();
  const applied = synced.getByRole("listitem", { name: `Declaration ${declaration.id}` });
  await expect(applied).toContainText("Applied");
  await expect(applied).toContainText("1 request joined");

  // -- revoke: the declaration is gone at once, and the history keeps both actions and the reason
  await applied.getByRole("button", { name: `Revoke ${declaration.id}` }).click();
  const revoke = applied.getByRole("form", { name: `Revoke declaration ${declaration.id}` });
  await revoke.getByRole("textbox", { name: /Why/ }).fill("end of test");
  await revoke.getByRole("button", { name: `Revoke declaration ${declaration.id}` }).click();
  await expect(synced.getByText("You have declared nothing.")).toBeVisible();
  expect((await worklist(request)).declarations).toEqual([]);

  // -- the next sync: unresolved again, exactly as before the declaration
  sync();
  const restored = await worklist(request);
  expect(restored.group.key).toBe(start.group.key);
  expect(restored.summary).toEqual(start.summary);
  expect(restored.declarations).toEqual([]);
  expect(await requestsOf(request, restored.group.key)).toEqual(before);
  expect(await crossingsInto(request, handler.key)).toEqual([]);

  const final = await openWorklist(page);
  await expect(final.getByLabel("Summary")).toContainText("2 unresolved requests");
  await expect(final.getByLabel("Summary")).not.toContainText("joined by your declarations");
  const again = groupIn(final);
  await expect(again).toContainText("2 requests");
  await expect(again).not.toContainText("Declaration #");
  await expect(again.getByRole("button", { name: SCOPE_BUTTON })).toBeVisible();
  await final.getByRole("tab", { name: "History" }).click();
  const history = final.getByRole("region", { name: "History" });
  await expect(history).toContainText(`declared #${declaration.id} (${SERVICE}) {BASE} in client.py`);
  await expect(history).toContainText(`revoked #${declaration.id}`);
  await expect(history).toContainText("the items API, for client.py");
  await expect(history).toContainText("end of test");
  expect(problems).toEqual([]);
});
