import { describe, expect, it } from "vitest";

import { flowEdges } from "@/components/graph/flow";
import { defaultExpanded, layoutDistricts, summaryId } from "@/lib/graph/districtLayout";
import { districts, type District } from "@/lib/graph/districts";
import { inclusionMarks, type InclusionKind } from "@/lib/graph/inclusionMarks";
import type { FamilyTallies } from "@/lib/graph/lift";
import {
  FAMILY_WORD,
  SAMPLE,
  frameFacts,
  frameMeta,
  frameTitle,
  notDrawn,
  notDrawnText,
  summaryFacts,
  summaryLabel,
  whyNotDrawn,
  whyNotDrawnText,
  type SummaryFacts,
} from "@/lib/graph/overviewCards";
import { overviewView, visibleEdge } from "@/lib/graph/overviewView";
import { formatCount } from "@/lib/model/format";
import { buildOverview, FAMILIES, repositoryId, type ViewEdge } from "@/lib/model/graph";

import { ecosystem300 } from "./fixtures/ecosystem";

/**
 * What the overview's cards say, on the 300-repository fixture: the numbers on a
 * summary add up to what the lift counted, the words are exactly the pinned ones,
 * and a line's ids chain from the drawn line back to the connection behind it.
 */

describe("the cards of the 300-repository fixture", () => {
  const { repositories, links } = ecosystem300();
  const edges = buildOverview(repositories, links).edges;
  const ds = districts(repositories, edges);
  const names = new Map(repositories.map((r) => [repositoryId(r.slug), r.name]));
  const marks = inclusionMarks(repositories, edges);

  const viewOf = (expanded: ReadonlySet<string>, extra: readonly ViewEdge[] = []) =>
    overviewView({ districts: ds, edges: [...edges, ...extra], expanded, shelf: layoutDistricts(ds, expanded).shelf });
  const closed = new Set<string>();
  const opened = defaultExpanded(ds, { width: 1440, height: 810 }).expanded;
  const everything = new Set(ds.map((d) => d.id));
  const states: [string, ReadonlySet<string>][] = [
    ["nothing open", closed],
    ["the default at 1440x810", opened],
    ["everything open", everything],
  ];

  const linksOf = (tallies: FamilyTallies): number => FAMILIES.reduce((sum, f) => sum + (tallies[f]?.links ?? 0), 0);
  const summariesOf = (expanded: ReadonlySet<string>) => {
    const view = viewOf(expanded);
    return { view, summaries: ds.filter((d) => !expanded.has(d.id)).map((d) => summaryFacts(view, d, names)) };
  };

  it("counts the links inside collapsed districts as the summaries do", () => {
    for (const [name, expanded] of states) {
      const { view, summaries } = summariesOf(expanded);
      const inside = summaries.reduce((sum, s) => sum + linksOf(s.internal), 0);
      expect(notDrawn(view).internal.links, name).toBe(inside);
      if (expanded === everything) expect(notDrawn(view).internal.links, name).toBe(0);
    }
    expect(notDrawn(summariesOf(closed).view).internal.links).toBeGreaterThan(0);
  });

  it("counts the links of a summary's lines twice over, once at each end, when nothing is open", () => {
    const { view, summaries } = summariesOf(closed);
    expect(view.edges.length).toBeGreaterThan(0);
    const crossing = summaries.reduce((sum, s) => sum + linksOf(s.crossing), 0);
    expect(crossing).toBe(2 * view.edges.reduce((sum, line) => sum + line.from.length, 0));
  });

  it("makes each summary's crossing links a fold over the lines that touch it", () => {
    for (const [name, expanded] of states) {
      const { view, summaries } = summariesOf(expanded);
      for (const s of summaries) {
        const expected: FamilyTallies = {};
        for (const line of view.edges.filter((l) => l.source === s.id || l.target === s.id)) {
          const tally = (expected[line.link.family] ??= { links: 0, count: 0 });
          tally.links += line.from.length;
          tally.count += line.link.count;
        }
        expect(s.crossing, `${name}: ${s.id}`).toEqual(expected);
        expect(s.id).toBe(summaryId(s.district));
        expect(s.internal, `${name}: ${s.id}`).toEqual(view.internal.get(s.district) ?? {});
      }
    }
  });

  it("names the first members of a summary and counts the rest", () => {
    expect(SAMPLE).toBe(3);
    const { summaries } = summariesOf(closed);
    expect(summaries.length).toBe(ds.length);
    for (const s of summaries) {
      const d = ds.find((x) => x.id === s.district)!;
      expect(s.sample, s.id).toEqual(d.members.slice(0, 3).map((m) => names.get(m)));
      expect(s.more, s.id).toBe(d.members.length - s.sample.length);
      expect(s.members, s.id).toBe(d.members.length);
      expect(s.label).toBe(d.label);
      expect(s.kind).toBe(d.kind);
    }
    expect(summaries.some((s) => s.more > 0)).toBe(true);
  });

  it("names a member by its id when no name is known", () => {
    const view = viewOf(closed);
    const d = ds[0]!;
    expect(summaryFacts(view, d, new Map()).sample).toEqual(d.members.slice(0, 3));
  });

  it("reads each family's tree frames as one kind of inclusion", () => {
    const kinds = new Map<string, Set<InclusionKind>>();
    for (const d of ds.filter((x) => x.kind === "tree")) {
      const facts = frameFacts(d, marks);
      expect(facts.inclusion, d.id).not.toBeNull();
      const set = kinds.get(d.root!) ?? new Set<InclusionKind>();
      kinds.set(d.root!, set);
      for (const k of facts.inclusion!.kinds) set.add(k);
    }
    expect([...kinds.get(repositoryId("t1-00"))!]).toEqual(["submodule"]);
    expect([...kinds.get(repositoryId("t2-00"))!]).toEqual(["manifest"]);
    expect([...kinds.get(repositoryId("t3-00"))!]).toEqual(["subrepo"]);
  });

  it("says nothing of inclusions for a district that is not a tree", () => {
    for (const d of ds.filter((x) => x.kind !== "tree")) {
      const facts = frameFacts(d, marks);
      expect(facts.inclusion, d.id).toBeNull();
      expect(frameMeta(facts), d.id).toBeNull();
    }
  });

  it("titles a frame with its name and size, and keeps a cut district's part", () => {
    for (const d of ds) {
      const facts = frameFacts(d, marks);
      expect(frameTitle(facts), d.id).toBe(`${d.label} · ${formatCount(d.members.length)}`);
      expect(facts.part, d.id).toEqual(d.part);
    }
    expect(ds.some((d) => d.part !== null)).toBe(true);
  });

  it("says in words what joins a whole tree, and how deep it goes", () => {
    const whole = districts(repositories, edges, { bound: false });
    const meta = (root: string) => {
      const d: District = whole.find((x) => x.kind === "tree" && x.root === repositoryId(root) && x.members.length > 1)!;
      return frameMeta(frameFacts(d, marks));
    };
    expect(meta("t1-00")).toBe("Git submodule · depth 5");
    expect(meta("t3-00")).toBe("git-subrepo · depth 5");
    // a district holding only a root has no inclusion to name
    const rootOnly = whole.find((x) => x.kind === "tree" && x.members.length === 1)!;
    expect(frameMeta(frameFacts(rootOnly, marks))).toBe("Inclusion tree");
  });

  describe("the words", () => {
    it("names the families as a sentence does", () => {
      expect(FAMILY_WORD).toEqual({
        code: "code",
        http: "HTTP",
        dependency: "package",
        config: "configuration",
        submodule: "inclusion",
        nested: "nested-repository",
      });
    });

    it("sums a summary into one sentence", () => {
      const base: SummaryFacts = {
        id: "summary:district:x",
        district: "district:x",
        label: "Used by many",
        kind: "shared",
        members: 3,
        sample: [],
        more: 0,
        crossing: {},
        internal: {},
      };
      expect(summaryLabel(base)).toBe("Used by many: 3 repositories");
      expect(summaryLabel({ ...base, members: 1 })).toBe("Used by many: 1 repository");
      expect(
        summaryLabel({
          ...base,
          crossing: { http: { links: 12, count: 20 }, code: { links: 1, count: 4 } },
          internal: { dependency: { links: 2, count: 2 }, nested: { links: 1, count: 1 } },
        }),
      ).toBe(
        "Used by many: 3 repositories; to other districts: 1 code link, 12 HTTP links; inside: 2 package links, 1 nested-repository link",
      );
      expect(summaryLabel({ ...base, internal: { config: { links: 1234, count: 1 } } })).toBe(
        "Used by many: 3 repositories; inside: 1,234 configuration links",
      );
    });

    it("says what is not drawn, in the pinned words", () => {
      expect(
        notDrawnText({ nesting: { links: 62, count: 62 }, internal: { links: 1, count: 5 }, internalDistricts: 1, unplaced: 0 }, 3),
      ).toBe("Not drawn: 3 packages with more than one possible publisher · 62 inclusions, shown by nesting · 1 link inside 1 collapsed district");
      expect(
        notDrawnText({ nesting: { links: 1, count: 1 }, internal: { links: 7, count: 9 }, internalDistricts: 2, unplaced: 1 }, 1),
      ).toBe(
        "Not drawn: 1 package with more than one possible publisher · 1 inclusion, shown by nesting · 7 links inside 2 collapsed districts · 1 link with an end not on the map",
      );
      expect(notDrawnText({ nesting: { links: 0, count: 0 }, internal: { links: 0, count: 0 }, internalDistricts: 0, unplaced: 0 }, 0)).toBeNull();
      expect(notDrawnText({ nesting: { links: 0, count: 0 }, internal: { links: 0, count: 0 }, internalDistricts: 0, unplaced: 2 }, 0)).toBe(
        "Not drawn: 2 links with an end not on the map",
      );
    });

    it("says why the chosen connection is not drawn, in the pinned words", () => {
      const district = ds.find((d) => d.kind === "shared")!;
      expect(whyNotDrawnText({ kind: "inside", district })).toBe(
        `The chosen connection is not drawn: both ends are inside ${district.label}, which is collapsed`,
      );
      expect(whyNotDrawnText({ kind: "nesting" })).toBe("The chosen connection is not drawn: it is an inclusion, shown by nesting");
      expect(whyNotDrawnText({ kind: "unplaced" })).toBe("The chosen connection is not drawn: an end is not on the map");
    });
  });

  describe("why a connection is not drawn", () => {
    const ownerOf = (id: string) => ds.find((d) => d.members.includes(id))!;

    it("is inside its district when both ends are, and the district is collapsed", () => {
      const view = viewOf(closed);
      const internal = edges.filter((e) => view.carriedBy.get(e.id) === null && ownerOf(e.source) === ownerOf(e.target));
      expect(internal.length).toBeGreaterThan(0);
      for (const edge of internal) {
        const why = whyNotDrawn(view, ds, edge);
        expect(why, edge.id).toEqual({ kind: "inside", district: ownerOf(edge.source) });
      }
    });

    it("is nesting for an inclusion between two districts, and for every inclusion once everything is open", () => {
      const between = edges.filter((e) => e.link.family === "submodule" && ownerOf(e.source) !== ownerOf(e.target));
      expect(between.length).toBeGreaterThan(0);
      for (const edge of between) expect(whyNotDrawn(viewOf(closed), ds, edge), edge.id).toEqual({ kind: "nesting" });

      const view = viewOf(everything);
      const inclusions = edges.filter((e) => e.link.family === "submodule");
      expect(inclusions.length).toBeGreaterThan(0);
      for (const edge of inclusions) expect(whyNotDrawn(view, ds, edge), edge.id).toEqual({ kind: "nesting" });
    });

    it("is nothing for a connection that is drawn", () => {
      for (const [name, expanded] of states) {
        const view = viewOf(expanded);
        const drawn = edges.filter((e) => view.carriedBy.get(e.id) !== null);
        expect(drawn.length, name).toBeGreaterThan(0);
        for (const edge of drawn) expect(whyNotDrawn(view, ds, edge), `${name}: ${edge.id}`).toBeNull();
      }
    });

    it("is an end off the map for a connection to something that is on no card", () => {
      const ghost: ViewEdge = { ...edges[0]!, id: "observed:ghost->nowhere", source: "repo:ghost", target: edges[0]!.target };
      const view = viewOf(closed, [ghost]);
      expect(view.unplaced).toContain(ghost.id);
      expect(whyNotDrawn(view, ds, ghost)).toEqual({ kind: "unplaced" });
    });
  });

  it("chains the ids of a drawn line back to the connections it carries", () => {
    const view = viewOf(opened);
    expect(view.edges.length).toBeGreaterThan(0);
    for (const line of view.edges) {
      const flow = flowEdges([line], { route: () => "float" });
      expect(flow.length, line.id).toBeGreaterThan(0);
      expect(
        flow.every((e) => e.data!.base === line.id),
        line.id,
      ).toBe(true);
      for (const id of line.from) expect(visibleEdge(view, id), id).toBe(line.id);
    }
  });
});
