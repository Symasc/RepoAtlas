import type { RepositoriesResponse, RepositoryView } from "@/lib/api/generated";

import { toFreshness, type FreshnessModel } from "./freshness";

/** Repository identity hues: eight, then they repeat (monograms take over in P2). */
export const HUE_COUNT = 8;

export interface RepositoryModel {
  slug: string;
  name: string;
  branch: string | null;
  headSha: string | null;
  files: number;
  symbols: number;
  relationships: number;
  languages: string[];
  manifests: string[];
  lockfiles: string[];
  lastIndexedAt: string | null;
  /** 1–8: which `--ra-repo-N` colour identifies this repository. */
  hue: number;
  freshness: FreshnessModel;
}

export interface WorkspaceTotals {
  repositories: number;
  files: number;
  symbols: number;
  relationships: number;
}

export function toRepositories(response: RepositoriesResponse): RepositoryModel[] {
  const views = [...(response.repositories ?? [])].sort((a, b) => a.slug.localeCompare(b.slug));
  const hues = assignHues(views.map((v) => v.slug));
  return views.map((view) => toRepository(view, hues.get(view.slug) ?? 1));
}

export function totals(repositories: readonly RepositoryModel[]): WorkspaceTotals {
  return repositories.reduce<WorkspaceTotals>(
    (sum, r) => ({
      repositories: sum.repositories + 1,
      files: sum.files + r.files,
      symbols: sum.symbols + r.symbols,
      relationships: sum.relationships + r.relationships,
    }),
    { repositories: 0, files: 0, symbols: 0, relationships: 0 },
  );
}

function toRepository(view: RepositoryView, hue: number): RepositoryModel {
  return {
    slug: view.slug,
    name: view.name,
    branch: view.branch ?? null,
    headSha: view.head_sha ?? null,
    files: view.indexed_files ?? 0,
    symbols: view.symbols ?? 0,
    relationships: view.relationships ?? 0,
    languages: view.languages ?? [],
    manifests: view.manifests ?? [],
    lockfiles: view.lockfiles ?? [],
    lastIndexedAt: view.last_indexed_at ?? null,
    hue,
    freshness: toFreshness(view.freshness),
  };
}

/**
 * A stable hue per slug: its hash picks a hue, and a taken hue moves it to the
 * next free one. The same set of repositories always gets the same colours, and
 * adding one repository recolours at most the ones that collide with it.
 */
export function assignHues(slugs: readonly string[]): Map<string, number> {
  const taken = new Set<number>();
  const hues = new Map<string, number>();
  for (const slug of [...slugs].sort()) {
    let hue = hash(slug) % HUE_COUNT;
    if (taken.size < HUE_COUNT) {
      while (taken.has(hue)) hue = (hue + 1) % HUE_COUNT;
    }
    taken.add(hue);
    hues.set(slug, hue + 1);
  }
  return hues;
}

/** FNV-1a: small, stable across runs and platforms. */
function hash(text: string): number {
  let value = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    value ^= text.charCodeAt(i);
    value = Math.imul(value, 0x01000193);
  }
  return value >>> 0;
}
