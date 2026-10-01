import type {
  ChildEntry,
  ChildLink,
  EdgeTypeCount,
  LinkKind,
  RepositoryLink,
  TopologyChildrenResponse,
} from "@/lib/api/generated";

import type { RepositoryModel } from "./repositories";

/**
 * The view graph: what each level draws, before React Flow sees it. Pure, so the
 * rules that matter (kinds never summed, a link drawn between the right two
 * things, what is shown at rest and what only on demand) are tested without a
 * browser.
 */

export type { LinkKind };

export interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}

// -- how a link reads ---------------------------------------------------------------

/**
 * A link's family, which picks its stroke: the index's kind, with configuration
 * (environment variables, config keys) told apart from code. Families are never
 * summed into one line.
 */
export type Family = "code" | "http" | "dependency" | "config";
export const FAMILIES: readonly Family[] = ["code", "http", "dependency", "config"];
export type FamilyCounts = Partial<Record<Family, number>>;

const CONFIG_ENTITIES: ReadonlySet<string> = new Set(["env_var", "config"]);
const CONFIG_EDGES: ReadonlySet<string> = new Set(["READS", "WRITES", "CONFIGURES"]);

export function familyOf(
  kind: LinkKind,
  entityType: string | null | undefined,
  types: readonly EdgeTypeCount[] = [],
): Family {
  if (kind === "http") return "http";
  if (kind === "dependency") return "dependency";
  if (entityType && CONFIG_ENTITIES.has(entityType)) return "config";
  if (types.length > 0 && types.every((t) => CONFIG_EDGES.has(t.type))) return "config";
  return "code";
}

export interface LinkData {
  kind: LinkKind;
  family: Family;
  count: number;
  types: EdgeTypeCount[];
  /** Package names on a dependency link, when the overview names them. */
  packages: string[];
  /** Up to a few exact edge references, to read the lines behind the link. */
  examples: string[];
  /** What the line says instead of its count: how a trace's step reads. */
  label?: string;
}

/** Examples kept per merged link: enough to show evidence, never a dump. */
const EXAMPLES = 6;

function linkOf(link: ChildLink | RepositoryLink, family: Family, packages: string[] = []): LinkData {
  return {
    kind: link.kind,
    family,
    count: link.count,
    types: (link.types ?? []).map((t) => ({ ...t })),
    packages,
    examples: [...(link.examples ?? [])].slice(0, EXAMPLES),
  };
}

function merge(into: LinkData, more: LinkData): void {
  into.count += more.count;
  for (const t of more.types) {
    const same = into.types.find((u) => u.type === t.type);
    if (same) same.count += t.count;
    else into.types.push({ ...t });
  }
  into.types.sort((a, b) => b.count - a.count || a.type.localeCompare(b.type));
  for (const ref of more.examples) {
    if (into.examples.length >= EXAMPLES) break;
    if (!into.examples.includes(ref)) into.examples.push(ref);
  }
  for (const name of more.packages) if (!into.packages.includes(name)) into.packages.push(name);
}

export interface ViewEdge {
  id: string;
  source: string;
  target: string;
  link: LinkData;
  /**
   * `detail` is one thing's own link, drawn only while that thing is pointed at
   * or selected; `aggregate` is a level's total towards something outside it,
   * drawn at rest. Absent: drawn as it is.
   */
  scope?: "detail" | "aggregate";
}

/** What a row in the inspector's connection list says, and what draws it. */
export interface Connection {
  id: string;
  /** The canvas node at the other end. */
  node: string;
  /** The line that draws this connection. */
  edge: string;
  direction: "outgoing" | "incoming";
  partner: Partner;
  /** Which part of the subject holds it: a file in the files card, a declaration. */
  via: string | null;
  link: LinkData;
}

export type PartnerKind = "repository" | "directory" | "file" | "shared" | "place" | "more";

export interface Partner {
  kind: PartnerKind;
  label: string;
  detail: string | null;
  /** For something shared across the index: what kind of thing it is. */
  entityType?: string;
  /** Where going to the partner leads, when it can be gone to. */
  repository?: string;
  path?: string;
}

const FAMILY_ORDER: Record<Family, number> = { code: 0, http: 1, dependency: 2, config: 3 };

/** Strongest first within a family, families in reading order. */
export function byStrength(a: Connection, b: Connection): number {
  return (
    FAMILY_ORDER[a.link.family] - FAMILY_ORDER[b.link.family] ||
    b.link.count - a.link.count ||
    a.partner.label.localeCompare(b.partner.label)
  );
}

function push<K, V>(map: Map<K, V[]>, key: K, value: V): void {
  const list = map.get(key);
  if (list) list.push(value);
  else map.set(key, [value]);
}

function basename(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1);
}

function dirname(path: string): string {
  const cut = path.lastIndexOf("/");
  return cut > 0 ? path.slice(0, cut) : "";
}

// -- the overview: repositories, and the links between them ----------------------

export interface OverviewGraph {
  /** Repositories with at least one link. */
  linked: RepositoryModel[];
  /** Repositories with none: independent is a finding, not a gap. */
  independent: RepositoryModel[];
  edges: ViewEdge[];
  /** By repository node: every link it takes part in. */
  connections: Map<string, Connection[]>;
}

export function repositoryId(slug: string): string {
  return `repo:${slug}`;
}

export function buildOverview(
  repositories: readonly RepositoryModel[],
  links: readonly RepositoryLink[],
): OverviewGraph {
  const known = new Map(repositories.map((r) => [r.slug, r]));
  const edges: ViewEdge[] = [];
  const connections = new Map<string, Connection[]>();
  const touched = new Set<string>();
  for (const link of links) {
    if (!known.has(link.source) || !known.has(link.target) || link.source === link.target) continue;
    // one edge per kind and direction: an HTTP call and a package dependency
    // between the same two repositories are two facts, drawn twice
    const id = `${link.kind}:${link.source}->${link.target}`;
    const source = repositoryId(link.source);
    const target = repositoryId(link.target);
    const data = linkOf(link, familyOf(link.kind, null, link.types ?? []), (link.packages ?? []).map((p) => p.name));
    edges.push({ id, source, target, link: data });
    const name = (slug: string) => known.get(slug)!.name;
    push(connections, source, {
      id: `${id}:out`,
      node: target,
      edge: id,
      direction: "outgoing",
      partner: { kind: "repository", label: name(link.target), detail: null, repository: link.target, path: "" },
      via: null,
      link: data,
    });
    push(connections, target, {
      id: `${id}:in`,
      node: source,
      edge: id,
      direction: "incoming",
      partner: { kind: "repository", label: name(link.source), detail: null, repository: link.source, path: "" },
      via: null,
      link: data,
    });
    touched.add(link.source);
    touched.add(link.target);
  }
  for (const list of connections.values()) list.sort(byStrength);
  return {
    linked: repositories.filter((r) => touched.has(r.slug)),
    independent: repositories.filter((r) => !touched.has(r.slug)),
    edges,
    connections,
  };
}

// -- a repository opened at a directory ---------------------------------------------

export const PREVIEW_ROWS = 5;
export const FILE_ROWS = 8;
/** Nodes per side column before the rest fold into one "more" node. */
export const SIDE_LIMIT = 9;

export const FRAME_ID = "frame";
export const FILES_ID = "files";

export interface FrameNode {
  id: typeof FRAME_ID;
  name: string;
  path: string;
  files: number;
  directories: number;
  leaves: number;
}

export interface DirectoryCard {
  id: string;
  path: string;
  name: string;
  files: number;
  /** The directory's first entries, once its preview has loaded. */
  preview: ChildEntry[] | null;
  previewTotal: number;
  /** Every link touching it, by family: what a card says without a line. */
  summary: FamilyCounts;
}

export interface FilesCard {
  id: typeof FILES_ID;
  entries: ChildEntry[];
  total: number;
  summary: FamilyCounts;
}

export interface SharedNode {
  id: string;
  entityType: string;
  label: string;
  count: number;
}

export interface PeerNode {
  id: string;
  repository: string;
  count: number;
}

/** Somewhere else in this repository that entries here link to. */
export interface PlaceNode {
  id: string;
  path: string;
  count: number;
}

/** What did not fit in a side column, folded into one node. */
export interface MoreNode {
  id: string;
  side: "left" | "right";
  nodes: number;
  count: number;
}

export interface OpenedGraph {
  frame: FrameNode;
  directories: DirectoryCard[];
  files: FilesCard | null;
  /** Right column: things shared across the index. */
  shared: SharedNode[];
  /** Left column: other repositories, then elsewhere in this one. */
  peers: PeerNode[];
  places: PlaceNode[];
  more: MoreNode[];
  edges: ViewEdge[];
  /** By node: every link it takes part in, for the inspector. */
  connections: Map<string, Connection[]>;
  /** Links the canvas has nowhere to draw, counted rather than hidden. */
  undrawn: number;
}

const SHARED_LABELS: Record<string, string> = {
  module: "External modules",
  package: "Packages",
  env_var: "Environment variables",
  command: "Commands",
  config: "Configuration",
  http_endpoint: "HTTP endpoints",
  http_route: "HTTP routes",
  http_deployment: "HTTP deployments",
  service: "Services",
  template: "Templates",
  symbol: "Symbols",
};

export function sharedLabel(entityType: string | null | undefined): string {
  if (!entityType) return "Shared";
  return SHARED_LABELS[entityType] ?? entityType.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

export function directoryId(path: string): string {
  return `dir:${path}`;
}

/** Directories first, hidden ones (`.github`) after the rest, each group by name. */
function byName(a: ChildEntry, b: ChildEntry): number {
  const hidden = Number(a.name.startsWith(".")) - Number(b.name.startsWith("."));
  return hidden !== 0 ? hidden : a.name.localeCompare(b.name);
}

function add(counts: FamilyCounts, family: Family, count: number): void {
  counts[family] = (counts[family] ?? 0) + count;
}

/** Where a far end is drawn, before the side columns are cut to size. */
type Far =
  | { kind: "card"; id: string }
  | { kind: "shared"; id: string; entityType: string }
  | { kind: "peer"; id: string; repository: string }
  | { kind: "place"; id: string; path: string };

export function buildOpened(input: {
  repository: RepositoryModel;
  path: string;
  children: TopologyChildrenResponse;
  previews: ReadonlyMap<string, TopologyChildrenResponse>;
}): OpenedGraph {
  const { repository, path, children, previews } = input;
  const entries = [...(children.entries ?? [])];
  const directories: DirectoryCard[] = entries
    .filter((e) => e.kind === "directory")
    .sort(byName)
    .map((e) => {
      const preview = previews.get(e.path);
      return {
        id: directoryId(e.path),
        path: e.path,
        name: e.name,
        files: e.files ?? 0,
        preview: preview ? [...(preview.entries ?? [])].slice(0, PREVIEW_ROWS) : null,
        previewTotal: preview?.entries_total ?? 0,
        summary: {},
      };
    });
  const leaves = entries.filter((e) => e.kind !== "directory");
  const files: FilesCard | null =
    leaves.length === 0 ? null : { id: FILES_ID, entries: leaves, total: leaves.length, summary: {} };
  const frame: FrameNode = {
    id: FRAME_ID,
    name: path ? basename(path) : repository.name,
    path,
    files: directories.reduce((sum, d) => sum + d.files, 0) + leaves.length,
    directories: directories.length,
    leaves: leaves.length,
  };

  // where each child is drawn: its own card, or the files card
  const cardOf = new Map<string, string>();
  for (const d of directories) cardOf.set(d.path, d.id);
  for (const leaf of leaves) cardOf.set(leaf.path, FILES_ID);
  /** The card holding a path: the entry itself, or the directory it is inside. */
  const cardFor = (at: string): string | undefined => {
    for (let p = at; p; p = dirname(p)) {
      const id = cardOf.get(p);
      if (id !== undefined) return id;
    }
    return undefined;
  };
  const cardById = new Map<string, DirectoryCard | FilesCard>(directories.map((d) => [d.id, d]));
  if (files) cardById.set(FILES_ID, files);

  // first pass: what each link joins, and how much reaches each far end
  let undrawn = 0;
  const resolved: { link: ChildLink; near: string; far: Far; family: Family; at: string | null }[] = [];
  const reach = new Map<string, number>();
  for (const link of children.links ?? []) {
    const near = cardFor(link.child);
    const peer = link.peer;
    let far: Far | null = null;
    if (near !== undefined) {
      if (peer.kind === "shared") {
        const entityType = peer.entity_type ?? "shared";
        far = { kind: "shared", id: `shared:${entityType}`, entityType };
      } else if (peer.kind === "repository" || (peer.repository && peer.repository !== repository.slug)) {
        if (peer.repository) far = { kind: "peer", id: `peer:${peer.repository}`, repository: peer.repository };
      } else if (peer.path) {
        const card = cardFor(peer.path);
        far = card !== undefined ? { kind: "card", id: card } : { kind: "place", id: `place:${peer.path}`, path: peer.path };
      }
    }
    if (near === undefined || far === null) {
      undrawn += 1;
      continue;
    }
    if (far.id === near) continue; // within one card: the files card, or inside a directory
    const family = familyOf(link.kind, peer.entity_type, link.types ?? []);
    resolved.push({ link, near, far, family, at: peer.path ?? null });
    reach.set(far.id, (reach.get(far.id) ?? 0) + link.count);
  }

  // the side columns: other repositories first, then the strongest places;
  // what does not fit folds into one node per side
  const shared = new Map<string, SharedNode>();
  const peers = new Map<string, PeerNode>();
  const places = new Map<string, PlaceNode>();
  for (const { far } of resolved) {
    const count = reach.get(far.id) ?? 0;
    if (far.kind === "shared" && !shared.has(far.id)) {
      shared.set(far.id, { id: far.id, entityType: far.entityType, label: sharedLabel(far.entityType), count });
    } else if (far.kind === "peer" && !peers.has(far.id)) {
      peers.set(far.id, { id: far.id, repository: far.repository, count });
    } else if (far.kind === "place" && !places.has(far.id)) {
      places.set(far.id, { id: far.id, path: far.path, count });
    }
  }
  const strongest = <T extends { count: number; id: string }>(a: T, b: T) => b.count - a.count || a.id.localeCompare(b.id);
  const left = [...[...peers.values()].sort(strongest), ...[...places.values()].sort(strongest)];
  const right = [...shared.values()].sort(strongest);
  const folded = new Map<string, string>();
  const more: MoreNode[] = [];
  const fold = (column: { id: string; count: number }[], side: "left" | "right") => {
    if (column.length <= SIDE_LIMIT) return column;
    const kept = column.slice(0, SIDE_LIMIT - 1);
    const rest = column.slice(SIDE_LIMIT - 1);
    const id = `more:${side}`;
    for (const node of rest) folded.set(node.id, id);
    more.push({ id, side, nodes: rest.length, count: rest.reduce((sum, n) => sum + n.count, 0) });
    return kept;
  };
  const keptLeft = new Set(fold(left, "left").map((n) => n.id));
  const keptRight = new Set(fold(right, "right").map((n) => n.id));

  const partnerOf = (id: string, via: string | null): Partner => {
    if (id === FILES_ID) {
      return via
        ? { kind: "file", label: basename(via), detail: dirname(via) || null, repository: repository.slug, path: via }
        : { kind: "file", label: "Files here", detail: null };
    }
    const card = cardById.get(id);
    if (card && "path" in card) {
      return { kind: "directory", label: card.name, detail: null, repository: repository.slug, path: card.path };
    }
    if (id.startsWith("shared:")) {
      const node = shared.get(id);
      return { kind: "shared", label: node?.label ?? id, detail: null, entityType: node?.entityType };
    }
    if (id.startsWith("peer:")) {
      const slug = id.slice("peer:".length);
      return { kind: "repository", label: slug, detail: null, repository: slug, path: "" };
    }
    if (id.startsWith("place:")) {
      const at = id.slice("place:".length);
      return { kind: "place", label: basename(at), detail: dirname(at) || null, repository: repository.slug, path: at };
    }
    return { kind: "more", label: id, detail: null };
  };

  // second pass: lines and connection rows
  const edges = new Map<string, ViewEdge>();
  const connections = new Map<string, Connection[]>();
  const draw = (id: string, source: string, target: string, data: LinkData, scope: ViewEdge["scope"]) => {
    const existing = edges.get(id);
    if (existing) merge(existing.link, data);
    else edges.set(id, { id, source, target, link: { ...data, types: data.types.map((t) => ({ ...t })), examples: [...data.examples] }, ...(scope ? { scope } : {}) });
  };
  resolved.forEach(({ link, near, far, family, at }, index) => {
    const drawn = folded.get(far.id) ?? far.id;
    const data = linkOf(link, family);
    const outgoing = link.direction !== "incoming";
    const [source, target] = outgoing ? [near, drawn] : [drawn, near];
    const edge = `${family}:${source}->${target}`;
    draw(edge, source, target, data, "detail");
    if (far.kind !== "card") {
      const [from, to] = outgoing ? [FRAME_ID, drawn] : [drawn, FRAME_ID];
      draw(`${family}:${from}=>${to}`, from, to, data, "aggregate");
    }
    const nearCard = cardById.get(near)!;
    add(nearCard.summary, family, link.count);
    if (far.kind === "card") add(cardById.get(far.id)!.summary, family, link.count);

    const via = near === FILES_ID ? link.child : null;
    // a file on the files card is named, not the card
    const nearPartner = partnerOf(far.id, far.id === FILES_ID ? at : null);
    push(connections, near, {
      id: `${index}:near`,
      node: drawn,
      edge,
      direction: outgoing ? "outgoing" : "incoming",
      partner: nearPartner,
      via: via ? basename(via) : null,
      link: data,
    });
    const farRow: Connection = {
      id: `${index}:far`,
      node: near,
      edge,
      direction: outgoing ? "incoming" : "outgoing",
      partner: partnerOf(near, via),
      via: null,
      link: data,
    };
    push(connections, far.id, farRow);
    // a folded node lists each place it stands for, and which card reaches it
    if (drawn !== far.id) push(connections, drawn, { ...farRow, id: `${index}:more`, partner: nearPartner, via: farRow.partner.label });
  });
  for (const list of connections.values()) list.sort(byStrength);

  return {
    frame,
    directories,
    files,
    shared: right.filter((n) => keptRight.has(n.id)) as SharedNode[],
    peers: [...peers.values()].filter((n) => keptLeft.has(n.id)).sort(strongest),
    places: [...places.values()].filter((n) => keptLeft.has(n.id)).sort(strongest),
    more,
    edges: [...edges.values()],
    connections,
    undrawn,
  };
}

// -- a file: its neighbourhood ------------------------------------------------------

export const FILE_ID = "file";
/** Neighbours shown per side before the rest fold into "more". */
export const NEIGHBOURS = 7;
export const NEIGHBOURS_EXPANDED = 40;

export interface Neighbour {
  id: string;
  kind: "file" | "repository" | "shared";
  label: string;
  detail: string | null;
  count: number;
  side: "left" | "right";
  /** For a file: its path; for a repository: its slug; for shared: the entity type. */
  target: string;
}

export interface FileGraph {
  anchor: { id: typeof FILE_ID; path: string; name: string; declarations: number };
  /** Left: what links to the file. Right: what the file links to. */
  left: Neighbour[];
  right: Neighbour[];
  more: MoreNode[];
  edges: ViewEdge[];
  /** Every connection of the file, strongest first. */
  connections: Connection[];
  /** By neighbour node (and "more" nodes): its connections with the file. */
  byNode: Map<string, Connection[]>;
  /** Links between parts of this one file. */
  within: number;
  undrawn: number;
}

export function buildFile(input: {
  repository: RepositoryModel;
  path: string;
  children: TopologyChildrenResponse;
  /** Per side, how many neighbours to show before folding the rest. */
  limit?: { left: number; right: number };
}): FileGraph {
  const { repository, path, children } = input;
  const limit = input.limit ?? { left: NEIGHBOURS, right: NEIGHBOURS };
  const declarations = (children.entries ?? []).filter((e) => e.kind === "declaration");
  const names = new Map(declarations.map((d) => [d.key ?? d.id, d.name]));

  let within = 0;
  let undrawn = 0;
  type Row = { neighbour: Omit<Neighbour, "side" | "count">; link: ChildLink; family: Family; outgoing: boolean };
  const rows: Row[] = [];
  for (const link of children.links ?? []) {
    const peer = link.peer;
    let neighbour: Row["neighbour"] | null = null;
    if (peer.kind === "shared") {
      const entityType = peer.entity_type ?? "shared";
      neighbour = { id: `shared:${entityType}`, kind: "shared", label: sharedLabel(entityType), detail: null, target: entityType };
    } else if (peer.kind === "repository" || (peer.repository && peer.repository !== repository.slug)) {
      if (peer.repository) {
        neighbour = { id: `peer:${peer.repository}`, kind: "repository", label: peer.repository, detail: null, target: peer.repository };
      }
    } else if (peer.path === path) {
      within += 1;
      continue;
    } else if (peer.path) {
      neighbour = {
        id: `file:${peer.path}`,
        kind: "file",
        label: basename(peer.path),
        detail: dirname(peer.path) || null,
        target: peer.path,
      };
    }
    if (neighbour === null) {
      undrawn += 1;
      continue;
    }
    rows.push({ neighbour, link, family: familyOf(link.kind, peer.entity_type, link.types ?? []), outgoing: link.direction !== "incoming" });
  }

  // one node per neighbour, on the side of most of its traffic
  const totals = new Map<string, { node: Row["neighbour"]; out: number; in: number }>();
  for (const row of rows) {
    const t = totals.get(row.neighbour.id) ?? { node: row.neighbour, out: 0, in: 0 };
    if (row.outgoing) t.out += row.link.count;
    else t.in += row.link.count;
    totals.set(row.neighbour.id, t);
  }
  const all: Neighbour[] = [...totals.values()].map(({ node, out, in: incoming }) => ({
    ...node,
    count: out + incoming,
    side: incoming > out ? "left" : "right",
  }));
  const strongest = (a: Neighbour, b: Neighbour) => b.count - a.count || a.label.localeCompare(b.label);
  const folded = new Map<string, string>();
  const more: MoreNode[] = [];
  const column = (side: "left" | "right") => {
    const list = all.filter((n) => n.side === side).sort(strongest);
    const max = limit[side];
    if (list.length <= max) return list;
    const kept = list.slice(0, Math.max(max - 1, 0));
    const rest = list.slice(kept.length);
    const id = `more:${side}`;
    for (const n of rest) folded.set(n.id, id);
    more.push({ id, side, nodes: rest.length, count: rest.reduce((sum, n) => sum + n.count, 0) });
    return kept;
  };
  const left = column("left");
  const right = column("right");

  const edges = new Map<string, ViewEdge>();
  const merged = new Map<string, Connection>();
  for (const row of rows) {
    const drawn = folded.get(row.neighbour.id) ?? row.neighbour.id;
    const data = linkOf(row.link, row.family);
    const [source, target] = row.outgoing ? [FILE_ID, drawn] : [drawn, FILE_ID];
    const edge = `${row.family}:${source}->${target}`;
    const existing = edges.get(edge);
    if (existing) merge(existing.link, data);
    else edges.set(edge, { id: edge, source, target, link: { ...data, types: data.types.map((t) => ({ ...t })), examples: [...data.examples] } });

    // the inspector lists each neighbour once per family and direction,
    // whichever declarations in the file hold the link
    const key = `${row.family}:${row.outgoing ? "out" : "in"}:${row.neighbour.id}`;
    const via = names.get(row.link.child) ?? null;
    const known = merged.get(key);
    if (known) {
      merge(known.link, data);
      if (via && known.via !== via) known.via = known.via ? "several declarations" : via;
    } else {
      merged.set(key, {
        id: key,
        node: drawn,
        edge,
        direction: row.outgoing ? "outgoing" : "incoming",
        partner: {
          kind: row.neighbour.kind === "shared" ? "shared" : row.neighbour.kind,
          label: row.neighbour.label,
          detail: row.neighbour.detail,
          ...(row.neighbour.kind === "shared" ? { entityType: row.neighbour.target } : {}),
          ...(row.neighbour.kind === "file" ? { repository: repository.slug, path: row.neighbour.target } : {}),
          ...(row.neighbour.kind === "repository" ? { repository: row.neighbour.target, path: "" } : {}),
        },
        via,
        link: data,
      });
    }
  }
  const connections = [...merged.values()].sort(byStrength);
  const byNode = new Map<string, Connection[]>();
  for (const c of connections) push(byNode, c.node, c);

  return {
    anchor: { id: FILE_ID, path, name: basename(path), declarations: declarations.length },
    left,
    right,
    more,
    edges: [...edges.values()],
    connections,
    byNode,
    within,
    undrawn,
  };
}

/**
 * Where a level's view comes from when the reader climbs out of a child: the
 * node standing for that child here, so the way back lands on it.
 */
export function childOf(path: string, deeper: string): string | null {
  if (deeper === path) return null;
  if (path && !deeper.startsWith(`${path}/`)) return null;
  const rest = path ? deeper.slice(path.length + 1) : deeper;
  const head = rest.split("/", 1)[0]!;
  return path ? `${path}/${head}` : head;
}
