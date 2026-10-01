import type { ChildEntry, EntityRef, LineReference } from "@/lib/api/generated";

/**
 * Which names in a file's source lead somewhere: pure, so the rules are tested
 * without a browser. A name is a link only where the index says so: a reference
 * the file asserts on that line, a declaration made on it, a use of a name the
 * file declares exactly once, or, on an import line, a binding the file goes on
 * to use. Everything else stays text, however it looks.
 */

/** Something a name in the source stands for. */
export interface SymbolTarget {
  key: string;
  name: string;
  type: string;
  repository: string | null;
  path: string | null;
  line: number | null;
  /** Declared in the file being read. */
  here: boolean;
}

/** One occurrence of a name that leads somewhere, as character offsets in its line. */
export interface SymbolSpan {
  start: number;
  end: number;
  key: string;
}

export interface SymbolIndex {
  targets: Map<string, SymbolTarget>;
  /** References by the lines they cover. */
  byLine: Map<number, LineReference[]>;
  /** Declarations by the line they start on. */
  declaredAt: Map<number, ChildEntry[]>;
  /** Names declared exactly once in the file, by name. */
  unique: Map<string, string>;
  /**
   * Names the file refers to elsewhere, where only one such target has that
   * name: what an import line's bindings stand for.
   */
  imported: Map<string, string>;
}

/** What never stands for a name in the source: the file itself, its imports' files. */
const NOT_NAMES: ReadonlySet<string> = new Set(["file", "repository"]);
/** Where a name is ambiguous on one line, the thing it names before the place it comes from. */
const PREFERENCE: Record<string, number> = { function: 0, method: 0, class: 0, symbol: 1, variable: 1, module: 3, package: 3 };

function rank(type: string): number {
  return PREFERENCE[type] ?? 2;
}

function target(ref: EntityRef, here: { repository: string; path: string }): SymbolTarget {
  return {
    key: ref.key,
    name: ref.name ?? "",
    type: ref.type,
    repository: ref.repository_slug ?? null,
    path: ref.path ?? null,
    line: ref.line_start ?? null,
    here: ref.repository_slug === here.repository && ref.path === here.path,
  };
}

export function symbolIndex(
  declarations: readonly ChildEntry[],
  references: readonly LineReference[],
  here: { repository: string; path: string },
): SymbolIndex {
  const targets = new Map<string, SymbolTarget>();
  const byLine = new Map<number, LineReference[]>();
  const declaredAt = new Map<number, ChildEntry[]>();
  const counts = new Map<string, string[]>();

  for (const d of declarations) {
    if (!d.key || NOT_NAMES.has(d.type ?? "") || d.type === "module") continue;
    targets.set(d.key, {
      key: d.key,
      name: d.name,
      type: d.type ?? "symbol",
      repository: here.repository,
      path: here.path,
      line: d.line_start ?? null,
      here: true,
    });
    if (d.line_start != null) declaredAt.set(d.line_start, [...(declaredAt.get(d.line_start) ?? []), d]);
    // a method is also reached by its own name: `Class.method` is written `method`
    const short = d.name.slice(d.name.lastIndexOf(".") + 1);
    counts.set(short, [...(counts.get(short) ?? []), d.key]);
  }
  for (const r of references) {
    if (NOT_NAMES.has(r.target.type)) continue;
    if (!targets.has(r.target.key)) targets.set(r.target.key, target(r.target, here));
    for (let line = r.line_start; line <= r.line_end; line++) {
      byLine.set(line, [...(byLine.get(line) ?? []), r]);
    }
  }
  const unique = new Map<string, string>();
  for (const [name, keys] of counts) if (keys.length === 1) unique.set(name, keys[0]!);
  const elsewhere = new Map<string, Set<string>>();
  for (const t of targets.values()) {
    if (t.here || t.type === "module" || t.type === "package") continue;
    const short = shortName(t.name);
    elsewhere.set(short, (elsewhere.get(short) ?? new Set()).add(t.key));
  }
  const imported = new Map<string, string>();
  for (const [name, keys] of elsewhere) if (keys.size === 1 && !counts.has(name)) imported.set(name, [...keys][0]!);
  return { targets, byLine, declaredAt, unique, imported };
}

const IDENTIFIER = /[A-Za-z_$][\w$]*/g;

/** The part of a name its uses are written with. */
function shortName(name: string): string {
  return name.slice(name.lastIndexOf(".") + 1);
}

/**
 * Spans of one line that lead somewhere. `quiet` says, per character, where a
 * name cannot be (a comment, a string), and `quoted` where a string sits, which
 * is the only place an import's specifier is.
 */
export function spansFor(
  line: number,
  text: string,
  index: SymbolIndex,
  where: { quiet?: (offset: number) => boolean; quoted?: (offset: number) => boolean } = {},
): SymbolSpan[] {
  const spans: SymbolSpan[] = [];
  const refs = index.byLine.get(line) ?? [];
  // a line that imports a module or package: its bindings are names used below
  const importing = refs.some((r) => r.target.type === "module" || r.target.type === "package");

  // an import's specifier, in quotes: the module or package it names
  for (const r of refs) {
    if (r.target.type !== "module" && r.target.type !== "package") continue;
    const name = r.target.name ?? "";
    if (!name) continue;
    for (const quote of ['"', "'", "`"]) {
      const at = text.indexOf(`${quote}${name}${quote}`);
      if (at >= 0 && (where.quoted?.(at + 1) ?? true)) {
        spans.push({ start: at + 1, end: at + 1 + name.length, key: r.target.key });
        break;
      }
    }
  }

  for (const match of text.matchAll(IDENTIFIER)) {
    const start = match.index;
    const end = start + match[0].length;
    if (where.quiet?.(start)) continue;
    if (spans.some((s) => start < s.end && s.start < end)) continue;
    const name = match[0];
    const said = refs
      .filter((r) => r.target.type !== "module" && r.target.type !== "package" && shortName(r.target.name ?? "") === name)
      .sort((a, b) => rank(a.target.type) - rank(b.target.type) || a.target.key.localeCompare(b.target.key))[0];
    const declared = (index.declaredAt.get(line) ?? []).find((d) => shortName(d.name) === name);
    const key = said?.target.key ?? declared?.key ?? index.unique.get(name) ?? (importing ? index.imported.get(name) : undefined) ?? null;
    if (key && index.targets.has(key)) spans.push({ start, end, key });
  }
  return spans.sort((a, b) => a.start - b.start);
}
