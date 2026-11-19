import type { InclusionLink, RepositoryLink } from "@/lib/api/generated";
import { toFreshness } from "@/lib/model/freshness";
import type { RepositoryModel } from "@/lib/model/repositories";

/**
 * A small world of fourteen repositories for what the ecosystem fixture lacks:
 * nested-repository links (a monorepo holding two packages that talk to each
 * other, an inclusion tree whose root also nests a repository) and one inclusion
 * of each kind of declaration, a format nobody named among them. Hand-built and
 * deterministic: the same call always returns the same repositories and links.
 */

const SLUGS = [
  "mono",
  "mono-web",
  "mono-api",
  "app",
  "lib",
  "fw",
  "fw-hal",
  "fw-docs",
  "sub-root",
  "sub-child",
  "sr-root",
  "sr-child",
  "odd-root",
  "odd-child",
] as const;

function repository(slug: string, index: number): RepositoryModel {
  return {
    slug,
    name: slug,
    branch: "main",
    headSha: String(index + 1).padStart(2, "0").repeat(20),
    files: 10 + index,
    symbols: 100 + index,
    relationships: 500 + index,
    languages: ["TypeScript"],
    manifests: ["package.json"],
    lockfiles: [],
    lastIndexedAt: null,
    hue: (index % 8) + 1,
    freshness: toFreshness({ state: "current_clean", depth: "live", repository_slug: slug }),
  };
}

/** `parent` includes `child`, declared in `format`: one inclusion, which is the whole link. */
function inclusion(parent: string, child: string, format: string): RepositoryLink {
  const stated: InclusionLink = { declared_at: `${parent}/manifest`, format, method: "path", path: `lib/${child}` };
  return {
    kind: "submodule",
    source: parent,
    target: child,
    count: 1,
    types: [{ type: "INCLUDES_REPOSITORY", count: 1 }],
    examples: [],
    inclusions: [stated],
    inclusions_total: 1,
  };
}

/** `parent` holds `child` as a nested repository, at `path` inside it. */
function nested(parent: string, child: string, path: string): RepositoryLink {
  return { kind: "nested", source: parent, target: child, count: 1, types: [], examples: [], nested_path: path };
}

export function nestedFixture(): { repositories: RepositoryModel[]; links: RepositoryLink[] } {
  const repositories = SLUGS.map(repository);
  const links: RepositoryLink[] = [
    nested("mono", "mono-web", "packages/web"),
    nested("mono", "mono-api", "packages/api"),
    { kind: "observed", source: "app", target: "lib", count: 3, types: [{ type: "IMPORTS", count: 3 }], examples: [] },
    { kind: "http", source: "app", target: "mono-web", count: 2, types: [{ type: "CALLS_HTTP_ROUTE", count: 2 }], examples: [] },
    { kind: "observed", source: "mono-web", target: "mono-api", count: 1, types: [{ type: "CALLS", count: 1 }], examples: [] },
    nested("fw", "fw-docs", "docs"),
    inclusion("fw", "fw-hal", "west"),
    inclusion("sub-root", "sub-child", "gitmodules"),
    inclusion("sr-root", "sr-child", "git-subrepo"),
    inclusion("odd-root", "odd-child", "unknown-tool"),
  ];
  return { repositories, links };
}
