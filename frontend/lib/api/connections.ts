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
  ServiceDeclarationCreateRequest,
  ServiceDeclarationPreviewRequest,
  ServiceDeclarationUpdateRequest,
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

/**
 * What a scoped declaration would do: the same read-only preview, asked with the caller's file
 * (and symbol) and the base's hash. The answer carries no origin or URL, and the base shown masked.
 */
export function useScopedPreview(request: HttpConnectionPreviewRequest | null) {
  return useQuery({
    queryKey: ["http_connections", "preview", "scoped", request],
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

/**
 * Declare a scoped base: the deployment-link create, sent with `link.scope` and the worklist group
 * as `provenance.request_group`. The server re-runs the scoped preview and refuses what it refuses.
 */
export function useScopedDeclaration() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (request: DeploymentLinkCreateRequest) => call("deployment_links/create", request),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["http_connections"] });
      void client.invalidateQueries({ queryKey: ["deployment_targets"] });
    },
  });
}

/**
 * What classifying an origin as an external service would do, request by request: read-only, asked
 * again whenever the form changes. Nothing is stored by asking.
 */
export function useServicePreview(request: ServiceDeclarationPreviewRequest | null) {
  return useQuery({
    queryKey: ["service_declarations", "preview", request],
    queryFn: ({ signal }) => call("service_declarations/preview", request!, signal),
    enabled: request !== null,
    staleTime: 0,
    retry: false,
  });
}

/** What operators did to external services, newest first. */
export function useServiceHistory(enabled: boolean) {
  return useQuery({
    queryKey: ["service_declarations", "history"],
    queryFn: ({ signal }) =>
      call("service_declarations/history", { context: GLOBAL, kind: "external_service", limit: 100 }, signal),
    enabled,
  });
}

/**
 * Classify, edit and revoke an external service. A write takes effect when stored: there is no sync
 * to wait for, so the worklist and the history are read again.
 */
export function useServiceDeclarations() {
  const client = useQueryClient();
  const settled = () => {
    void client.invalidateQueries({ queryKey: ["http_connections"] });
    void client.invalidateQueries({ queryKey: ["service_declarations"] });
  };
  return {
    create: useMutation({
      mutationFn: (request: ServiceDeclarationCreateRequest) => call("service_declarations/create", request),
      onSuccess: settled,
    }),
    update: useMutation({
      mutationFn: (request: ServiceDeclarationUpdateRequest) => call("service_declarations/update", request),
      onSuccess: settled,
    }),
    revoke: useMutation({
      mutationFn: ({ id, note }: { id: number; note: string | null }) =>
        call("service_declarations/delete", { context: GLOBAL, id, provenance: { via: "ui", note } }),
      onSuccess: settled,
    }),
  };
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
