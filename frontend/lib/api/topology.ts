"use client";

import { useQueries, useQuery } from "@tanstack/react-query";

import { call } from "./client";
import type { CapabilityContext, TopologyChildrenResponse } from "./generated";
import { WORKSPACE } from "./queries";

export function inRepository(slug: string): CapabilityContext {
  return { scope: "repository", repository: slug };
}

/** Which repositories link to which, by kind. */
export function useTopologyOverview() {
  return useQuery({
    queryKey: ["topology/overview", WORKSPACE],
    queryFn: ({ signal }) => call("topology/overview", { context: WORKSPACE }, signal),
  });
}

/** One level of a repository, with its links, other repositories included. */
export function useChildren(slug: string | null, path: string) {
  return useQuery({
    queryKey: ["topology/children", slug, path],
    queryFn: ({ signal }) =>
      call(
        "topology/children",
        { context: inRepository(slug ?? ""), path, cross_repository: true },
        signal,
      ),
    enabled: slug !== null,
  });
}

/**
 * The first entries of each directory on screen, so a card can list what is in
 * it. One bounded read per visible card, fetched after the level itself.
 */
export function usePreviews(
  slug: string | null,
  paths: readonly string[],
  rows: number,
): (TopologyChildrenResponse | undefined)[] {
  return useQueries({
    queries: paths.map((path) => ({
      queryKey: ["topology/children", slug, path, "preview", rows],
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        call("topology/children", { context: inRepository(slug ?? ""), path, limit: rows }, signal),
      enabled: slug !== null,
      staleTime: 60_000,
    })),
    // structurally shared: the same array until some preview actually changes
    combine: (results) => results.map((result) => result.data),
  });
}
