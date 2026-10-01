"use client";

import { Loader, UnstyledButton } from "@mantine/core";
import { useReducedMotion } from "@mantine/hooks";
import { useQueries, type UseQueryResult } from "@tanstack/react-query";
import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent,
} from "react";

import { useRegisterExit, type Direction } from "@/components/graph/transition";
import { typeNoun } from "@/components/graph/traceNodes";
import { call } from "@/lib/api/client";
import type { InspectResponse } from "@/lib/api/generated";
import { inRepository, useSource } from "@/lib/api/topology";
import { fragmentGroups, tokenRuns, type FragmentGroup } from "@/lib/code/fragments";
import { grammarFor, type CodeToken } from "@/lib/code/highlight";
import { useTokens, type CodeBlock } from "@/lib/code/useTokens";
import { formatCount } from "@/lib/model/format";
import { spansFor, type SymbolIndex, type SymbolSpan, type SymbolTarget } from "@/lib/model/symbols";

import classes from "./CodeReader.module.css";

type Segment = NonNullable<InspectResponse["segments"]>[number];
type Piece = { kind: "line"; number: number; text: string } | { kind: "gap"; start: number; end: number };

/** Lines one press on a gap reads, at most: within what one inspection returns. */
const GAP_STEP = 400;

/** Every segment the extra reads returned, and whether one is still out. Module scope, so it is stable. */
function gathered(results: UseQueryResult<InspectResponse>[]) {
  return {
    segments: results.flatMap((r) => r.data?.segments ?? []),
    loading: results.some((r) => r.isPending),
  };
}

function piecesOf(segments: readonly Segment[], total: number | null): Piece[] {
  const sorted = segments
    .filter((s) => !s.declaration && s.ref.line_start != null)
    .sort((a, b) => a.ref.line_start! - b.ref.line_start!);
  const lines = new Map<number, string>();
  for (const segment of sorted) {
    const text = (segment.text ?? "").replace(/\n$/, "").split("\n");
    text.forEach((line, i) => lines.set(segment.ref.line_start! + i, line));
  }
  const numbers = [...lines.keys()].sort((a, b) => a - b);
  const out: Piece[] = [];
  let next = 1;
  for (const number of numbers) {
    if (number > next) out.push({ kind: "gap", start: next, end: number - 1 });
    out.push({ kind: "line", number, text: lines.get(number)! });
    next = number + 1;
  }
  if (total !== null && next <= total && numbers.length > 0) out.push({ kind: "gap", start: next, end: total });
  return out;
}

function blocksOf(pieces: readonly Piece[]): CodeBlock[] {
  const blocks: CodeBlock[] = [];
  for (const piece of pieces) {
    if (piece.kind !== "line") continue;
    const last = blocks[blocks.length - 1];
    if (last && last.start + last.lines.length === piece.number) last.lines.push(piece.text);
    else blocks.push({ start: piece.number, lines: [piece.text] });
  }
  return blocks;
}

function labelFor(target: SymbolTarget | undefined, here: { repository: string; path: string }): string {
  if (!target) return "symbol";
  const where = target.here
    ? "declared here"
    : target.repository && target.repository !== here.repository
      ? `in ${target.repository}`
      : target.path
        ? `in ${target.path.slice(target.path.lastIndexOf("/") + 1)}`
        : target.type === "module" || target.type === "package"
          ? "imported by name"
          : "shared";
  return `${target.name}, ${typeNoun(target.type).toLowerCase()} ${where}`;
}

const Line = memo(function Line({
  number,
  groups,
  selected,
  hit,
  index,
  here,
}: {
  number: number;
  groups: FragmentGroup[];
  selected: string | null;
  hit: boolean;
  index: SymbolIndex;
  here: { repository: string; path: string };
}) {
  const current = selected !== null && groups.some((g) => g.key === selected);
  return (
    <span className={classes.line} data-line={number} data-hit={hit || undefined} data-current={current || undefined}>
      <span className={classes.number} aria-hidden>
        {number}
      </span>
      <span className={classes.text}>
        {groups.length === 0
          ? " "
          : groups.map((group, i) => {
              const parts = group.fragments.map((f, k) => (
                <span key={k} style={f.color ? ({ color: f.color, fontStyle: f.italic ? "italic" : undefined } as CSSProperties) : undefined}>
                  {f.text}
                </span>
              ));
              if (!group.key) return <span key={i}>{parts}</span>;
              const target = index.targets.get(group.key);
              return (
                <button
                  key={i}
                  type="button"
                  tabIndex={-1}
                  className={classes.symbol}
                  data-key={group.key}
                  data-current={group.key === selected || undefined}
                  data-elsewhere={(target && !target.here) || undefined}
                  aria-label={labelFor(target, here)}
                >
                  {parts}
                </button>
              );
            })}
      </span>
    </span>
  );
});

/**
 * A file's source as RepoAtlas indexed it, coloured, with every name the index
 * resolved made a link: a press selects what it stands for (every use of it in
 * the file lights up), a second press follows its chain. The lines one read
 * did not return are said, and read on request.
 */
export function CodeReader({
  repository,
  path,
  index,
  selected,
  line,
  onPick,
  arrival,
}: {
  repository: string;
  path: string;
  /** What the file's names stand for, from its references. */
  index: SymbolIndex;
  selected: string | null;
  line: number | null;
  onPick: (key: string) => void;
  arrival: Direction;
}) {
  const here = useMemo(() => ({ repository, path }), [repository, path]);
  const source = useSource(repository, path, true);

  // lines past what the first read returned, read a gap at a time on request
  const [wanted, setWanted] = useState<{ start: number; end: number }[]>([]);
  const more = useQueries({
    queries: wanted.map((range) => ({
      queryKey: ["inspect", repository, path, "range", range.start, range.end],
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        call(
          "inspect",
          {
            context: inRepository(repository),
            target: { path },
            line_start: range.start,
            line_end: range.end,
            context_lines: 0,
            presentation: "full",
            budget: { max_items: 18, max_total_chars: 32_000, max_item_chars: 8_000 },
          },
          signal,
        ),
      staleTime: 5 * 60_000,
    })),
    combine: gathered,
  });
  const loading = more.loading;
  const total = source.data?.file.line_count ?? null;
  const pieces = useMemo(
    () => piecesOf([...(source.data?.segments ?? []), ...more.segments], total),
    [source.data, total, more.segments],
  );

  const grammar = grammarFor(path);
  const blocks = useMemo(() => blocksOf(pieces), [pieces]);
  const tokens = useTokens(blocks, grammar);
  const lines = useMemo(() => {
    const out = new Map<number, { spans: SymbolSpan[]; groups: FragmentGroup[] }>();
    for (const piece of pieces) {
      if (piece.kind !== "line") continue;
      const lineTokens: CodeToken[] | undefined = tokens.get(piece.number);
      const spans = spansFor(piece.number, piece.text, index, tokenRuns(lineTokens));
      out.set(piece.number, { spans, groups: fragmentGroups(piece.text, lineTokens, spans) });
    }
    return out;
  }, [pieces, tokens, index]);

  // one press handler for every name in the file
  const onClick = useCallback(
    (event: MouseEvent<HTMLElement>) => {
      const symbol = (event.target as HTMLElement).closest<HTMLElement>("[data-key]");
      if (symbol?.dataset.key) onPick(symbol.dataset.key);
    },
    [onPick],
  );

  // the line asked for, brought into view
  const scroller = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (line === null) return;
    scroller.current?.querySelector<HTMLElement>(`[data-line="${line}"]`)?.scrollIntoView({ block: "center" });
  }, [line, pieces]);

  // leaving into a chain: the view closes in on the name pressed, then goes
  const reduced = useReducedMotion() ?? false;
  const stage = useRef<HTMLDivElement>(null);
  const [leaving, setLeaving] = useState<{ origin: string; direction: Direction } | null>(null);
  const register = useRegisterExit();
  useLayoutEffect(
    () =>
      register?.(async (direction, via) => {
        if (reduced || direction === "none") return;
        let origin = "50% 40%";
        const box = stage.current?.getBoundingClientRect();
        const symbol = via ? stage.current?.querySelector<HTMLElement>(`[data-key="${CSS.escape(via)}"][data-current]`) : null;
        if (box && symbol) {
          const at = symbol.getBoundingClientRect();
          origin = `${at.left + at.width / 2 - box.left}px ${at.top + at.height / 2 - box.top}px`;
        }
        setLeaving({ origin, direction });
        await new Promise((resolve) => setTimeout(resolve, direction === "in" ? 360 : 200));
      }),
    [register, reduced],
  );

  if (source.isPending) {
    return (
      <p className={classes.status}>
        <Loader size={12} type="dots" /> Reading {path}
      </p>
    );
  }
  if (source.error) return <p className={classes.status} role="alert">{source.error.message}</p>;

  return (
    <div
      ref={stage}
      className={classes.reader}
      data-arrival={reduced ? undefined : arrival}
      data-leaving={leaving?.direction}
      style={leaving ? { transformOrigin: leaving.origin } : undefined}
    >
      {pieces.length === 0 ? (
        <p className={classes.status}>The index holds no text for this file.</p>
      ) : (
        // focusable, so the keyboard scrolls it; its names are reached through the inspector's list
        <div className={classes.scroll} ref={scroller} tabIndex={0} role="region" aria-label={`Source of ${path.slice(path.lastIndexOf("/") + 1)}`}>
          <pre className={classes.code} onClick={onClick}>
            {pieces.map((piece) =>
              piece.kind === "gap" ? (
                <span key={`gap-${piece.start}`} className={classes.gap}>
                  <span>
                    ⋯ lines {formatCount(piece.start)}–{formatCount(piece.end)} not read yet
                  </span>
                  <UnstyledButton
                    className={classes.gapButton}
                    disabled={loading}
                    onClick={() => setWanted((w) => [...w, { start: piece.start, end: Math.min(piece.end, piece.start + GAP_STEP - 1) }])}
                  >
                    Read {formatCount(Math.min(GAP_STEP, piece.end - piece.start + 1))} more
                  </UnstyledButton>
                </span>
              ) : (
                <Line
                  key={piece.number}
                  number={piece.number}
                  groups={lines.get(piece.number)?.groups ?? []}
                  selected={selected}
                  hit={line === piece.number}
                  index={index}
                  here={here}
                />
              ),
            )}
          </pre>
        </div>
      )}
    </div>
  );
}
