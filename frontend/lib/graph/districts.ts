import { repositoryId, type ViewEdge } from "@/lib/model/graph";
import type { RepositoryModel } from "@/lib/model/repositories";

/**
 * How the overview's repositories fall into districts: inclusion trees (a
 * workspace that vendors others), the repositories everything uses, groups that
 * talk to each other, small groups, those linked only to the districts above,
 * and those with no links. Pure and total: every repository lands in exactly one
 * district, and the answer does not depend on the order repositories or edges
 * arrive in (every tie is broken by slug, in code-unit order).
 */

export type DistrictKind = "shared" | "tree" | "group" | "small" | "loose" | "unlinked";

export interface District {
  id: string;
  kind: DistrictKind;
  label: string;
  /** Repository node ids (`repo:<slug>`), in display order. */
  members: string[];
  /** For the tree kinds: the family's root node id; otherwise null. */
  root: string | null;
  /** Set only when a district was cut to a readable size: which part this is, counting from 1. */
  part: { index: number; of: number } | null;
}

/** The fewest others depending on a repository for it to be shared. */
export const HUB_MIN = 8;
/** Or this share of the linked repositories, whichever asks for more. */
export const HUB_SHARE = 0.1;
/** A root with this many tree children is looked at for splitting by directory. */
export const WIDE_ROOT = 24;
/** Children sharing a directory before it is a district of its own. */
export const MIN_DIR_GROUP = 3;
/** Repositories talking to each other before they are a group of their own. */
export const MIN_GROUP = 4;
/** The most a district holds before it is cut into parts (the groups and shared are never cut). */
export const DISTRICT_MAX = 24;

/** Code-unit order: the same on every machine, which a locale-aware comparison is not. */
const cmp = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

function push<K, V>(map: Map<K, V[]>, key: K, value: V): void {
  const list = map.get(key);
  if (list) list.push(value);
  else map.set(key, [value]);
}

function include<K, V>(map: Map<K, Set<V>>, key: K, value: V): void {
  const set = map.get(key);
  if (set) set.add(value);
  else map.set(key, new Set([value]));
}

/** What the rules read from the edges, indexed once. */
interface Facts {
  /** Every repository, by slug. */
  slugs: string[];
  names: Map<string, string>;
  /** Repositories with at least one edge. */
  linked: Set<string>;
  /** Inclusion edges, parent to child. */
  children: Map<string, Set<string>>;
  parents: Map<string, Set<string>>;
  /** Where a parent states a child's inclusion: the smallest path, or null when none is listed. */
  paths: Map<string, Map<string, string | null>>;
  /** Every other edge. */
  plain: { source: string; target: string; count: number }[];
  /** By repository: the distinct others with a non-inclusion edge into it. */
  fanIn: Map<string, Set<string>>;
}

function facts(repositories: readonly RepositoryModel[], edges: readonly ViewEdge[]): Facts {
  const names = new Map<string, string>();
  for (const r of repositories) if (!names.has(r.slug)) names.set(r.slug, r.name);
  const slugs = [...names.keys()].sort(cmp);
  const slugOf = new Map(slugs.map((s) => [repositoryId(s), s]));
  const f: Facts = {
    slugs,
    names,
    linked: new Set(),
    children: new Map(),
    parents: new Map(),
    paths: new Map(),
    plain: [],
    fanIn: new Map(),
  };
  for (const edge of edges) {
    const source = slugOf.get(edge.source);
    const target = slugOf.get(edge.target);
    if (source === undefined || target === undefined || source === target) continue;
    f.linked.add(source).add(target);
    if (edge.link.kind === "submodule") {
      include(f.children, source, target);
      include(f.parents, target, source);
      const own = f.paths.get(source) ?? new Map<string, string | null>();
      f.paths.set(source, own);
      if (!own.has(target)) own.set(target, null);
      for (const inclusion of edge.link.inclusions ?? []) {
        const smallest = own.get(target);
        if (smallest === null || smallest === undefined || cmp(inclusion.path, smallest) < 0) own.set(target, inclusion.path);
      }
    } else {
      f.plain.push({ source, target, count: edge.link.count });
      include(f.fanIn, target, source);
    }
  }
  return f;
}

// -- inclusion trees (R1, R2) ----------------------------------------------------------

interface Forest {
  /** In the order they were made roots. */
  roots: string[];
  /** Each assigned repository's root. */
  root: Map<string, string>;
  parent: Map<string, string | null>;
  depth: Map<string, number>;
  /** By repository: its tree children, by slug. */
  kids: Map<string, string[]>;
}

/**
 * The source components of the inclusion graph among `rest`: components with no
 * inclusion coming in from another one. A pure loop is one; each yields its
 * smallest slug.
 */
function loopRoots(rest: readonly string[], children: ReadonlyMap<string, ReadonlySet<string>>): string[] {
  const inRest = new Set(rest);
  const index = new Map<string, number>();
  const low = new Map<string, number>();
  const component = new Map<string, number>();
  const onStack = new Set<string>();
  const stack: string[] = [];
  let counter = 0;
  let components = 0;
  const visit = (v: string): void => {
    index.set(v, counter);
    low.set(v, counter);
    counter += 1;
    stack.push(v);
    onStack.add(v);
    for (const w of [...(children.get(v) ?? [])].sort(cmp)) {
      if (!inRest.has(w)) continue;
      if (!index.has(w)) {
        visit(w);
        low.set(v, Math.min(low.get(v)!, low.get(w)!));
      } else if (onStack.has(w)) {
        low.set(v, Math.min(low.get(v)!, index.get(w)!));
      }
    }
    if (low.get(v) === index.get(v)) {
      for (;;) {
        const w = stack.pop()!;
        onStack.delete(w);
        component.set(w, components);
        if (w === v) break;
      }
      components += 1;
    }
  };
  for (const v of rest) if (!index.has(v)) visit(v);

  const entered = new Set<number>();
  for (const v of rest) {
    for (const w of children.get(v) ?? []) {
      if (inRest.has(w) && component.get(v) !== component.get(w)) entered.add(component.get(w)!);
    }
  }
  const smallest = new Map<number, string>();
  for (const v of rest) {
    const c = component.get(v)!;
    const held = smallest.get(c);
    if (held === undefined || cmp(v, held) < 0) smallest.set(c, v);
  }
  return [...smallest].filter(([c]) => !entered.has(c)).map(([, slug]) => slug).sort(cmp);
}

function forest(f: Facts): Forest {
  const root = new Map<string, string>();
  const parent = new Map<string, string | null>();
  const depth = new Map<string, number>();
  const roots: string[] = [];

  /** Multi-source breadth-first assignment, level by level, over the repositories not yet assigned. */
  const grow = (sources: readonly string[]): void => {
    for (const s of sources) {
      root.set(s, s);
      parent.set(s, null);
      depth.set(s, 0);
      roots.push(s);
    }
    let frontier = [...sources];
    for (let d = 1; frontier.length > 0; d++) {
      const parentsAt = new Set(frontier);
      const reached = new Set<string>();
      for (const p of frontier) for (const c of f.children.get(p) ?? []) if (!root.has(c)) reached.add(c);
      const decided = [...reached].sort(cmp).map((c) => {
        const above = [...(f.parents.get(c) ?? [])].filter((p) => parentsAt.has(p));
        const chosen = above.map((p) => root.get(p)!).sort(cmp)[0]!;
        const tree = above.filter((p) => root.get(p) === chosen).sort(cmp)[0]!;
        return { c, chosen, tree };
      });
      for (const { c, chosen, tree } of decided) {
        root.set(c, chosen);
        parent.set(c, tree);
        depth.set(c, d);
      }
      frontier = decided.map((x) => x.c);
    }
  };

  grow(f.slugs.filter((s) => (f.children.get(s)?.size ?? 0) > 0 && !f.parents.has(s)));
  for (;;) {
    const rest = f.slugs.filter((s) => (f.children.has(s) || f.parents.has(s)) && !root.has(s));
    if (rest.length === 0) break;
    grow(loopRoots(rest, f.children));
  }

  const kids = new Map<string, string[]>();
  for (const [slug, tree] of parent) if (tree !== null) push(kids, tree, slug);
  for (const list of kids.values()) list.sort(cmp);
  return { roots, root, parent, depth, kids };
}

/** `slug` and everything below it in the tree, in pre-order, each repository's children by slug. */
function preorder(slug: string, kids: ReadonlyMap<string, readonly string[]>): string[] {
  const out: string[] = [];
  const stack = [slug];
  while (stack.length > 0) {
    const at = stack.pop()!;
    out.push(at);
    const below = kids.get(at) ?? [];
    for (let i = below.length - 1; i >= 0; i--) stack.push(below[i]!);
  }
  return out;
}

/** The directory a parent states a child's inclusion in: its smallest listed path up to the last "/", or "". */
function directoryOf(f: Facts, parent: string, child: string): string {
  const path = f.paths.get(parent)?.get(child) ?? "";
  const cut = path.lastIndexOf("/");
  return cut < 0 ? "" : path.slice(0, cut);
}

export interface InclusionTree {
  /** The family's root slug. */
  root: string;
  /** Every member's slug, in pre-order. */
  members: string[];
  /** The longest chain of inclusions from the root. */
  depth: number;
  /** The root's own tree children, each with the directory it is included in ("" when unstated). */
  children: { slug: string; dir: string }[];
}

/** The inclusion families the district rules start from, for reports. */
export function inclusionTrees(repositories: readonly RepositoryModel[], edges: readonly ViewEdge[]): InclusionTree[] {
  const f = facts(repositories, edges);
  const fo = forest(f);
  return fo.roots.map((root) => {
    const members = preorder(root, fo.kids);
    return {
      root,
      members,
      depth: Math.max(...members.map((m) => fo.depth.get(m)!)),
      children: (fo.kids.get(root) ?? []).map((slug) => ({ slug, dir: directoryOf(f, root, slug) })),
    };
  });
}

// -- shared (R4) -----------------------------------------------------------------------

/** Dependents a repository needs to be shared, among `linked` repositories with a link. */
export function hubThreshold(linked: number): number {
  // the epsilon keeps ceil(0.1 * 30) at 3 whatever the float product rounds to
  return Math.max(HUB_MIN, Math.ceil(HUB_SHARE * linked - 1e-9));
}

/** By repository, the number of distinct others with a non-inclusion edge into it; only those above zero. */
export function fanIns(repositories: readonly RepositoryModel[], edges: readonly ViewEdge[]): Map<string, number> {
  const f = facts(repositories, edges);
  return new Map([...f.fanIn].map(([slug, from]) => [slug, from.size]));
}

// -- the districts ---------------------------------------------------------------------

interface Draft {
  id: string;
  kind: DistrictKind;
  label: string;
  members: string[];
  root: string | null;
  /** The pieces that may not be cut apart, when members are not each their own (small groups). */
  units?: string[][];
  part?: { index: number; of: number };
}

/**
 * Ids are built from escaped components: "/", ":" and "#" never survive inside
 * one, so a slug or directory with a slash cannot collide with another id.
 */
const enc = encodeURIComponent;

function treeDrafts(root: string, f: Facts, fo: Forest): Draft[] {
  const rootName = f.names.get(root)!;
  const whole = (list: readonly string[]): string[] => list.flatMap((k) => preorder(k, fo.kids));
  const kids = fo.kids.get(root) ?? [];
  if (kids.length >= WIDE_ROOT) {
    const byDir = new Map<string, string[]>();
    for (const kid of kids) {
      const dir = directoryOf(f, root, kid);
      if (dir !== "") push(byDir, dir, kid);
    }
    const groups = [...byDir]
      .filter(([, list]) => list.length >= MIN_DIR_GROUP)
      .map(([dir, list]) => ({ dir, list, members: whole(list) }));
    if (groups.length >= 2) {
      groups.sort((a, b) => b.members.length - a.members.length || cmp(a.dir, b.dir));
      const grouped = new Set(groups.flatMap((g) => g.list));
      const rest = whole(kids.filter((k) => !grouped.has(k)));
      const drafts: Draft[] = [{ id: `district:tree:${enc(root)}`, kind: "tree", label: rootName, members: [root], root }];
      for (const g of groups) {
        drafts.push({ id: `district:tree:${enc(root)}/${enc(g.dir)}`, kind: "tree", label: `${rootName} · ${g.dir}`, members: g.members, root });
      }
      if (rest.length > 0) {
        drafts.push({ id: `district:tree:${enc(root)}:other`, kind: "tree", label: `${rootName} · other`, members: rest, root });
      }
      return drafts;
    }
  }
  return [{ id: `district:tree:${enc(root)}`, kind: "tree", label: rootName, members: preorder(root, fo.kids), root }];
}

/** Cuts an oversize district into parts of about equal size, never through a unit. */
function cut(d: Draft): Draft[] {
  const cuttable = d.kind === "tree" || d.kind === "small" || d.kind === "loose" || d.kind === "unlinked";
  const n = d.members.length;
  if (!cuttable || n <= DISTRICT_MAX) return [d];
  const parts = Math.ceil(n / DISTRICT_MAX);
  const target = Math.ceil(n / parts);
  const pieces: string[][] = [];
  let current: string[] = [];
  for (const unit of d.units ?? d.members.map((m) => [m])) {
    if (current.length > 0 && current.length + unit.length > target) {
      pieces.push(current);
      current = [];
    }
    current.push(...unit);
  }
  if (current.length > 0) pieces.push(current);
  return pieces.map((members, k) => ({
    id: `${d.id}#${k + 1}`,
    kind: d.kind,
    label: `${d.label} · ${k + 1} of ${pieces.length}`,
    members,
    root: d.root,
    part: { index: k + 1, of: pieces.length },
  }));
}

export function districts(
  repositories: readonly RepositoryModel[],
  edges: readonly ViewEdge[],
  options: { bound?: boolean } = {},
): District[] {
  const f = facts(repositories, edges);
  const fo = forest(f);
  const nameOf = (slug: string): string => f.names.get(slug)!;
  const drafts: Draft[] = [];

  // R4: shared
  const threshold = hubThreshold(f.linked.size);
  const fanOf = (slug: string): number => f.fanIn.get(slug)?.size ?? 0;
  const hubs = f.slugs
    .filter((s) => f.linked.has(s) && !fo.root.has(s) && fanOf(s) >= threshold)
    .sort((a, b) => fanOf(b) - fanOf(a) || cmp(a, b));
  if (hubs.length > 0) drafts.push({ id: "district:shared", kind: "shared", label: "Used by many", members: hubs, root: null });

  // R1–R3: one family per root, the biggest first
  const families = fo.roots.map((root) => treeDrafts(root, f, fo));
  const familySize = (family: Draft[]): number => family.reduce((sum, d) => sum + d.members.length, 0);
  families.sort((a, b) => familySize(b) - familySize(a) || cmp(a[0]!.root!, b[0]!.root!));
  for (const family of families) drafts.push(...family);

  // R5: what talks together, among what is left
  const hubSet = new Set(hubs);
  const remaining = new Set(f.slugs.filter((s) => f.linked.has(s) && !fo.root.has(s) && !hubSet.has(s)));
  const partners = new Map<string, Set<string>>();
  const weight = new Map<string, number>();
  for (const { source, target, count } of f.plain) {
    if (!remaining.has(source) || !remaining.has(target)) continue;
    include(partners, source, target);
    include(partners, target, source);
    weight.set(source, (weight.get(source) ?? 0) + count);
    weight.set(target, (weight.get(target) ?? 0) + count);
  }
  const seen = new Set<string>();
  const components: string[][] = [];
  for (const start of [...remaining].sort(cmp)) {
    if (seen.has(start)) continue;
    seen.add(start);
    const members = [start];
    for (let i = 0; i < members.length; i++) {
      for (const next of partners.get(members[i]!) ?? []) {
        if (seen.has(next)) continue;
        seen.add(next);
        members.push(next);
      }
    }
    components.push(members.sort(cmp));
  }
  const reach = (slug: string): number => partners.get(slug)?.size ?? 0;
  const led = (a: string, b: string): number => reach(b) - reach(a) || (weight.get(b) ?? 0) - (weight.get(a) ?? 0) || cmp(a, b);
  const groups: Draft[] = [];
  const small: string[][] = [];
  const loose: string[] = [];
  for (const members of components) {
    if (members.length >= MIN_GROUP) {
      const ordered = [...members].sort(led);
      const leader = ordered[0]!;
      groups.push({
        id: `district:group:${enc(leader)}`,
        kind: "group",
        label: `${nameOf(leader)} and ${members.length - 1} others`,
        members: ordered,
        root: null,
      });
    } else if (members.length >= 2) {
      small.push(members);
    } else {
      loose.push(members[0]!);
    }
  }
  groups.sort((a, b) => b.members.length - a.members.length || cmp(a.id, b.id));
  drafts.push(...groups);
  if (small.length > 0) {
    small.sort((a, b) => b.length - a.length || cmp(a[0]!, b[0]!));
    drafts.push({ id: "district:small", kind: "small", label: "Small groups", members: small.flat(), root: null, units: small });
  }
  if (loose.length > 0) {
    drafts.push({ id: "district:loose", kind: "loose", label: "Linked elsewhere", members: loose.sort(cmp), root: null });
  }

  // R6: no links
  const unlinked = f.slugs.filter((s) => !f.linked.has(s));
  if (unlinked.length > 0) {
    drafts.push({ id: "district:unlinked", kind: "unlinked", label: "No links recorded with the others", members: unlinked, root: null });
  }

  // R7: cut what is too big to read
  const bounded = (options.bound ?? true) ? drafts.flatMap(cut) : drafts;
  return bounded.map((d) => ({
    id: d.id,
    kind: d.kind,
    label: d.label,
    members: d.members.map(repositoryId),
    root: d.root === null ? null : repositoryId(d.root),
    part: d.part ?? null,
  }));
}

/** The ids of the districts holding more than `DISTRICT_MAX` repositories. */
export function oversize(ds: readonly District[]): string[] {
  return ds.filter((d) => d.members.length > DISTRICT_MAX).map((d) => d.id);
}
