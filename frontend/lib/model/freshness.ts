import type { FreshnessDepth, FreshnessState, RepositoryFreshness } from "@/lib/api/generated";

/**
 * How a freshness state should read. The backend's state names the strongest
 * thing it checked; the tone only groups states by what a person should do.
 */
export type FreshnessTone = "current" | "unverified" | "attention" | "problem" | "unknown";

interface StateCopy {
  label: string;
  tone: FreshnessTone;
  explanation: string;
}

const SYNC = "Run `repoatlas sync` to catch up.";

export const FRESHNESS: Record<FreshnessState, StateCopy> = {
  current_clean: {
    label: "Current",
    tone: "current",
    explanation: "The index matches HEAD and the files on disk.",
  },
  indexed: {
    label: "Indexed",
    tone: "unverified",
    explanation: "Indexed facts are present. HEAD and the working tree were not checked.",
  },
  dirty: {
    label: "Uncommitted changes",
    tone: "attention",
    explanation: "The index matches HEAD, but uncommitted edits in the working tree may not be reflected.",
  },
  stale_head: {
    label: "HEAD moved",
    tone: "attention",
    explanation: `HEAD has changed since the last sync. ${SYNC}`,
  },
  stale_worktree: {
    label: "Files changed",
    tone: "attention",
    explanation: `Files have changed since the last sync. ${SYNC}`,
  },
  stale_build: {
    label: "Older index format",
    tone: "attention",
    explanation: `An earlier RepoAtlas build wrote this index. ${SYNC}`,
  },
  interrupted: {
    label: "Sync interrupted",
    tone: "problem",
    explanation: "The last sync stopped partway. Run `repoatlas sync` again.",
  },
  not_indexed: {
    label: "Not indexed",
    tone: "problem",
    explanation: "This repository has not been indexed yet. Run `repoatlas sync`.",
  },
  unavailable: {
    label: "Unavailable",
    tone: "problem",
    explanation: "The repository can't be read where it was indexed.",
  },
  unknown: {
    label: "Unknown",
    tone: "unknown",
    explanation: "RepoAtlas couldn't determine whether this index is current.",
  },
};

export interface FreshnessModel extends StateCopy {
  state: FreshnessState;
  depth: FreshnessDepth;
  /** The backend's own words, e.g. "stale (HEAD changed)". */
  detail: string;
  /** When this repository was last indexed, as the check that read it says. */
  indexedAt: string | null;
  /** Files an earlier RepoAtlas build indexed, which the next sync rewrites. */
  outdatedFiles: number;
}

export function toFreshness(freshness: RepositoryFreshness): FreshnessModel {
  return {
    ...FRESHNESS[freshness.state],
    state: freshness.state,
    depth: freshness.depth,
    detail: freshness.detail ?? "",
    indexedAt: freshness.last_indexed_at ?? null,
    outdatedFiles: freshness.outdated_files ?? 0,
  };
}

export interface FreshnessSummary {
  total: number;
  current: number;
  /** The worst tone present: one stale repository is not averaged away. */
  tone: FreshnessTone;
  headline: string;
}

const SEVERITY: Record<FreshnessTone, number> = {
  current: 0,
  unverified: 1,
  unknown: 2,
  attention: 3,
  problem: 4,
};

export function summarize(models: readonly FreshnessModel[]): FreshnessSummary {
  const total = models.length;
  const current = models.filter((m) => m.tone === "current").length;
  const tone = models.reduce<FreshnessTone>(
    (worst, m) => (SEVERITY[m.tone] > SEVERITY[worst] ? m.tone : worst),
    "current",
  );
  let headline: string;
  if (total === 0) headline = "No repositories";
  else if (current === total) headline = total === 1 ? "Current" : `All ${total} current`;
  else headline = `${current} of ${total} current`;
  return { total, current, tone, headline };
}
