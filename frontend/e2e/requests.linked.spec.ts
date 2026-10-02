import type { Page, Request } from "@playwright/test";

import { ask, expect, ready, test } from "./fixtures";

/**
 * The HTTP join's stored decisions in the explorer (M5b), on the linked corpus.
 * `http-refused` makes three requests the join refuses, each for its own reason;
 * `http-web`'s one request is linked to `http-api`'s handler. The refs and states
 * are the ones M5b's Phase A froze, and `tests/capabilities` pins them in Python.
 */
const ROUTE_UNMATCHED = "hcd1-25680369ded384629e5e";
const SERVICE_UNMATCHED = "hcd1-1a63689c3b7bd6debe06";
const BASE_UNKNOWN = "hcd1-72d6b105266f94bea41e";
const LINKED = "hcd1-b208cc48f79ed1081fac";

const HOPS = ["caller", "origin", "service_identity", "deployment_binding", "source_binding", "route", "handler"];

/** Every `http_operations` request the page sends, as its body. */
function operationsSent(page: Page): Record<string, unknown>[] {
  const sent: Record<string, unknown>[] = [];
  page.on("request", (request: Request) => {
    if (request.method() === "POST" && request.url().endsWith("/api/ra/http_operations")) {
      sent.push(request.postDataJSON() as Record<string, unknown>);
    }
  });
  return sent;
}

/** One crossing's stepper, by the decision it shows. */
function chainOf(page: Page, ref: string) {
  return page.getByRole("list", { name: `The chain of ${ref}` });
}

async function states(page: Page, ref: string): Promise<string[][]> {
  return chainOf(page, ref)
    .locator("li[data-hop]")
    .evaluateAll((steps) => steps.map((s) => [s.getAttribute("data-hop")!, s.getAttribute("data-state")!, s.getAttribute("data-tone")!]));
}

async function openRequests(page: Page, repository: string) {
  await page.goto(`/?sel=${encodeURIComponent(`repo:${repository}`)}`);
  await ready(page);
  const panel = page.getByRole("complementary", { name: `${repository}, inspected` });
  await panel.getByRole("tab", { name: "Requests" }).click();
  return panel;
}

test("a repository's Requests list each refusal by its exact status, join-only requests counted but not listed", async ({ page, problems }) => {
  const sent = operationsSent(page);
  const panel = await openRequests(page, "http-refused");

  const decisions = panel.getByRole("region", { name: "Decisions" });
  await expect(decisions.getByRole("listitem")).toHaveCount(3);
  await expect(decisions.getByRole("listitem").filter({ hasText: "http-route-unmatched" })).toContainText("1");
  await expect(decisions.getByRole("listitem").filter({ hasText: "http-service-unmatched" })).toContainText("1");
  await expect(decisions.getByRole("listitem").filter({ hasText: "http-base-unknown" })).toContainText("1 on join-only requests");

  const requests = panel.getByRole("region", { name: "Requests", exact: true });
  await expect(chainOf(page, ROUTE_UNMATCHED)).toBeVisible();
  await expect(chainOf(page, SERVICE_UNMATCHED)).toBeVisible();
  await expect(requests).toContainText("/missing/7");
  await expect(requests).toContainText("/items/7");
  await expect(requests).toContainText("no_indexed_handler");
  // the join-only row is not listed until asked for
  await expect(chainOf(page, BASE_UNKNOWN)).toHaveCount(0);
  await expect(panel).not.toContainText("/items/{item_id}");

  // every read so far: repository scope, other repositories shown, no evidence, no lookup
  expect(sent.length).toBeGreaterThan(0);
  for (const body of sent) {
    expect(body.context).toEqual({ scope: "repository", repository: "http-refused" });
    expect(body.cross_repository).toBe(true);
    expect(body.include_provenance).toBe(false);
    expect(body).not.toHaveProperty("decision_ref");
  }
  const listing = sent.find((body) => Array.isArray(body.status) && !(body.status as string[]).includes("join_only"));
  expect(listing?.status).toEqual(["matched", "ambiguous", "no_indexed_handler", "unresolved"]);

  await panel.getByRole("switch", { name: "Show join-only requests (1)" }).click({ force: true });
  await expect(chainOf(page, BASE_UNKNOWN)).toBeVisible();
  await expect(panel.getByRole("region", { name: "Join-only requests" })).toContainText("join_only");
  expect(problems).toEqual([]);
});

test("a refusal's chain stops at its own hop, the hops never reached subdued and placeless", async ({ page, problems }) => {
  await openRequests(page, "http-refused");

  expect(await states(page, ROUTE_UNMATCHED)).toEqual([
    ...HOPS.slice(0, 5).map((hop) => [hop, "proven", "emphasis"]),
    ["route", "unresolved", "emphasis"],
    ["handler", "not_reached", "subdued"],
  ]);
  expect(await states(page, SERVICE_UNMATCHED)).toEqual([
    ["caller", "proven", "emphasis"],
    ["origin", "proven", "emphasis"],
    ["service_identity", "unresolved", "emphasis"],
    ...HOPS.slice(3).map((hop) => [hop, "not_reached", "subdued"]),
  ]);
  const route = chainOf(page, ROUTE_UNMATCHED).locator('li[data-hop="route"]');
  await expect(route).toContainText("route_unmatched");
  const handler = chainOf(page, ROUTE_UNMATCHED).locator('li[data-hop="handler"]');
  await expect(handler.getByRole("button")).toHaveCount(0);
  await expect(handler).not.toContainText("/");

  // a proven hop opens out to its place, in another repository too
  const identity = chainOf(page, ROUTE_UNMATCHED).locator('li[data-hop="service_identity"]');
  await identity.getByRole("button", { name: "1 place" }).click();
  await expect(identity.getByRole("button", { name: "http-api/app.py:5" })).toBeVisible();
  expect(problems).toEqual([]);
});

test("opening a decision's evidence reads exactly that decision, in global scope, with every repository's lines", async ({ page, problems }) => {
  const sent = operationsSent(page);
  const panel = await openRequests(page, "http-refused");
  const crossing = panel.locator(`[data-decision="${ROUTE_UNMATCHED}"]`).first();
  await crossing.getByRole("button", { name: "Evidence" }).click();

  await expect.poll(() => sent.filter((body) => body.decision_ref === ROUTE_UNMATCHED).length).toBe(1);
  const lookup = sent.find((body) => body.decision_ref === ROUTE_UNMATCHED)!;
  expect(lookup.context).toEqual({ scope: "global" });
  expect(lookup.include_provenance).toBe(true);
  // nothing else was looked up
  expect(sent.filter((body) => body.decision_ref !== undefined)).toHaveLength(1);

  const caller = chainOf(page, ROUTE_UNMATCHED).locator('li[data-hop="caller"]');
  await caller.getByRole("button", { name: "1 place" }).click();
  await expect(caller.locator("pre")).toContainText('requests.get("https://http-pair.example/missing/7")');
  const identity = chainOf(page, ROUTE_UNMATCHED).locator('li[data-hop="service_identity"]');
  await identity.getByRole("button", { name: "1 place" }).click();
  await expect(identity.locator("pre")).toBeVisible();
  expect(problems).toEqual([]);
});

test("a handler link's Evidence shows the decision its own ref names, every hop proven", async ({ page, request, problems }) => {
  const overview = await ask(request, "topology/overview", { context: { scope: "global" } });
  const http = (overview.links ?? []).filter((l) => l.kind === "http");
  expect(http.map((l) => [l.source, l.target])).toEqual([["http-web", "http-api"]]);

  const sent = operationsSent(page);
  await page.goto(`/?sel=${encodeURIComponent("repo:http-web")}`);
  await ready(page);
  const panel = page.getByRole("complementary", { name: "http-web, inspected" });
  await panel.locator("button[aria-pressed]").filter({ hasText: "http-api" }).first().click();
  await expect(panel.getByRole("tab", { name: "Evidence", selected: true })).toBeVisible();

  const decision = panel.getByRole("region", { name: "The stored decision" });
  await expect(decision).toContainText(LINKED);
  await expect(decision).toContainText("linked");
  expect(await states(page, LINKED)).toEqual(HOPS.map((hop) => [hop, "proven", "emphasis"]));
  expect(sent.filter((body) => body.decision_ref !== undefined)).toEqual([
    expect.objectContaining({ context: { scope: "global" }, decision_ref: LINKED, include_provenance: true }),
  ]);
  expect(problems).toEqual([]);
});

test("a refusal is never drawn: the only HTTP line is the link", async ({ page, problems }) => {
  const lit = page.locator('.react-flow__edges g[data-kind="http"][data-emphasis="lit"]');
  await page.goto(`/?sel=${encodeURIComponent("repo:http-refused")}`);
  await ready(page);
  await expect(page.getByRole("complementary", { name: "http-refused, inspected" })).toBeVisible();
  await expect(lit).toHaveCount(0);
  await page.goto(`/?sel=${encodeURIComponent("repo:http-web")}`);
  await ready(page);
  await expect(lit.first()).toBeVisible();
  expect(problems).toEqual([]);
});

test("a file's Requests list that file's requests", async ({ page, problems }) => {
  await page.goto(`/?repo=http-refused&path=${encodeURIComponent("client.py")}`);
  await ready(page);
  const panel = page.getByRole("complementary", { name: "client.py, inspected" });
  await panel.getByRole("tab", { name: "Requests" }).click();
  await expect(chainOf(page, ROUTE_UNMATCHED)).toBeVisible();
  await expect(chainOf(page, SERVICE_UNMATCHED)).toBeVisible();
  await expect(panel.getByRole("switch", { name: "Show join-only requests (1)" })).toBeVisible();
  expect(problems).toEqual([]);
});
