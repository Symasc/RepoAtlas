"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { call } from "./client";
import type { CapabilityContext, DeploymentLinkFields } from "./generated";

function repositoryContext(repository: string): CapabilityContext {
  return { scope: "repository", repository };
}

/** A repository's deployment targets and links, for the branch asked about (the checkout's when null). */
export function useDeploymentTargets(repository: string, branch: string | null) {
  return useQuery({
    queryKey: ["deployment_targets", repository, branch],
    queryFn: ({ signal }) =>
      call("deployment_targets", { context: repositoryContext(repository), branch }, signal),
  });
}

/** Create, update and remove the user's links; each refreshes the targets they resolve. */
export function useDeploymentLinks(repository: string) {
  const client = useQueryClient();
  const context = repositoryContext(repository);
  const onSuccess = () => client.invalidateQueries({ queryKey: ["deployment_targets", repository] });
  return {
    create: useMutation({
      mutationFn: (link: DeploymentLinkFields) => call("deployment_links/create", { context, link }),
      onSuccess,
    }),
    update: useMutation({
      mutationFn: ({ id, link }: { id: number; link: DeploymentLinkFields }) =>
        call("deployment_links/update", { context, id, link }),
      onSuccess,
    }),
    remove: useMutation({
      mutationFn: (id: number) => call("deployment_links/delete", { context, id }),
      onSuccess,
    }),
  };
}
