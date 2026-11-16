"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { call } from "./client";
import type { InclusionDeclarationFields } from "./generated";
import { WORKSPACE } from "./queries";

/** The user's inclusion declarations, each with what the last sync made of it. */
export function useInclusionDeclarations(enabled: boolean) {
  return useQuery({
    queryKey: ["inclusion_declarations"],
    queryFn: ({ signal }) => call("inclusion_declarations", { context: WORKSPACE }, signal),
    enabled,
  });
}

/** Declare which repository an unsettled inclusion is, or revoke a declaration. */
export function useInclusionDeclarationWrites() {
  const client = useQueryClient();
  const onSuccess = () => client.invalidateQueries({ queryKey: ["inclusion_declarations"] });
  return {
    create: useMutation({
      mutationFn: (declaration: InclusionDeclarationFields) =>
        call("inclusion_declarations/create", { context: WORKSPACE, declaration, provenance: { via: "ui" } }),
      onSuccess,
    }),
    remove: useMutation({
      mutationFn: (id: number) =>
        call("inclusion_declarations/delete", { context: WORKSPACE, id, provenance: { via: "ui" } }),
      onSuccess,
    }),
  };
}
