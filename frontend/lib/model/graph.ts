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
 * The view graph: what the canvas draws, before React Flow sees it. Pure, so the
 * rules that matter (kinds never summed, a link drawn between the right two
 * things, nothing overlapping) are tested without a browser.
 */

export type { LinkKind };

export interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface LinkData {
  kind: LinkKind;
  count: number;
  types: EdgeTypeCount[];
  /** Package names on a dependency link, when the overview names them. */
  packages: string[];
}

export interface ViewEdge {
  id: string;
  source: string;
  target: string;
  /** `tree` draws containment; a link draws a relationship of one kind. */
  role: "tree" | "link";
  link?: LinkData;
  /**
   * For links off the grid of an opened level: `detail` is one card's own link,
   * drawn when that card is pointed at or focused; `aggregate` is the level's
   * total per kind, drawn at rest. Absent for links drawn as they are.
   */
  scope?: "detail" | "aggregate";
}

// -- the overview: repositories, and the links between them ----------------------

export interface OverviewGraph {
  /** Repositories with at least one link, laid out by the layered engine. */
  linked: RepositoryModel[];
  /** Repositories with none, set in their own row: independent is a finding, not a gap. */
  independent: RepositoryModel[];
  edges: ViewEdge[];
}

export function buildOverview(
  repositories: readonly RepositoryModel[],
  links: readonly RepositoryLink[],
): OverviewGraph {
  const known = new Set(repositories.map((r) => r.slug));
  const edges: ViewEdge[] = [];
  const touched = new Set<string>();
  for (const link of links) {
    if (!known.has(link.source) || !known.has(link.target) || link.source === link.target) continue;
    // one edge per kind and direction: an HTTP call and a package dependency
    // between the same two repositories are two facts, drawn twice
    edges.push({
      id: `${link.kind}:${link.source}->${link.target}`,
      source: repositoryId(link.source),
      target: repositoryId(link.target),
      role: "link",
      link: {
        kind: link.kind,
        count: link.count,
        types: [...(link.types ?? [])],
        packages: (link.packages ?? []).map((p) => p.name),
      },
    });
    touched.add(link.source);
    touched.add(link.target);
  }
  return {
    linked: repositories.filter((r) => touched.has(r.slug)),
    independent: repositories.filter((r) => !touched.has(r.slug)),
    edges,
  };
}

export function repositoryId(slug: string): string {
  return `repo:${slug}`;
}

// -- a repository opened at a path ------------------------------------------------

export const PREVIEW_ROWS = 5;
export const FILE_ROWS = 8;

export interface ChainStep {
  id: string;
  /** "" for the repository itself. */
  path: string;
  name: string;
}

export interface DirectoryCard {
  id: string;
  path: string;
  name: string;
  files: number;
  /** The directory's first entries, once its preview has loaded. */
  preview: ChildEntry[] | null;
  previewTotal: number;
}

export interface FilesCard {
  id: string;
  /** Files (or, inside a file, declarations) at this level. */
  entries: ChildEntry[];
  total: number;
  label: string;
}

export type SharedKind = string;

export interface SharedNode {
  id: string;
  entityType: SharedKind;
  label: string;
  count: number;
}

export interface PeerNode {
  id: string;
  repository: string;
  path: string | null;
}

/** Somewhere else in this repository that entries here link to. */
export interface AwayNode {
  id: string;
  path: string;
  /** The level to open to see it: the directory that holds it. */
  opens: string;
}

export interface OpenedGraph {
  chain: ChainStep[];
  directories: DirectoryCard[];
  files: FilesCard | null;
  shared: SharedNode[];
  peers: PeerNode[];
  away: AwayNode[];
  edges: ViewEdge[];
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

export const FILES_ID = "files";

/** Directories first, hidden ones (`.github`) after the rest, each group by name. */
function byName(a: ChildEntry, b: ChildEntry): number {
  const hidden = Number(a.name.startsWith(".")) - Number(b.name.startsWith("."));
  return hidden !== 0 ? hidden : a.name.localeCompare(b.name);
}

export function buildOpened(input: {
  repository: RepositoryModel;
  path: string;
  children: TopologyChildrenResponse;
  previews: ReadonlyMap<string, TopologyChildrenResponse>;
}): OpenedGraph {
  const { repository, path, children, previews } = input;
  const segments = path ? path.split("/") : [];
  const chain: ChainStep[] = [
    { id: "chain:", path: "", name: repository.name },
    ...segments.map((name, i) => {
      const at = segments.slice(0, i + 1).join("/");
      return { id: `chain:${at}`, path: at, name };
    }),
  ];

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
      };
    });
  const leaves = entries.filter((e) => e.kind !== "directory");
  const inFile = children.target === "file";
  const files: FilesCard | null =
    leaves.length === 0
      ? null
      : {
          id: FILES_ID,
          entries: leaves.slice(0, FILE_ROWS),
          total: inFile ? (children.entries_total ?? leaves.length) : leaves.length,
          label: inFile ? "Declarations" : "Files",
        };

  // where each child is drawn: its own card, or the files card
  const cardOf = new Map<string, string>();
  for (const d of directories) cardOf.set(d.path, d.id);
  if (files) for (const leaf of leaves) cardOf.set(leaf.path, FILES_ID);
  /** The card holding a path: the entry itself, or the directory it is inside. */
  const cardFor = (at: string): string | undefined => {
    for (let p = at; p; p = p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "") {
      const id = cardOf.get(p);
      if (id !== undefined) return id;
    }
    return undefined;
  };

  const level = chain[chain.length - 1]!.id;
  const shared = new Map<string, SharedNode>();
  const peers = new Map<string, PeerNode>();
  const away = new Map<string, AwayNode>();
  const edges = new Map<string, ViewEdge>();
  let undrawn = 0;

  const add = (id: string, source: string, target: string, link: ChildLink, scope?: ViewEdge["scope"]) => {
    const existing = edges.get(id);
    if (existing?.link) {
      existing.link.count += link.count;
      existing.link.types.push(...(link.types ?? []));
      return;
    }
    edges.set(id, {
      id,
      source,
      target,
      role: "link",
      link: { kind: link.kind, count: link.count, types: [...(link.types ?? [])], packages: [] },
      ...(scope ? { scope } : {}),
    });
  };

  for (const link of children.links ?? []) {
    const near = cardFor(link.child);
    if (near === undefined) {
      undrawn += 1;
      continue;
    }
    const peer = link.peer;
    let far: string;
    if (peer.kind === "shared") {
      const entityType = peer.entity_type ?? "shared";
      far = `shared:${entityType}`;
      const node = shared.get(far) ?? { id: far, entityType, label: sharedLabel(entityType), count: 0 };
      node.count += link.count;
      shared.set(far, node);
    } else if (peer.kind === "repository" || (peer.repository && peer.repository !== repository.slug)) {
      if (!peer.repository) {
        undrawn += 1;
        continue;
      }
      far = `peer:${peer.repository}`;
      if (!peers.has(far)) peers.set(far, { id: far, repository: peer.repository, path: peer.path ?? null });
    } else {
      const card = peer.path ? cardFor(peer.path) : undefined;
      if (card !== undefined) {
        far = card;
      } else if (peer.path) {
        far = `away:${peer.path}`;
        const cut = peer.path.lastIndexOf("/");
        if (!away.has(far)) away.set(far, { id: far, path: peer.path, opens: cut > 0 ? peer.path.slice(0, cut) : "" });
      } else {
        undrawn += 1;
        continue;
      }
    }
    if (far === near) continue; // within one card: the files card, or inside a directory
    const [source, target] = link.direction === "incoming" ? [far, near] : [near, far];
    const offGrid = !far.startsWith("dir:") && far !== FILES_ID;
    if (!offGrid) {
      add(`${link.kind}:${source}->${target}`, source, target, link);
      continue;
    }
    // off the grid: the card's own line on demand, the level's total at rest
    add(`${link.kind}:${source}->${target}`, source, target, link, "detail");
    const [from, to] = link.direction === "incoming" ? [far, level] : [level, far];
    add(`${link.kind}:${from}=>${to}`, from, to, link, "aggregate");
  }

  const tree: ViewEdge[] = [];
  for (let i = 1; i < chain.length; i++) {
    tree.push({ id: `tree:${chain[i - 1]!.id}->${chain[i]!.id}`, source: chain[i - 1]!.id, target: chain[i]!.id, role: "tree" });
  }
  for (const card of [...directories.map((d) => d.id), ...(files ? [FILES_ID] : [])]) {
    tree.push({ id: `tree:${level}->${card}`, source: level, target: card, role: "tree" });
  }

  return {
    chain,
    directories,
    files,
    shared: [...shared.values()].sort((a, b) => b.count - a.count),
    peers: [...peers.values()].sort((a, b) => a.repository.localeCompare(b.repository)),
    away: [...away.values()].sort((a, b) => a.path.localeCompare(b.path)),
    edges: [...tree, ...edges.values()],
    undrawn,
  };
}
