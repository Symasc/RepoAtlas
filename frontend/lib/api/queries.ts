"use client";

import { QueryClient, useQuery } from "@tanstack/react-query";

import { toFreshness, type FreshnessModel } from "@/lib/model/freshness";
import { toRepositories } from "@/lib/model/repositories";

import { ApiError, call } from "./client";
import type { CapabilityContext } from "./generated";

export const WORKSPACE: CapabilityContext = { scope: "global" };

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        // a refusal will not change on retry; a server that is starting might
        retry: (failures, error) => error instanceof ApiError && error.retryable && failures < 2,
      },
    },
  });
}

/** Every indexed repository, with freshness as stored: one cheap read. */
export function useRepositories() {
  return useQuery({
    queryKey: ["repositories", WORKSPACE],
    queryFn: ({ signal }) => call("repositories", { context: WORKSPACE }, signal),
    select: toRepositories,
  });
}

/**
 * Freshness checked against git and the files on disk: seconds on a large
 * workspace, so it is fetched after the shell renders and refreshed when the
 * window regains focus rather than on a timer. Drift (repositories added to or
 * removed from the root) is not checked here.
 */
export function useLiveFreshness() {
  return useQuery({
    queryKey: ["status", WORKSPACE, "live"],
    queryFn: ({ signal }) =>
      call("status", { context: WORKSPACE, depth: "live", include_drift: false }, signal),
    select: (status): Map<string, FreshnessModel> =>
      new Map(
        (status.freshness.repositories ?? []).map((r) => [r.repository_slug, toFreshness(r)]),
      ),
    staleTime: 60_000,
    refetchOnWindowFocus: true,
  });
}
