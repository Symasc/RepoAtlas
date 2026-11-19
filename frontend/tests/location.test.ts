import { describe, expect, it } from "vitest";

import { districts } from "@/lib/graph/districts";
import { buildOverview } from "@/lib/model/graph";
import { DEFAULT_OVERVIEW, isOverview, readOverviewState, search, type OverviewState, type Place } from "@/lib/model/location";

import { ecosystem300 } from "./fixtures/ecosystem";

/**
 * The overview's state in the address: how `expand` and `isolate` are written
 * and read back. Each test names the shortcut it would catch.
 */

const OV: Place = { repository: null, path: "" };

const read = (query: string): OverviewState => readOverviewState(new URLSearchParams(query));

describe("writing the overview's state", () => {
  it("writes the districts without their prefix, sorted by code unit, once each", () => {
    const state: OverviewState = { expand: ["district:tree:a#1", "district:shared", "district:tree:a#1"], isolate: null };
    expect(search(OV, null, state)).toBe("?expand=shared%2Ctree%3Aa%231");
    // code-unit order, not locale order: "Z" sorts before "a"
    expect(search(OV, null, { expand: ["district:a", "district:Z"], isolate: null })).toBe("?expand=Z%2Ca");
  });

  it("writes an empty list as `expand=`, and nothing for no state", () => {
    expect(search(OV, "repo:x", { expand: [], isolate: null })).toBe("?expand=&sel=repo%3Ax");
    expect(search(OV)).toBe("");
    expect(search(OV, "repo:x")).toBe("?sel=repo%3Ax");
    expect(search(OV, null, DEFAULT_OVERVIEW)).toBe("");
  });

  it("writes neither anywhere but the overview", () => {
    const state: OverviewState = { expand: ["district:shared"], isolate: "district:tree:a" };
    expect(search({ repository: "app", path: "" }, null, state)).toBe("?repo=app");
    expect(search({ repository: null, path: "", trace: "k" }, null, state)).toBe("?trace=k");
    expect(isOverview(OV)).toBe(true);
    expect(isOverview({ repository: null, path: "", trace: null })).toBe(true);
    expect(isOverview({ repository: null, path: "", trace: "k" })).toBe(false);
    expect(isOverview({ repository: "app", path: "" })).toBe(false);
  });

  it("writes isolate, then expand, then sel", () => {
    const state: OverviewState = { isolate: "district:tree:a", expand: ["district:shared"] };
    expect(search(OV, "repo:x", state)).toBe("?isolate=tree%3Aa&expand=shared&sel=repo%3Ax");
  });

  it("writes no empty isolate", () => {
    expect(search(OV, null, { expand: null, isolate: "district:" })).toBe("");
    expect(search(OV, null, { expand: null, isolate: "" })).toBe("");
  });
});

describe("reading the overview's state", () => {
  const SAMPLES = [
    "district:tree:a%2Fb/c%2Fd#2",
    "district:tree:x:other",
    "district:group:%40s%2Fp",
    "district:group:a%2Cb",
    "district:shared",
  ];

  it("gives back exactly what was written, whatever the id holds", () => {
    for (const id of SAMPLES) {
      const written = search(OV, null, { expand: [id, "district:small"], isolate: id });
      const back = read(written.slice(1));
      expect(back.isolate, id).toBe(id);
      expect([...(back.expand ?? [])].sort(), id).toEqual([id, "district:small"].sort());
    }
    // a slug with a comma is encoded inside its id, so splitting on commas never cuts it
    const comma = "district:group:" + encodeURIComponent("a,b");
    expect(comma).not.toContain(",");
    const back = read(search(OV, null, { expand: [comma, "district:small"], isolate: null }).slice(1));
    expect([...(back.expand ?? [])].sort()).toEqual([comma, "district:small"].sort());
  });

  it("tells absent from empty, drops empty tokens, repeats and an id already prefixed", () => {
    expect(read("")).toEqual({ expand: null, isolate: null });
    expect(read("expand=").expand).toEqual([]);
    expect(read("expand=,,").expand).toEqual([]);
    expect(read("expand=a,,a,b").expand).toEqual(["district:a", "district:b"]);
    expect(read("expand=district:shared").expand).toEqual(["district:shared"]);
    expect(read("isolate=").isolate).toBeNull();
    expect(read("isolate=shared").isolate).toBe("district:shared");
    expect(read("isolate=district:shared").isolate).toBe("district:shared");
  });

  it("never throws, on any string", () => {
    let seed = 12345;
    const next = (n: number) => {
      seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
      return seed % n;
    };
    const alphabet = ["%", "#", ",", ":", "/", "&", "=", "+", " ", "a", "Z", "0", "é", "日", "\u{1F600}", "%E0%A4%A", "%zz", "district:", "\uD800"];
    const samples = Array.from({ length: 300 }, () =>
      Array.from({ length: next(40) }, () => alphabet[next(alphabet.length)]).join(""),
    );
    samples.push("%".repeat(10_000), "a,".repeat(5_000), "%".repeat(1) + "x".repeat(10_000));
    for (const sample of samples) {
      expect(() => {
        const viaValue = readOverviewState(new URLSearchParams({ expand: sample, isolate: sample }));
        const viaQuery = readOverviewState(new URLSearchParams(`expand=${sample}&isolate=${sample}`));
        search(OV, sample, viaValue);
        search(OV, sample, viaQuery);
      }).not.toThrow();
    }
  });
});

describe("district ids", () => {
  it("never hold a comma, so a list of them can be split on commas", () => {
    const { repositories, links } = ecosystem300();
    const ds = districts(repositories, buildOverview(repositories, links).edges);
    expect(ds.length).toBeGreaterThan(1);
    for (const d of ds) expect(d.id.includes(","), d.id).toBe(false);
  });
});
