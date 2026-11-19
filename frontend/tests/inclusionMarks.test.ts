import { describe, expect, it } from "vitest";

import { districts } from "@/lib/graph/districts";
import { formatLabel, inclusionKind, inclusionMarks, INCLUSION_KINDS } from "@/lib/graph/inclusionMarks";
import { buildOverview, repositoryId } from "@/lib/model/graph";

import { ecosystem300, FIXTURE_PARTS } from "./fixtures/ecosystem";

/**
 * How each repository of an inclusion family reads: who includes it, how deep,
 * and by what kind of declaration, on the 300-repository fixture (its three trees
 * are the fixture's, frozen) and as a table of formats.
 */

/** A seeded shuffle (a 32-bit LCG), so the order changes the same way on every run. */
function shuffled<T>(list: readonly T[], seed: number): T[] {
  let state = seed >>> 0;
  const next = () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state / 4294967296;
  };
  const out = [...list];
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(next() * (i + 1));
    [out[i], out[j]] = [out[j]!, out[i]!];
  }
  return out;
}

describe("the inclusion marks of the 300-repository fixture", () => {
  const { repositories, links } = ecosystem300();
  const edges = buildOverview(repositories, links).edges;
  const marks = inclusionMarks(repositories, edges);
  const { t1, t2, t3 } = FIXTURE_PARTS;
  const mark = (slug: string) => marks.get(repositoryId(slug))!;

  it("reads the binary tree of git submodules: its parent, its depth and its kind", () => {
    for (let i = 1; i < t1.length; i++) {
      const m = mark(t1[i]!);
      expect(m.parent, t1[i]).toBe(repositoryId(t1[Math.floor((i - 1) / 2)]!));
      expect(m.depth, t1[i]).toBe(Math.floor(Math.log2(i + 1)));
      expect(m.kind, t1[i]).toBe("submodule");
      expect(m.label, t1[i]).toBe("Git submodule");
      expect(m.root, t1[i]).toBe(repositoryId(t1[0]!));
    }
  });

  it("reads the west manifest: its members are manifest inclusions, down to depth 5", () => {
    for (let i = 1; i < t2.length; i++) {
      expect(mark(t2[i]!).kind, t2[i]).toBe("manifest");
      expect(mark(t2[i]!).label, t2[i]).toBe("west manifest");
      expect(mark(t2[i]!).format, t2[i]).toBe("west");
    }
    expect(mark("t2-31").parent).toBe(repositoryId("t2-01"));
    expect(mark("t2-31").depth).toBe(2);
    expect(mark("t2-34").depth).toBe(5);
  });

  it("reads the git-subrepo tree, its shared child and its loop", () => {
    for (let i = 1; i < t3.length; i++) {
      expect(mark(t3[i]!).kind, t3[i]).toBe("subrepo");
      expect(mark(t3[i]!).label, t3[i]).toBe("git-subrepo");
    }
    // t3-09 is also included by a leaf of T1, but it stays where it was first reached
    expect(mark("t3-09").parent).toBe(repositoryId("t3-04"));
    expect(mark("t3-09").depth).toBe(3);
    expect(mark("t3-09").kind).toBe("subrepo");
    expect(mark("t3-09").root).toBe(repositoryId("t3-00"));
    // t3-39 includes t3-01 back; t3-01 is still the root's child
    expect(mark("t3-01").parent).toBe(repositoryId("t3-00"));
    expect(mark("t3-01").depth).toBe(1);
  });

  it("gives a root no parent, no depth, and nothing to say about how it was included", () => {
    for (const root of ["t1-00", "t2-00", "t3-00"]) {
      expect(mark(root)).toEqual({
        parent: null,
        depth: 0,
        root: repositoryId(root),
        format: null,
        kind: null,
        label: null,
        path: null,
      });
    }
  });

  it("marks exactly the repositories of the tree districts", () => {
    const inTrees = districts(repositories, edges)
      .filter((d) => d.kind === "tree")
      .flatMap((d) => d.members);
    expect([...marks.keys()].sort()).toEqual([...inTrees].sort());
    expect(marks.size).toBe(t1.length + t2.length + t3.length);
  });

  it("states where each parent includes a child: the path of its inclusion", () => {
    expect(mark("t1-05").path).toBe("vendor/t1-05");
    expect(mark("t2-01").path).toBe("modules/hal/t2-01");
    expect(mark("t3-02").path).toBe("lib/t3-02");
  });

  it("does not depend on the order repositories and edges arrive in", () => {
    for (const seed of [1, 2, 3]) {
      const other = inclusionMarks(shuffled(repositories, seed), shuffled(edges, seed + 10));
      expect([...other]).toEqual([...marks]);
      expect(other).toEqual(marks);
    }
  });

  it("lists its keys in code-unit order", () => {
    const keys = [...marks.keys()];
    expect(keys).toEqual([...keys].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0)));
  });
});

describe("the kinds and words of the declaration formats", () => {
  it("sorts every format into a kind", () => {
    expect(inclusionKind("gitmodules")).toBe("submodule");
    expect(inclusionKind("git-subrepo")).toBe("subrepo");
    expect(inclusionKind("west")).toBe("manifest");
    expect(inclusionKind("repo-manifest")).toBe("manifest");
    expect(inclusionKind("vcstool")).toBe("manifest");
    expect(inclusionKind("unknown-tool")).toBe("other");
    expect(inclusionKind("")).toBe("other");
  });

  it("names every format in the words of the inspector, and an unnamed one as written", () => {
    expect(formatLabel("gitmodules")).toBe("Git submodule");
    expect(formatLabel("git-subrepo")).toBe("git-subrepo");
    expect(formatLabel("west")).toBe("west manifest");
    expect(formatLabel("repo-manifest")).toBe("repo manifest");
    expect(formatLabel("vcstool")).toBe("vcstool .repos");
    expect(formatLabel("unknown-tool")).toBe("unknown-tool");
    expect(formatLabel("constructor")).toBe("constructor");
  });

  it("lists the kinds in the order a frame names them", () => {
    expect(INCLUSION_KINDS).toEqual(["submodule", "subrepo", "manifest", "other"]);
  });
});
