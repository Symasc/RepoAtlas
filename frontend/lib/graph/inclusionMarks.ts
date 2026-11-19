import { repositoryId, type ViewEdge } from "@/lib/model/graph";
import type { RepositoryModel } from "@/lib/model/repositories";

import { inclusionParents } from "./districts";

/**
 * How a repository belongs to its inclusion tree, for the card and the frame to
 * say in words: who includes it, how deep, and by what kind of declaration. Pure,
 * and independent of the order repositories and edges arrive in.
 */

export type InclusionKind = "submodule" | "subrepo" | "manifest" | "other";
/** The kinds in the order they are listed. */
export const INCLUSION_KINDS: readonly InclusionKind[] = ["submodule", "subrepo", "manifest", "other"];

/** Code-unit order: the same on every machine, which a locale-aware comparison is not. */
const cmp = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

export function inclusionKind(format: string): InclusionKind {
  if (format === "gitmodules") return "submodule";
  if (format === "git-subrepo") return "subrepo";
  if (format === "west" || format === "repo-manifest" || format === "vcstool") return "manifest";
  return "other";
}

/** The words for a declaration format; a format nobody named reads as written. */
const FORMAT_LABEL: ReadonlyMap<string, string> = new Map([
  ["gitmodules", "Git submodule"],
  ["git-subrepo", "git-subrepo"],
  ["west", "west manifest"],
  ["repo-manifest", "repo manifest"],
  ["vcstool", "vcstool .repos"],
]);

export function formatLabel(format: string): string {
  return FORMAT_LABEL.get(format) ?? format;
}

export interface InclusionMark {
  /** The including repository's node id; null for a family's root. */
  parent: string | null;
  depth: number;
  /** The family's root node id. */
  root: string;
  /** The declaration format of the inclusion that makes it a child, as the parent states it. */
  format: string | null;
  kind: InclusionKind | null;
  label: string | null;
  /** Where the parent states the inclusion: the smallest listed path. */
  path: string | null;
}

/**
 * By repository node id, the mark of every repository in an inclusion family. A
 * root has no format, kind, label or path; any other member takes them from the
 * inclusion its parent states with the smallest path.
 */
export function inclusionMarks(repositories: readonly RepositoryModel[], edges: readonly ViewEdge[]): Map<string, InclusionMark> {
  const stated = new Map<string, { format: string; path: string }[]>();
  for (const edge of edges) {
    if (edge.link.kind !== "submodule") continue;
    const key = `${edge.source}\u0000${edge.target}`;
    const list = stated.get(key) ?? [];
    stated.set(key, list);
    for (const inclusion of edge.link.inclusions ?? []) list.push({ format: inclusion.format, path: inclusion.path });
  }

  const marks = new Map<string, InclusionMark>();
  for (const [slug, place] of inclusionParents(repositories, edges)) {
    const id = repositoryId(slug);
    const root = repositoryId(place.root);
    if (place.parent === null) {
      marks.set(id, { parent: null, depth: place.depth, root, format: null, kind: null, label: null, path: null });
      continue;
    }
    const parent = repositoryId(place.parent);
    const smallest = [...(stated.get(`${parent}\u0000${id}`) ?? [])].sort((a, b) => cmp(a.path, b.path) || cmp(a.format, b.format))[0];
    marks.set(
      id,
      smallest === undefined
        ? { parent, depth: place.depth, root, format: null, kind: null, label: "Inclusion", path: null }
        : {
            parent,
            depth: place.depth,
            root,
            format: smallest.format,
            kind: inclusionKind(smallest.format),
            label: formatLabel(smallest.format),
            path: smallest.path,
          },
    );
  }
  return marks;
}
