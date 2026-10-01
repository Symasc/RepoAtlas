"use client";

import { Loader } from "@mantine/core";
import { useMemo, type CSSProperties } from "react";

import { useLines } from "@/lib/api/topology";
import { fragmentGroups } from "@/lib/code/fragments";
import { grammarFor } from "@/lib/code/highlight";
import { useTokens, type CodeBlock } from "@/lib/code/useTokens";

import classes from "./Snippet.module.css";

/**
 * A few lines of one file, coloured, with the lines a step names marked: what
 * the index read there, recorded in the investigation of `subject`.
 */
export function Snippet({
  repository,
  path,
  lines,
  subject,
  around = 3,
}: {
  repository: string;
  path: string;
  lines: { start: number; end: number };
  subject: string;
  around?: number;
}) {
  const read = useLines(repository, path, lines, subject, around);
  const block = useMemo<CodeBlock[]>(() => {
    const segments = [...(read.data?.segments ?? [])]
      .filter((s) => !s.declaration && s.ref.line_start != null)
      .sort((a, b) => a.ref.line_start! - b.ref.line_start!);
    const first = segments[0];
    if (!first) return [];
    const text = segments.flatMap((s) => (s.text ?? "").replace(/\n$/, "").split("\n"));
    return [{ start: first.ref.line_start!, lines: text }];
  }, [read.data]);
  const tokens = useTokens(block, grammarFor(path));

  if (read.isPending) {
    return (
      <p className={classes.status}>
        <Loader size={12} type="dots" /> Reading {path.slice(path.lastIndexOf("/") + 1)}
      </p>
    );
  }
  if (read.error) return <p className={classes.status}>{read.error.message}</p>;
  const only = block[0];
  if (!only) return <p className={classes.status}>The index holds no text for these lines.</p>;
  return (
    // focusable, so a line wider than the panel can be scrolled from the keyboard
    <pre className={classes.snippet} tabIndex={0} role="group" aria-label={`${path}, lines ${lines.start} to ${lines.end}`}>
      {only.lines.map((text, i) => {
        const number = only.start + i;
        const marked = number >= lines.start && number <= lines.end;
        return (
          <span key={number} className={classes.line} data-marked={marked || undefined}>
            <span className={classes.number} aria-hidden>
              {number}
            </span>
            <span className={classes.text}>
              {fragmentGroups(text, tokens.get(number), []).flatMap((g) =>
                g.fragments.map((f, k) => (
                  <span
                    key={`${number}-${k}-${f.text.length}`}
                    style={f.color ? ({ color: f.color, fontStyle: f.italic ? "italic" : undefined } as CSSProperties) : undefined}
                  >
                    {f.text}
                  </span>
                )),
              )}
              {text === "" ? " " : null}
            </span>
          </span>
        );
      })}
    </pre>
  );
}
