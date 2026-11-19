import type { EdgeType, InclusionLink, RepositoryLink } from "@/lib/api/generated";
import { toFreshness } from "@/lib/model/freshness";
import { HUE_COUNT, type RepositoryModel } from "@/lib/model/repositories";

/**
 * A deterministic ecosystem of 300 repositories, shaped like the workspaces the
 * overview has to hold: three inclusion trees of different habits, a dense mesh
 * of six communities, hubs everything depends on, small components of their
 * own, and repositories with no links at all. The same call always returns the
 * same repositories and links: its own tiny generator, no clock, no
 * `Math.random`.
 */

export interface FixtureParts {
  /** Binary inclusion tree, depth 5, `.gitmodules`: `t1-00` is the root. */
  t1: readonly string[];
  /** West-style root `t2-00` with 30 direct children, and a chain reaching depth 5. */
  t2: readonly string[];
  /** Depth 5, git-subrepo: `t3-00` is the root. */
  t3: readonly string[];
  /** Every mesh repository, community by community. */
  mesh: readonly string[];
  /** The mesh by community, in order: neighbours are joined by one bridge each. */
  communities: readonly (readonly string[])[];
  /** Repositories 60 or more others depend on. */
  hubs: readonly string[];
  /** Ten linked pairs, then three triples. */
  small: readonly string[];
  /** `small` by component. */
  smallComponents: readonly (readonly string[])[];
  /** Six repositories (two in T1, one each in T2 and T3, two in the mesh) that 5 to 12 others in the other parts depend on. */
  libraries: readonly string[];
  /** No links at all. */
  independent: readonly string[];
}

function slugs(prefix: string, count: number): string[] {
  return Array.from({ length: count }, (_, i) => `${prefix}-${String(i).padStart(2, "0")}`);
}

const COMMUNITY_SIZES = [15, 15, 15, 15, 14, 14] as const;
const communities = COMMUNITY_SIZES.map((size, c) => slugs(`m${c}`, size));
const small = slugs("s", 29);
// ten pairs (s-00..s-19), then three triples (s-20..s-28)
const smallComponents = [
  ...Array.from({ length: 10 }, (_, k) => small.slice(2 * k, 2 * k + 2)),
  ...Array.from({ length: 3 }, (_, k) => small.slice(20 + 3 * k, 23 + 3 * k)),
];

export const FIXTURE_PARTS: FixtureParts = {
  t1: slugs("t1", 63),
  t2: slugs("t2", 37),
  t3: slugs("t3", 40),
  mesh: communities.flat(),
  communities,
  hubs: slugs("hub", 3),
  small,
  smallComponents,
  libraries: ["t1-03", "t1-10", "t2-05", "t3-02", "m3-04", "m0-11"],
  independent: slugs("i", 40),
};

/** Every slug, in part order. */
const ALL_SLUGS: readonly string[] = [
  ...FIXTURE_PARTS.t1,
  ...FIXTURE_PARTS.t2,
  ...FIXTURE_PARTS.t3,
  ...FIXTURE_PARTS.mesh,
  ...FIXTURE_PARTS.hubs,
  ...FIXTURE_PARTS.small,
  ...FIXTURE_PARTS.independent,
];

// -- a tiny seeded generator -----------------------------------------------------------

/** A 32-bit linear congruential generator (Numerical Recipes constants). */
function lcg(seed: number) {
  let state = seed >>> 0;
  /** In [0, 1). */
  const next = (): number => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state / 4294967296;
  };
  /** In [0, n). */
  const int = (n: number): number => Math.floor(next() * n);
  const pick = <T>(list: readonly T[]): T => list[int(list.length)]!;
  const shuffle = <T>(list: readonly T[]): T[] => {
    const out = [...list];
    for (let i = out.length - 1; i > 0; i--) {
      const j = int(i + 1);
      const held = out[i]!;
      out[i] = out[j]!;
      out[j] = held;
    }
    return out;
  };
  return { next, int, pick, shuffle };
}

type Rng = ReturnType<typeof lcg>;

function sha(rng: Rng): string {
  return Array.from({ length: 40 }, () => "0123456789abcdef".charAt(rng.int(16))).join("");
}

// -- repositories ----------------------------------------------------------------------

const LANGUAGES = ["TypeScript", "Python", "Go", "Rust", "Java", "C++"] as const;

function repository(slug: string, index: number, rng: Rng): RepositoryModel {
  const files = 20 + rng.int(4000);
  return {
    slug,
    name: slug,
    branch: "main",
    headSha: sha(rng),
    files,
    symbols: files * (3 + rng.int(6)),
    relationships: files * (5 + rng.int(20)),
    languages: [rng.pick(LANGUAGES)],
    manifests: ["package.json"],
    lockfiles: [],
    lastIndexedAt: null,
    hue: (index % HUE_COUNT) + 1,
    freshness: toFreshness({ state: "current_clean", depth: "live", repository_slug: slug }),
  };
}

// -- links -----------------------------------------------------------------------------

type InclusionFormat = "gitmodules" | "west" | "git-subrepo";

function inclusionOf(format: InclusionFormat, child: string, rng: Rng, path?: string): InclusionLink {
  const revision = sha(rng);
  const head = rng.next() < 0.8 ? revision : sha(rng);
  const common = { revision, head, in_sync: head === revision };
  if (format === "gitmodules") {
    return { declared_at: ".gitmodules", format, method: "path", path: `vendor/${child}`, url: null, ...common };
  }
  if (format === "west") {
    return {
      declared_at: "west.yml",
      format,
      method: "remote",
      path: path ?? `modules/${child}`,
      url: `https://git.example.com/org/${child}.git`,
      ...common,
    };
  }
  return { declared_at: `lib/${child}/.gitrepo`, format, method: "path", path: `lib/${child}`, url: null, ...common };
}

/** `parent` includes `child`: one inclusion, which is the whole link. */
function inclusion(parent: string, child: string, format: InclusionFormat, rng: Rng, path?: string): RepositoryLink {
  return {
    kind: "submodule",
    source: parent,
    target: child,
    count: 1,
    types: [{ type: "INCLUDES_REPOSITORY", count: 1 }],
    examples: [],
    inclusions: [inclusionOf(format, child, rng, path)],
    inclusions_total: 1,
  };
}

/** `count` cut into `k` parts of at least 1 each. */
function split(count: number, k: number, rng: Rng): number[] {
  const parts: number[] = [];
  let left = count;
  for (let i = 0; i < k - 1; i++) {
    const take = 1 + rng.int(left - (k - 1 - i));
    parts.push(take);
    left -= take;
  }
  parts.push(left);
  return parts;
}

const CODE_TYPES: readonly EdgeType[] = ["IMPORTS", "CALLS", "USES"];

function code(source: string, target: string, count: number, rng: Rng): RepositoryLink {
  const k = Math.min(count, 1 + rng.int(3));
  const names = rng.shuffle(CODE_TYPES).slice(0, k);
  const parts = split(count, k, rng);
  return {
    kind: "observed",
    source,
    target,
    count,
    types: names.map((type, i) => ({ type, count: parts[i]! })),
    examples: [`${source}/src/client.ts:${1 + rng.int(400)}`],
  };
}

function http(source: string, target: string, count: number): RepositoryLink {
  return {
    kind: "http",
    source,
    target,
    count,
    types: [{ type: "CALLS_HTTP_ROUTE", count }],
    examples: [`${source}/src/api.ts:${10 + (count % 90)}`],
  };
}

/** `source` depends on a package `target` provides; `declarations` is how many places say so. */
function dependency(source: string, target: string, declarations: number): RepositoryLink {
  return {
    kind: "dependency",
    source,
    target,
    count: declarations,
    types: [{ type: "DEPENDS_ON", count: declarations }],
    examples: [],
    packages: [
      {
        name: `@acme/${target}`,
        ecosystem: "npm",
        constraint: "^1.0.0",
        declarations,
        declared_at: "package.json",
        provided_at: "package.json",
      },
    ],
  };
}

/** Link weights across orders of magnitude: most small, a few in the thousands. */
function weight(rng: Rng): number {
  const r = rng.next();
  if (r < 0.5) return 1 + rng.int(30);
  if (r < 0.85) return 30 + rng.int(370);
  if (r < 0.97) return 400 + rng.int(2100);
  return 2500 + rng.int(6500);
}

// -- the ecosystem ---------------------------------------------------------------------

export function ecosystem300(): { repositories: RepositoryModel[]; links: RepositoryLink[] } {
  const rng = lcg(1);
  const repositories = ALL_SLUGS.map((slug, i) => repository(slug, i, rng));
  const links: RepositoryLink[] = [];
  const { t1, t2, t3, communities: groups, hubs, smallComponents: components } = FIXTURE_PARTS;

  /**
   * Pushes inclusion links; on every `every`th one it also pushes a parallel link
   * of another kind between the same two repositories, code and dependency
   * alternating (the same pair joined by two families, as real workspaces are).
   */
  const includer = (every: number) => {
    let pushed = 0;
    let parallels = 0;
    return (parent: string, child: string, format: InclusionFormat, path?: string) => {
      links.push(inclusion(parent, child, format, rng, path));
      pushed += 1;
      if (pushed % every !== 0) return;
      links.push(parallels % 2 === 0 ? code(parent, child, weight(rng), rng) : dependency(parent, child, 1 + rng.int(4)));
      parallels += 1;
    };
  };

  // T1: a binary tree, parents first (heap order); every 3rd inclusion has a parallel link
  const includeT1 = includer(3);
  for (let i = 1; i < t1.length; i++) {
    includeT1(t1[Math.floor((i - 1) / 2)]!, t1[i]!, "gitmodules");
  }

  // T2: a root with 30 direct children, one of which starts a chain down to depth 5;
  // every 4th inclusion has a parallel link. The root's children sit in directories by kind.
  const includeT2 = includer(4);
  for (let i = 1; i <= 30; i++) {
    const directory =
      i <= 12 ? "modules/hal" : i <= 20 ? "modules/lib" : i <= 25 ? "modules/crypto" : i <= 27 ? "bootloader" : "tools";
    includeT2(t2[0]!, t2[i]!, "west", `${directory}/${t2[i]!}`);
  }
  for (const [from, to] of [[1, 31], [31, 32], [32, 33], [33, 34], [1, 35], [1, 36]] as const) {
    includeT2(t2[from]!, t2[to]!, "west");
  }

  // T3: a binary tree of 40 (depth 5), parents first; every 3rd inclusion has a parallel link
  const includeT3 = includer(3);
  for (let i = 1; i < t3.length; i++) {
    includeT3(t3[Math.floor((i - 1) / 2)]!, t3[i]!, "git-subrepo");
  }
  // a shared child: t3-09 is also included by a leaf of T1
  links.push(inclusion(t1[45]!, t3[9]!, "gitmodules", rng));
  // a loop: t3-39 sits under t3-01 (via 19, 9 and 4), and includes it back
  links.push(inclusion(t3[39]!, t3[1]!, "git-subrepo", rng));

  // the mesh: every repository has at least three partners in its community
  for (const members of groups) {
    const n = members.length;
    const partners = new Map<string, Set<string>>(members.map((m) => [m, new Set<string>()]));
    const pairs: [string, string][] = [];
    const connect = (a: string, b: string) => {
      if (a === b || partners.get(a)!.has(b)) return;
      partners.get(a)!.add(b);
      partners.get(b)!.add(a);
      pairs.push([a, b]);
    };
    members.forEach((m, i) => connect(m, members[(i + 1) % n]!));
    for (const m of members) {
      const extra = 1 + rng.int(3);
      for (let k = 0; k < extra; k++) connect(m, rng.pick(members));
    }
    for (const m of members) {
      while (partners.get(m)!.size < 3) connect(m, rng.pick(members));
    }
    for (const [a, b] of pairs) {
      const forward = rng.next() < 0.5;
      links.push(code(forward ? a : b, forward ? b : a, weight(rng), rng));
      // a third of the pairs also call each other over HTTP
      if (rng.next() < 0.33) {
        const calls = rng.next() < 0.5;
        links.push(http(calls ? a : b, calls ? b : a, 1 + rng.int(120)));
      }
    }
  }
  // neighbouring communities: exactly one bridge each
  for (let c = 0; c + 1 < groups.length; c++) {
    links.push(code(rng.pick(groups[c]!), rng.pick(groups[c + 1]!), 3 + rng.int(60), rng));
  }

  // hubs: each a dependency of 66 distinct repositories, drawn from every part
  const strata: [readonly string[], number][] = [
    [t1, 15],
    [t2, 9],
    [t3, 10],
    [FIXTURE_PARTS.mesh, 32],
  ];
  for (const hub of hubs) {
    for (const [pool, take] of strata) {
      for (const source of rng.shuffle(pool).slice(0, take)) {
        links.push(dependency(source, hub, 1 + rng.int(4)));
      }
    }
  }

  // small components: ten linked pairs and three chains of three
  components.forEach((members, k) => {
    if (members.length === 2) {
      const [a, b] = members as [string, string];
      const kind = k % 3;
      if (kind === 0) links.push(code(a, b, weight(rng), rng));
      else if (kind === 1) links.push(http(a, b, 1 + rng.int(120)));
      else links.push(dependency(a, b, 1 + rng.int(4)));
    } else {
      const [a, b, c] = members as [string, string, string];
      links.push(code(a, b, weight(rng), rng));
      links.push(http(b, c, 1 + rng.int(120)));
    }
  });

  // libraries: depended on by 5 to 12 repositories of the other parts (never as many as a hub),
  // and a few light code links between parts, so the mesh is not sealed off
  const bodies = [t1, t2, t3, FIXTURE_PARTS.mesh];
  const taken = new Set(links.map((l) => `${l.kind}:${l.source}->${l.target}`));
  for (const library of FIXTURE_PARTS.libraries) {
    const own = bodies.find((body) => body.includes(library))!;
    const pool = bodies.filter((body) => body !== own).flat();
    const wanted = 5 + rng.int(8);
    const sources = rng.shuffle(pool).filter((s) => !taken.has(`dependency:${s}->${library}`));
    for (const source of sources.slice(0, wanted)) {
      links.push(dependency(source, library, 1 + rng.int(4)));
      taken.add(`dependency:${source}->${library}`);
    }
  }
  for (let made = 0, attempts = 0; made < 15 && attempts < 1000; attempts++) {
    const from = rng.int(bodies.length);
    const to = (from + 1 + rng.int(bodies.length - 1)) % bodies.length;
    const a = rng.pick(bodies[from]!);
    const b = rng.pick(bodies[to]!);
    if (taken.has(`observed:${a}->${b}`) || taken.has(`observed:${b}->${a}`)) continue;
    taken.add(`observed:${a}->${b}`);
    links.push(code(a, b, 1 + rng.int(30), rng));
    made += 1;
  }

  // as the API lists them: by slug
  repositories.sort((a, b) => (a.slug < b.slug ? -1 : a.slug > b.slug ? 1 : 0));
  return { repositories, links };
}
