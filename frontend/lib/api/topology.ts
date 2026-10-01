"use client";

import { keepPreviousData, useQueries, useQuery } from "@tanstack/react-query";


import { ApiError, call } from "./client";
import type { CapabilityContext, ImpactResponse, InspectResponse, TopologyChildrenResponse, TopologyEdgeResponse } from "./generated";
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

export function childrenQuery(slug: string, path: string) {
  return {
    queryKey: ["topology/children", slug, path] as const,
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      call("topology/children", { context: inRepository(slug), path, cross_repository: true }, signal),
  };
}

/** One level of a repository, with its links, other repositories included. */
export function useChildren(slug: string | null, path: string, enabled = true) {
  return useQuery({
    ...childrenQuery(slug ?? "", path),
    enabled: slug !== null && enabled,
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

// -- evidence: one investigation per thing being inspected -----------------------------

/**
 * The investigation each inspected thing records its evidence in, so the edge
 * evidence and source read for one file sit in one ledger, and unrelated things
 * never share one. The server keeps a bounded number and expires idle ones: a
 * refusal of category `session` starts a fresh one.
 */
const investigations = new Map<string, string>();

async function within<T extends { investigation_id: string }>(
  subject: string,
  run: (investigation: string | null) => Promise<T>,
): Promise<T> {
  const known = investigations.get(subject) ?? null;
  try {
    const result = await run(known);
    investigations.set(subject, result.investigation_id);
    return result;
  } catch (error) {
    if (known === null || !(error instanceof ApiError) || error.error.category !== "session") throw error;
    investigations.delete(subject);
    const result = await run(null);
    investigations.set(subject, result.investigation_id);
    return result;
  }
}

/**
 * One exact stored relationship and the lines that assert it, per example
 * reference. Asked across the workspace: a link's other end may be in another
 * repository, and a reference outside the asked scope is refused as unknown.
 */
export function useEdges(refs: readonly string[], subject: string) {
  return useQueries({
    queries: refs.map((ref) => ({
      queryKey: ["topology/edge", ref],
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        within<TopologyEdgeResponse>(subject, (investigation) =>
          call(
            "topology/edge",
            { context: WORKSPACE, edge_ref: ref, investigation_id: investigation },
            signal,
          ),
        ),
      staleTime: 5 * 60_000,
    })),
  });
}

/** The budget an inspection may use: the server's own ceilings. */
const SOURCE_BUDGET = { max_items: 18, max_total_chars: 32_000, max_item_chars: 8_000 } as const;

/** A file's indexed source, as much as one inspection may return. */
export function useSource(slug: string, path: string, enabled: boolean) {
  return useQuery({
    queryKey: ["inspect", slug, path],
    queryFn: ({ signal }) =>
      within(`file:${slug}:${path}`, (investigation) =>
        call(
          "inspect",
          {
            context: inRepository(slug),
            target: { path },
            presentation: "full",
            context_lines: 0,
            budget: SOURCE_BUDGET,
            investigation_id: investigation,
          },
          signal,
        ),
      ),
    enabled,
    staleTime: 5 * 60_000,
  });
}

/** Shorter than this, a file search would match nearly everything. */
export const FILE_QUERY_MIN = 2;

/** Files across the workspace whose paths hold every term typed. Records nothing. */
export function useFileSearch(query: string, limit = 12) {
  const q = query.trim();
  return useQuery({
    queryKey: ["topology/files", q, limit],
    queryFn: ({ signal }) => call("topology/files", { context: WORKSPACE, query: q, limit }, signal),
    enabled: q.length >= FILE_QUERY_MIN,
    placeholderData: keepPreviousData,
    staleTime: 60_000,
  });
}

/**
 * Names declared across the workspace (variables, functions, classes, methods,
 * environment variables) that hold every term typed. Records nothing.
 */
export function useNameSearch(query: string, limit = 10) {
  const q = query.trim();
  return useQuery({
    queryKey: ["topology/names", q, limit],
    queryFn: ({ signal }) => call("topology/names", { context: WORKSPACE, query: q, limit }, signal),
    enabled: q.length >= FILE_QUERY_MIN,
    placeholderData: keepPreviousData,
    staleTime: 60_000,
  });
}

/** What each line of a file declares and refers to, other repositories included. */
export function useReferences(slug: string, path: string) {
  return useQuery({
    queryKey: ["topology/references", slug, path],
    queryFn: ({ signal }) =>
      call("topology/references", { context: inRepository(slug), path, cross_repository: true }, signal),
    staleTime: 5 * 60_000,
  });
}

/** How many things one trace lists at most: impact's own ceiling. */
export const TRACE_CANDIDATES = 100;

/**
 * What a change to one thing reaches, and by which indexed steps. One
 * investigation per trace: the lines read for its steps are recorded with it.
 */
export function useImpact(seed: string | null) {
  return useQuery({
    queryKey: ["impact", seed ?? ""],
    queryFn: ({ signal }) =>
      within<ImpactResponse>(`trace:${seed!}`, (investigation) =>
        call(
          "impact",
          { context: WORKSPACE, seed: seed!, presentation: "full", max_candidates: TRACE_CANDIDATES, investigation_id: investigation },
          signal,
        ),
      ),
    enabled: seed !== null,
    staleTime: 5 * 60_000,
  });
}

/** A few lines of one indexed file, recorded in the investigation of `subject`. */
export function useLines(
  slug: string | null,
  path: string | null,
  lines: { start: number; end: number } | null,
  subject: string,
  around = 3,
) {
  return useQuery({
    queryKey: ["inspect", slug, path, lines?.start ?? 0, lines?.end ?? 0, around],
    queryFn: ({ signal }) =>
      within<InspectResponse>(subject, (investigation) =>
        call(
          "inspect",
          {
            context: inRepository(slug!),
            target: { path: path! },
            line_start: lines!.start,
            line_end: lines!.end,
            context_lines: around,
            presentation: "full",
            investigation_id: investigation,
          },
          signal,
        ),
      ),
    enabled: slug !== null && path !== null && lines !== null,
    staleTime: 5 * 60_000,
  });
}
