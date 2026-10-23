"use client";

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  filterFields,
  type ConnectionFilters,
} from "@/lib/model/connections";

import { call } from "./client";
import type {
  DeploymentLinkCreateRequest,
  HttpConnectionPreviewRequest,
} from "./generated";

const GLOBAL = { scope: "global" } as const;

/** The workspace's unresolved requests, grouped by origin, and the operator's declarations. */
export function useConnections(filters: ConnectionFilters, enabled: boolean) {
  return useQuery({
    queryKey: ["http_connections", "list", filters],
    queryFn: ({ signal }) => call("http_connections", { context: GLOBAL, ...filterFields(filters) }, signal),
    enabled,
  });
}

/** One group's requests, page by page, and its candidate services (the first page carries them). */
export function useGroup(group: string | null, filters: ConnectionFilters) {
  return useInfiniteQuery({
    queryKey: ["http_connections", "group", group, filters.includeResolved],
    queryFn: ({ pageParam, signal }) =>
      call(
        "http_connections",
        {
          context: GLOBAL,
          group: group!,
          include_resolved: filters.includeResolved,
          limit: 25,
          ...(pageParam ? { cursor: pageParam } : {}),
        },
        signal,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => (last.has_more ? (last.next_cursor ?? null) : null),
    enabled: group !== null,
  });
}

/** What a mapping would do, request by request: read-only, asked again whenever the form changes. */
export function useMapPreview(request: HttpConnectionPreviewRequest | null) {
  return useQuery({
    queryKey: ["http_connections", "preview", request],
    queryFn: ({ signal }) => call("http_connections/preview", request!, signal),
    enabled: request !== null,
    staleTime: 0,
    retry: false,
  });
}

export function useHistory(enabled: boolean) {
  return useQuery({
    queryKey: ["http_connections", "history"],
    queryFn: ({ signal }) => call("http_connections/history", { context: GLOBAL, limit: 100 }, signal),
    enabled,
  });
}

/** Declare and revoke: the existing deployment-link writes, with where the action came from. */
export function useDeclarations() {
  const client = useQueryClient();
  const settled = () => {
    void client.invalidateQueries({ queryKey: ["http_connections"] });
    void client.invalidateQueries({ queryKey: ["deployment_targets"] });
  };
  return {
    declare: useMutation({
      mutationFn: (request: DeploymentLinkCreateRequest) => call("deployment_links/create", request),
      onSuccess: settled,
    }),
    revoke: useMutation({
      mutationFn: ({ repository, id, note }: { repository: string; id: number; note: string | null }) =>
        call("deployment_links/delete", {
          context: { scope: "repository", repository },
          id,
          provenance: { via: "ui", note },
        }),
      onSuccess: settled,
    }),
  };
}
