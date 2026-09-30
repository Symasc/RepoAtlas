import { test as base, expect, type APIRequestContext, type Page } from "@playwright/test";

import type { EndpointName, Endpoints } from "@/lib/api/generated";

/**
 * Every page records its console errors, uncaught exceptions and failed requests;
 * a test ends by asserting there were none unless it says otherwise.
 */
export const test = base.extend<{ problems: string[] }>({
  problems: async ({ page }, use) => {
    const problems: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "error") problems.push(`console: ${message.text()}`);
    });
    page.on("pageerror", (error) => problems.push(`exception: ${error.message}`));
    page.on("requestfailed", (request) =>
      problems.push(`request failed: ${request.url()} ${request.failure()?.errorText ?? ""}`),
    );
    await use(problems);
  },
});

export { expect };

/** Asks the live API, through the explorer's own route, what the UI should show. */
export async function ask<N extends EndpointName>(
  request: APIRequestContext,
  name: N,
  body: Endpoints[N]["request"],
): Promise<Endpoints[N]["response"]> {
  const response = await request.post(`/api/ra/${name}`, { data: body });
  expect(response.status(), await response.text()).toBe(200);
  const payload = (await response.json()) as { ok: true; result: Endpoints[N]["response"] };
  return payload.result;
}

/** The shell has the live index: the freshness check has answered. */
export async function ready(page: Page): Promise<void> {
  await expect(
    page.getByRole("button", { name: /^Index freshness: (All \d+ current|\d+ of \d+ current|Current|No repositories)$/ }),
  ).toBeVisible({ timeout: 90_000 });
}
