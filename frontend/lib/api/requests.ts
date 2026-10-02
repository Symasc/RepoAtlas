"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import {
  crossingNamed,
  decisionRequest,
  readRows,
  requestsRequest,
  type RequestsScope,
} from "@/lib/model/decisions";

import { call } from "./client";
import { within } from "./topology";

/**
 * A Requests view's rows, page by page, as the API pages them: never a scan for
 * one row. `hidden` reads the join-only rows, which are fetched only when asked
 * for.
 */
export function useRequests(scope: RequestsScope, hidden = false, enabled = true) {
  return useInfiniteQuery({
    queryKey: ["http_operations", "requests", scope.repository, scope.path, hidden],
    queryFn: async ({ pageParam, signal }) => {
      const response = await call("http_operations", requestsRequest(scope, { hidden, cursor: pageParam }), signal);
      return { response, rows: readRows(response) };
    },
    initialPageParam: null as string | null,
    getNextPageParam: (last) => (last.response.has_more ? (last.response.next_cursor ?? null) : null),
    enabled,
  });
}

/** How many join-only requests are in scope, and their decisions, without listing them. */
export function useHiddenSummary(scope: RequestsScope) {
  return useQuery({
    queryKey: ["http_operations", "hidden", scope.repository, scope.path],
    queryFn: ({ signal }) => call("http_operations", requestsRequest(scope, { hidden: true, limit: 1 }), signal),
    select: (response) => response.summary,
  });
}

/**
 * One stored decision by its exact ref, with every repository's part of its chain
 * and the lines of its proven hops: read only when its Evidence is opened, and
 * recorded in the inspected thing's investigation.
 */
export function useDecision(ref: string | null, subject: string) {
  return useQuery({
    queryKey: ["http_operations", "decision", ref],
    queryFn: async ({ signal }) => {
      const response = await within(subject, (investigation) =>
        call("http_operations", decisionRequest(ref!, investigation), signal),
      );
      return {
        found: crossingNamed(readRows(response), ref!),
        evidence: new Map((response.evidence ?? []).map((item) => [item.id, item])),
      };
    },
    enabled: ref !== null,
    staleTime: 5 * 60_000,
  });
}
