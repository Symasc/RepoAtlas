import { readFileSync } from "node:fs";
import path from "node:path";

import { expect, test } from "@playwright/test";

import { STATE } from "../playwright.config";

/**
 * The explorer's route adds the API token, so it must refuse everything a page
 * on another site, or a rebound DNS name, could send it. Asked over real HTTP.
 */
const BODY = JSON.stringify({ context: { scope: "global" } });

async function code(response: { json(): Promise<unknown> }): Promise<string> {
  return ((await response.json()) as { error: { code: string } }).error.code;
}

test("answers the explorer's own requests", async ({ request }) => {
  const response = await request.post("/api/ra/status", {
    headers: { "content-type": "application/json" },
    data: BODY,
  });
  expect(response.status()).toBe(200);
  expect(((await response.json()) as { ok: boolean }).ok).toBe(true);
});

test("refuses a Host that is not loopback", async ({ request }) => {
  const response = await request.post("/api/ra/status", {
    headers: { "content-type": "application/json", host: "attacker.example:7338" },
    data: BODY,
  });
  expect(response.status()).toBe(403);
  expect(await code(response)).toBe("HOST_NOT_ALLOWED");
});

test("refuses a request from another site", async ({ request }) => {
  const response = await request.post("/api/ra/status", {
    headers: {
      "content-type": "application/json",
      origin: "http://attacker.example",
      "sec-fetch-site": "cross-site",
    },
    data: BODY,
  });
  expect(response.status()).toBe(403);
  expect(await code(response)).toBe("CROSS_SITE_REQUEST");
});

test("refuses a body a form could send", async ({ request }) => {
  const response = await request.post("/api/ra/status", {
    headers: { "content-type": "text/plain" },
    data: BODY,
  });
  expect(response.status()).toBe(415);
});

test("refuses endpoints the contract does not serve, and other methods", async ({ request }) => {
  for (const name of ["sync", "verify_grounding"]) {
    const response = await request.post(`/api/ra/${name}`, {
      headers: { "content-type": "application/json" },
      data: BODY,
    });
    expect(response.status()).toBe(404);
    expect(await code(response)).toBe("UNKNOWN_ENDPOINT");
  }
  expect((await request.get("/api/ra/status")).status()).toBe(405);
});

test("never hands the token to the browser", async ({ page }) => {
  // the suite's own server wrote this file (playwright.config.ts)
  const record = readFileSync(path.join(STATE, "live-api.json"), "utf8");
  const { token } = JSON.parse(record) as { token: string };
  const bodies: Promise<string>[] = [];
  page.on("response", (response) => bodies.push(response.text().catch(() => "")));
  await page.goto("/");
  await expect(page.getByRole("button", { name: /^Index freshness: .*current/ })).toBeVisible({
    timeout: 90_000,
  });
  expect(await page.content()).not.toContain(token);
  for (const body of await Promise.all(bodies)) expect(body).not.toContain(token);
  expect(bodies.length).toBeGreaterThan(3);
});
