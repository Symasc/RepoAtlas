"use client";

import { Loader } from "@mantine/core";
import { useEffect, useMemo, useRef } from "react";

import { useSource } from "@/lib/api/topology";
import { formatCount } from "@/lib/model/format";

import type { LineSpan } from "./Evidence";
import { Quiet } from "./Inspector";
import classes from "./Inspector.module.css";

type Piece = { kind: "line"; number: number; text: string } | { kind: "gap"; start: number; end: number };

/**
 * The file as RepoAtlas indexed it: as much as one inspection returns, with
 * the lines it did not return said rather than skipped silently.
 */
export function CodeView({ repository, path, highlight }: { repository: string; path: string; highlight: LineSpan | null }) {
  const source = useSource(repository, path, true);
  const pieces = useMemo<Piece[]>(() => {
    const segments = [...(source.data?.segments ?? [])]
      .filter((s) => !s.declaration && s.ref.line_start != null)
      .sort((a, b) => a.ref.line_start! - b.ref.line_start!);
    const out: Piece[] = [];
    let next = 1;
    for (const segment of segments) {
      const start = segment.ref.line_start!;
      if (start < next) continue;
      if (start > next) out.push({ kind: "gap", start: next, end: start - 1 });
      const lines = (segment.text ?? "").replace(/\n$/, "").split("\n");
      lines.forEach((text, i) => out.push({ kind: "line", number: start + i, text }));
      next = start + lines.length;
    }
    const total = source.data?.file.line_count ?? null;
    if (total !== null && next <= total && segments.length > 0) out.push({ kind: "gap", start: next, end: total });
    return out;
  }, [source.data]);

  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!highlight) return;
    const line = container.current?.querySelector<HTMLElement>(`[data-line="${highlight.start}"]`);
    line?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [highlight, pieces]);

  if (source.isPending) {
    return (
      <p className={classes.loading}>
        <Loader size={12} type="dots" /> Reading {path}
      </p>
    );
  }
  if (source.error) return <p className={classes.problem}>{source.error.message}</p>;
  const file = source.data.file;
  const coverage = source.data.coverage;
  const lineCount = pieces.filter((p) => p.kind === "line").length;
  return (
    <div className={classes.code}>
      <p className={classes.codeMeta}>
        {file.line_count != null
          ? `${formatCount(lineCount)} of ${formatCount(file.line_count)} lines`
          : `${formatCount(lineCount)} lines`}
        {" · "}
        {coverage.source === "verified_worktree" ? "read from the working tree, which matches the index" : "from the indexed text"}
      </p>
      {pieces.length === 0 ? (
        <Quiet>The index holds no text for this file.</Quiet>
      ) : (
        <div className={classes.codeScroll} ref={container}>
          <pre className={classes.codeBlock}>
            {pieces.map((piece) =>
              piece.kind === "gap" ? (
                <span key={`gap-${piece.start}`} className={classes.codeGap}>
                  ⋯ lines {formatCount(piece.start)}–{formatCount(piece.end)} not returned
                </span>
              ) : (
                <span
                  key={piece.number}
                  className={classes.codeLine}
                  data-line={piece.number}
                  data-hit={highlight && piece.number >= highlight.start && piece.number <= highlight.end ? true : undefined}
                >
                  <span className={classes.lineNumber}>{piece.number}</span>
                  <span>{piece.text || " "}</span>
                </span>
              ),
            )}
          </pre>
        </div>
      )}
    </div>
  );
}
