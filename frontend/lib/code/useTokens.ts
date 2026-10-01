"use client";

import { useQueries, type UseQueryResult } from "@tanstack/react-query";
import { useCallback } from "react";

import { tokenize, type CodeToken } from "./highlight";

/** A run of consecutive source lines, highlighted as one so a grammar keeps its state across them. */
export interface CodeBlock {
  start: number;
  lines: string[];
}

/** A short, stable fingerprint of a block's text, to key its tokens by. */
function fingerprint(text: string): string {
  let hash = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return `${text.length}:${(hash >>> 0).toString(36)}`;
}

/**
 * Each block's lines as coloured tokens, by line number; a line not (yet)
 * highlighted is absent and reads as plain text meanwhile. Computed once per
 * block text and kept, like any other answer.
 */
export function useTokens(blocks: readonly CodeBlock[], grammar: string | null): Map<number, CodeToken[]> {
  // stable while the blocks are, so the map is rebuilt only when a block's tokens land
  const combine = useCallback(
    (results: UseQueryResult<CodeToken[][] | null>[]) => {
      const lines = new Map<number, CodeToken[]>();
      results.forEach((result, i) => {
        result.data?.forEach((tokens, k) => lines.set(blocks[i]!.start + k, tokens));
      });
      return lines;
    },
    [blocks],
  );
  return useQueries({
    queries: blocks.map((block) => {
      const text = block.lines.join("\n");
      return {
        queryKey: ["tokens", grammar, block.start, fingerprint(text)],
        queryFn: () => tokenize(text, grammar),
        enabled: grammar !== null,
        staleTime: Infinity,
        gcTime: 5 * 60_000,
      };
    }),
    combine,
  });
}
