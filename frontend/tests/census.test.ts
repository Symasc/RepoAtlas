import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import type { RepositoriesResponse, TopologyOverviewResponse } from "@/lib/api/generated";
import { defaultExpanded, layoutDistricts, visibleCards } from "@/lib/graph/districtLayout";
import { districts, fanIns, hubThreshold, inclusionTrees, oversize, type District } from "@/lib/graph/districts";
import { fitZoom } from "@/lib/graph/fit";
import { buildOverview, repositoryId, type ViewEdge } from "@/lib/model/graph";
import { toRepositories, type RepositoryModel } from "@/lib/model/repositories";

import { ecosystem300 } from "./fixtures/ecosystem";

/**
 * A census of how the district rules cut a workspace: the fixture always, and
 * a real index when `RA_CORPUS` names a directory holding `repositories.json`
 * and `overview.json` (the API's envelopes, saved). One `DISTRICT_CENSUS` line
 * per corpus carries the numbers. Nothing here is a target: only that every
 * repository lands in exactly one district, and that a corpus file is an ok
 * envelope. No thresholds, no timing.
 */

const VIEWPORTS = [
  { width: 1440, height: 810 },
  { width: 1920, height: 1080 },
  { width: 2560, height: 1440 },
] as const;
const READABLE_ZOOMS = [0.75, 0.5] as const;

interface Corpus {
  name: string;
  repositories: RepositoryModel[];
  edges: ViewEdge[];
  /** How many inclusions the overview could not settle; null where there is no overview to ask. */
  unsettled: number | null;
}

const cmp = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);
const round4 = (n: number) => Math.round(n * 10000) / 10000;
const round = (n: number) => Math.round(n * 100) / 100;

/** The `result` of a saved API envelope; a file that is not an ok envelope is an error naming it. */
function readResult(file: string): unknown {
  const envelope = JSON.parse(readFileSync(file, "utf8")) as { ok?: unknown; result?: unknown };
  if (envelope.ok !== true) throw new Error(`${file} is not an ok envelope (ok is ${JSON.stringify(envelope.ok)})`);
  return envelope.result;
}

function readCorpus(directory: string): Corpus {
  const repositoriesFile = path.join(directory, "repositories.json");
  const overviewFile = path.join(directory, "overview.json");
  const repositories = toRepositories(readResult(repositoriesFile) as RepositoriesResponse);
  const overview = readResult(overviewFile) as TopologyOverviewResponse;
  const graph = buildOverview(repositories, overview.links ?? []);
  return { name: directory, repositories, edges: graph.edges, unsettled: overview.unsettled_inclusions_total ?? null };
}

const summarise = (ds: readonly District[], repositories: number) => {
  const largest = ds.reduce<District | null>((best, d) => (best === null || d.members.length > best.members.length ? d : best), null);
  return {
    districts: ds.map((d) => ({ id: d.id, kind: d.kind, size: d.members.length })),
    largest: largest === null ? null : { id: largest.id, size: largest.members.length, share: round4(largest.members.length / Math.max(1, repositories)) },
  };
};

function census(corpus: Corpus): void {
  const { repositories, edges } = corpus;
  const everyone = repositories.map((r) => repositoryId(r.slug)).sort(cmp);
  const before = districts(repositories, edges, { bound: false });
  const after = districts(repositories, edges);
  for (const ds of [before, after]) {
    const members = ds.flatMap((d) => d.members);
    expect(new Set(members).size, `${corpus.name}: a repository is in two districts`).toBe(members.length);
    expect([...members].sort(cmp), `${corpus.name}: every repository is in a district`).toEqual(everyone);
    const idList = ds.map((d) => d.id);
    expect(new Set(idList).size, `${corpus.name}: two districts share an id`).toBe(idList.length);
  }

  const linked = new Set(edges.flatMap((e) => [e.source, e.target]));
  const kinds = new Map<string, number>();
  for (const e of edges) kinds.set(e.link.kind, (kinds.get(e.link.kind) ?? 0) + 1);
  const trees = inclusionTrees(repositories, edges).map((t) => {
    const dirs = new Map<string, number>();
    for (const child of t.children) dirs.set(child.dir, (dirs.get(child.dir) ?? 0) + 1);
    return {
      root: t.root,
      directChildren: t.children.length,
      assigned: t.members.length,
      maxDepth: t.depth,
      topDirs: [...dirs].sort((a, b) => b[1] - a[1] || cmp(a[0], b[0])).slice(0, 12),
    };
  });
  const fanIn = [...fanIns(repositories, edges)].sort((a, b) => b[1] - a[1] || cmp(a[0], b[0])).slice(0, 15);

  const defaults = VIEWPORTS.flatMap((viewport) =>
    READABLE_ZOOMS.map((readableZoom) => {
      const result = defaultExpanded(after, viewport, { readableZoom });
      const shape = layoutDistricts(after, result.expanded, result.shelf);
      return {
        viewport,
        readableZoom,
        level: result.level,
        expandedIds: [...result.expanded].sort(cmp),
        visibleCards: visibleCards(after, result.expanded),
        fitZoom: round4(fitZoom(shape.extent, viewport)),
        extent: { width: round(shape.extent.width), height: round(shape.extent.height) },
        readable: result.readable,
      };
    }),
  );

  console.info(
    `DISTRICT_CENSUS ${JSON.stringify({
      corpus: corpus.name,
      repositories: repositories.length,
      linked: linked.size,
      unlinked: repositories.length - linked.size,
      linksByKind: Object.fromEntries([...kinds].sort((a, b) => cmp(a[0], b[0]))),
      inclusionEdges: kinds.get("submodule") ?? 0,
      unsettled_inclusions_total: corpus.unsettled,
      trees,
      fanIn,
      hubThreshold: hubThreshold(linked.size),
      before: summarise(before, repositories.length),
      after: summarise(after, repositories.length),
      oversize: oversize(after),
      defaults,
    })}`,
  );
}

describe("the district census", () => {
  it("cuts the fixture", () => {
    const { repositories, links } = ecosystem300();
    census({ name: "fixture ecosystem300", repositories, edges: buildOverview(repositories, links).edges, unsettled: null });
  });

  const directory = process.env.RA_CORPUS;
  if (directory) {
    it("cuts the corpus named by RA_CORPUS", { timeout: 120_000 }, () => {
      census(readCorpus(directory));
    });
  }
});
