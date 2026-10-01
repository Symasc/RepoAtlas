import type { SymbolSpan } from "@/lib/model/symbols";

import { isComment, isString, type CodeToken } from "./highlight";

/** A piece of one line: its text, its colour, and the symbol it belongs to, if any. */
export interface Fragment {
  text: string;
  color?: string;
  italic?: boolean;
  key?: string;
}

/** Consecutive fragments of one symbol, or of none, rendered together. */
export interface FragmentGroup {
  key?: string;
  fragments: Fragment[];
}

/** Where each token sits in its line, and what kind of text it is. */
export interface TokenRuns {
  quiet: (offset: number) => boolean;
  quoted: (offset: number) => boolean;
}

function runsOf(tokens: readonly CodeToken[]): { start: number; end: number; token: CodeToken }[] {
  const runs = [];
  let at = 0;
  for (const token of tokens) {
    runs.push({ start: at, end: at + token.content.length, token });
    at += token.content.length;
  }
  return runs;
}

/** Comment and string offsets of a highlighted line, for finding names in it. */
export function tokenRuns(tokens: readonly CodeToken[] | undefined): TokenRuns {
  if (!tokens) return { quiet: () => false, quoted: () => true };
  const runs = runsOf(tokens);
  const at = (offset: number) => runs.find((r) => offset >= r.start && offset < r.end)?.token;
  return {
    quiet: (offset) => {
      const token = at(offset);
      return token ? isComment(token) || isString(token) : false;
    },
    quoted: (offset) => {
      const token = at(offset);
      return token ? isString(token) : false;
    },
  };
}

/** Font style bit for italics, as Shiki writes it. */
const ITALIC = 1;

/**
 * A line cut at every token edge and every symbol edge, then gathered into runs
 * of one symbol or none. Unhighlighted text is one plain token.
 */
export function fragmentGroups(text: string, tokens: readonly CodeToken[] | undefined, spans: readonly SymbolSpan[]): FragmentGroup[] {
  const runs = runsOf(tokens && tokens.length > 0 ? tokens : [{ content: text }]);
  const cuts = new Set<number>([0, text.length]);
  for (const r of runs) cuts.add(r.start).add(r.end);
  for (const s of spans) cuts.add(s.start).add(s.end);
  const edges = [...cuts].filter((c) => c >= 0 && c <= text.length).sort((a, b) => a - b);

  const groups: FragmentGroup[] = [];
  for (let i = 0; i < edges.length - 1; i++) {
    const start = edges[i]!;
    const end = edges[i + 1]!;
    if (end <= start) continue;
    const token = runs.find((r) => start >= r.start && start < r.end)?.token;
    const key = spans.find((s) => start >= s.start && start < s.end)?.key;
    const fragment: Fragment = { text: text.slice(start, end), color: token?.color, italic: ((token?.fontStyle ?? 0) & ITALIC) !== 0, key };
    const last = groups[groups.length - 1];
    if (last && last.key === key) last.fragments.push(fragment);
    else groups.push({ key, fragments: [fragment] });
  }
  return groups;
}
