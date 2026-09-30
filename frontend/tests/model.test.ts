import { describe, expect, it } from "vitest";

import type { FreshnessState, RepositoriesResponse } from "@/lib/api/generated";
import { plural, relativeTime, shortSha } from "@/lib/model/format";
import { FRESHNESS, summarize, toFreshness } from "@/lib/model/freshness";
import { assignHues, HUE_COUNT, toRepositories, totals } from "@/lib/model/repositories";

function freshness(state: FreshnessState) {
  return toFreshness({ state, depth: "live", repository_slug: "r" });
}

describe("freshness", () => {
  it("names every state the backend can report", () => {
    const states: FreshnessState[] = [
      "not_indexed",
      "unavailable",
      "interrupted",
      "stale_build",
      "indexed",
      "current_clean",
      "dirty",
      "stale_head",
      "stale_worktree",
      "unknown",
    ];
    for (const state of states) expect(FRESHNESS[state].label).not.toBe("");
  });

  it("is summarised by its worst repository, never averaged", () => {
    const summary = summarize([
      freshness("current_clean"),
      freshness("current_clean"),
      freshness("current_clean"),
      freshness("interrupted"),
    ]);
    expect(summary.tone).toBe("problem");
    expect(summary.headline).toBe("3 of 4 current");
  });

  it("reads plainly when everything is current", () => {
    expect(summarize([freshness("current_clean")]).headline).toBe("Current");
    expect(summarize([freshness("current_clean"), freshness("current_clean")]).headline).toBe(
      "All 2 current",
    );
    expect(summarize([]).headline).toBe("No repositories");
  });

  it("does not count stored, unchecked state as current", () => {
    expect(summarize([freshness("indexed")])).toMatchObject({ current: 0, tone: "unverified" });
  });
});

describe("repositories", () => {
  const response: RepositoriesResponse = {
    context: { scope: "global" },
    scope: "global",
    repositories: [
      { slug: "b", name: "b", freshness: { state: "indexed", depth: "stored", repository_slug: "b" } },
      {
        slug: "a",
        name: "a",
        indexed_files: 3,
        symbols: 5,
        relationships: 7,
        freshness: { state: "indexed", depth: "stored", repository_slug: "a" },
      },
    ],
  };

  it("are listed by slug with the contract's defaults filled in", () => {
    const [a, b] = toRepositories(response);
    expect(a).toMatchObject({ slug: "a", files: 3, branch: null, languages: [] });
    expect(b).toMatchObject({ slug: "b", files: 0, symbols: 0, relationships: 0 });
  });

  it("add up to the workspace's totals", () => {
    expect(totals(toRepositories(response))).toEqual({
      repositories: 2,
      files: 3,
      symbols: 5,
      relationships: 7,
    });
  });

  it("get distinct, stable hues up to the palette's size", () => {
    const slugs = ["RepoAtlas", "lvl", "pitch", "watermarking", "e", "f", "g", "h"];
    const first = assignHues(slugs);
    expect(new Set(first.values()).size).toBe(HUE_COUNT);
    expect(assignHues([...slugs].reverse())).toEqual(first);
    for (const hue of first.values()) expect(hue).toBeGreaterThanOrEqual(1);
  });
});

describe("formatting", () => {
  it("counts with grouping and agreement", () => {
    expect(plural(1, "file")).toBe("1 file");
    expect(plural(3821, "file")).toBe("3,821 files");
    expect(plural(2, "repository", "repositories")).toBe("2 repositories");
  });

  it("shortens a SHA and dates relative to now", () => {
    expect(shortSha("3ff32e9c85e9d410c4b9e2c63977b3e87296b4df")).toBe("3ff32e9");
    const now = Date.parse("2026-09-30T12:00:00Z");
    expect(relativeTime("2026-09-30T09:00:00Z", now)).toBe("3 hours ago");
    expect(relativeTime("2026-09-30T11:59:40Z", now)).toBe("just now");
  });
});
