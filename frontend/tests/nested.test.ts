import { describe, expect, it } from "vitest";

import { flowEdges } from "@/components/graph/flow";
import { layoutDistricts, summaryId } from "@/lib/graph/districtLayout";
import { districts } from "@/lib/graph/districts";
import { inclusionMarks } from "@/lib/graph/inclusionMarks";
import { liftId } from "@/lib/graph/lift";
import { frameFacts, frameMeta, summaryFacts, summaryLabel } from "@/lib/graph/overviewCards";
import { overviewView } from "@/lib/graph/overviewView";
import { buildOverview, repositoryId } from "@/lib/model/graph";

import { nestedFixture } from "./fixtures/nested";

/**
 * Nested-repository links and one inclusion of each kind, on the small fixture:
 * a nested link is a line like any other (never counted as nesting, which is for
 * inclusions), and an inclusion says in words how it was declared.
 */

describe("the nested-repository fixture", () => {
  const { repositories, links } = nestedFixture();
  const edges = buildOverview(repositories, links).edges;
  const ds = districts(repositories, edges);
  const names = new Map(repositories.map((r) => [repositoryId(r.slug), r.name]));
  const marks = inclusionMarks(repositories, edges);
  const id = repositoryId;
  const districtOf = (slug: string) => ds.find((d) => d.members.includes(id(slug)))!;

  const viewOf = (expanded: ReadonlySet<string>) =>
    overviewView({ districts: ds, edges, expanded, shelf: layoutDistricts(ds, expanded).shelf });
  const everything = new Set(ds.map((d) => d.id));

  it("falls into the districts the rest of this file reads from", () => {
    const group = districtOf("mono");
    expect(group.kind).toBe("group");
    expect([...group.members].sort()).toEqual(["mono", "mono-web", "mono-api", "app", "lib"].map(id).sort());
    expect(districtOf("fw-docs").kind).toBe("loose");
    const trees = ["fw", "sub-root", "sr-root", "odd-root"].map(districtOf);
    for (const tree of trees) expect(tree.kind).toBe("tree");
    expect(new Set(trees.map((t) => t.id)).size).toBe(4);
    expect(ds.length).toBe(6);
  });

  describe("with everything open", () => {
    const view = viewOf(everything);

    it("draws both nested links as lines of their own, in the nested family", () => {
      for (const edge of ["nested:mono->mono-web", "nested:mono->mono-api"]) {
        const line = view.edges.find((l) => l.id === edge);
        expect(line, edge).toBeDefined();
        expect(line!.lifted, edge).toBe(false);
        expect(line!.link.family, edge).toBe("nested");
        expect(line!.from, edge).toEqual([edge]);
      }
    });

    it("counts the four inclusions as nesting, and none of the nested links", () => {
      expect(view.nesting.links).toBe(4);
      expect(view.nesting.count).toBe(4);
      expect(view.internal.size).toBe(0);
    });

    it("carries every nested link on a line", () => {
      const nested = edges.filter((e) => e.link.family === "nested");
      expect(nested.map((e) => e.id).sort()).toEqual(["nested:fw->fw-docs", "nested:mono->mono-api", "nested:mono->mono-web"]);
      for (const edge of nested) {
        expect(view.carriedBy.has(edge.id), edge.id).toBe(true);
        expect(view.carriedBy.get(edge.id), edge.id).not.toBeNull();
      }
    });

    it("draws them as nested relationships", () => {
      const flow = flowEdges(
        view.edges.filter((l) => l.link.family === "nested"),
        { route: () => "float" },
      );
      expect(flow.length).toBe(3);
      for (const e of flow) expect(e.data!.relation, e.id).toBe("nested");
    });
  });

  describe("with nothing open", () => {
    const view = viewOf(new Set());
    const group = districtOf("mono");
    const tree = districtOf("fw");
    const loose = districtOf("fw-docs");

    it("lifts a nested link between two districts onto their summaries", () => {
      const line = liftId("nested", summaryId(tree.id), summaryId(loose.id));
      expect(view.carriedBy.get("nested:fw->fw-docs")).toBe(line);
      const drawn = view.edges.find((l) => l.id === line);
      expect(drawn).toBeDefined();
      expect(drawn!.lifted).toBe(true);
      expect(drawn!.from).toEqual(["nested:fw->fw-docs"]);
      expect(drawn!.link.family).toBe("nested");
    });

    it("counts the nested links inside the group on the group", () => {
      expect(view.internal.get(group.id)?.nested?.links).toBe(2);
    });

    it("says what each summary holds, nested links and inclusions alike", () => {
      expect(summaryLabel(summaryFacts(view, tree, names))).toBe(
        "fw: 2 repositories; to other districts: 1 nested-repository link; inside: 1 inclusion link",
      );
      expect(summaryLabel(summaryFacts(view, loose, names))).toBe(
        "Linked elsewhere: 1 repository; to other districts: 1 nested-repository link",
      );
    });
  });

  describe("the marks of the inclusions", () => {
    it("tells the kind of each declaration, and says an unnamed format as written", () => {
      expect(marks.get(id("fw-hal"))).toMatchObject({ parent: id("fw"), depth: 1, format: "west", kind: "manifest", label: "west manifest" });
      expect(marks.get(id("sub-child"))).toMatchObject({ parent: id("sub-root"), kind: "submodule", label: "Git submodule" });
      expect(marks.get(id("sr-child"))).toMatchObject({ parent: id("sr-root"), kind: "subrepo", label: "git-subrepo" });
      expect(marks.get(id("odd-child"))).toMatchObject({
        parent: id("odd-root"),
        format: "unknown-tool",
        kind: "other",
        label: "unknown-tool",
        path: "lib/odd-child",
      });
    });

    it("marks only the inclusion families: a nested link makes no tree", () => {
      expect([...marks.keys()].sort()).toEqual(
        ["fw", "fw-hal", "sub-root", "sub-child", "sr-root", "sr-child", "odd-root", "odd-child"].map(id).sort(),
      );
    });

    it("names what joins each tree in its frame", () => {
      const meta = (root: string) => frameMeta(frameFacts(districtOf(root), marks));
      expect(meta("fw")).toBe("west manifest · depth 1");
      expect(meta("sub-root")).toBe("Git submodule · depth 1");
      expect(meta("sr-root")).toBe("git-subrepo · depth 1");
      expect(meta("odd-root")).toBe("unknown-tool · depth 1");
      expect(frameFacts(districtOf("odd-root"), marks).inclusion?.kinds).toEqual(["other"]);
    });
  });
});
