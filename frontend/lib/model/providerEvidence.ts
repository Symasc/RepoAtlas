import type {
  BoundNote,
  CoverageLimitation,
  PreviewProviderDiagnostics,
  PreviewRow,
  ProviderLifecycle,
  UnpublishedMatch,
} from "@/lib/api/generated";

import { plural } from "./format";

/*
 * What the HTTP Connections preview can say of the routes a chosen repository declares in source and
 * does not serve (R1). Every sentence here is a hypothesis or a count, never a decision: the preview's
 * outcomes, counts, requirements and `can_create` come from the server and are not read from this
 * module. No sentence claims a route is absent: an unproven match is labelled "not proven", and
 * "no match" is always said to be no proof that the repository has no such route.
 */

/** A category the server can name, in the order the interface lists them. */
export const CATEGORY_ORDER = [
  "registration-unproven",
  "path-unresolved",
  "identity-unresolved",
  "framework-constraint",
  "bound-reached",
  "unclassified",
] as const;

export type EvidenceCategory = (typeof CATEGORY_ORDER)[number];

/** Why a recognized declaration is not served, in a sentence; the code is kept beside it. */
export const CATEGORY_TEXT: Readonly<Record<EvidenceCategory, string>> = {
  "registration-unproven": "found in source; no proven application registers it (registration not proven)",
  "path-unresolved": "found in source; its method or full path could not be resolved",
  "identity-unresolved": "found in source; the application or router it belongs to could not be identified",
  "framework-constraint": "found in source; withheld by a framework rule RepoAtlas applies",
  "bound-reached": "found in source; an analysis limit was reached before it could be proven",
  unclassified: "found in source, not proven served; RepoAtlas could not classify why",
};

/** A recognized declaration's status, in words. Neither is a statement that the route is not served by anything. */
export const STATUS_TEXT: Readonly<Record<UnpublishedMatch["status"], string>> = {
  declared: "declared in source, not proven served",
  unpublished: "recognized in source, not published as served",
};

export const EVIDENCE_LABEL = "Provider evidence";
export const HINT_LABEL = "Possible matches (not proven)";
export const HINT_HEADING = "Possible matches — hypotheses, not connections";
export const NOT_PROVEN = "not proven";
export const HYPOTHESIS_NOTE = "Nothing is joined through these; the decision above is unchanged.";

/** The note under a list that has a `no matching route` row: that outcome is not an absence. */
export const NO_MATCH_NOTE = (repository: string): string =>
  `'no matching route' means no route this index proves ${repository} serves matches the request. It is not proof that ${repository} has no such route.`;

function isCategory(value: string | null | undefined): value is EvidenceCategory {
  return (CATEGORY_ORDER as readonly string[]).includes(value ?? "");
}

/** A category's sentence. A category the table does not know reads as unclassified: never guessed. */
export function categoryText(category: string | null | undefined): string {
  return isCategory(category) ? CATEGORY_TEXT[category] : CATEGORY_TEXT.unclassified;
}

const count = (value: number | undefined): number => value ?? 0;

/** The repository's recognized declarations, by what became of them, as one sentence. */
export function summaryLine(repository: string, lifecycle: ProviderLifecycle): string {
  return [
    `In ${repository}'s indexed source: ${plural(count(lifecycle.recognized), "route declaration")} recognized`,
    `${count(lifecycle.published)} served`,
    `${count(lifecycle.declared)} declared, not proven`,
    `${count(lifecycle.unpublished)} recognized, not published`,
  ].join(" · ");
}

export interface CategoryLine {
  category: string;
  count: number;
  text: string;
}

/** One line per category with a declaration in it: the count, then the category's sentence. */
export function categoryLines(lifecycle: ProviderLifecycle): CategoryLine[] {
  const counts = lifecycle.by_category ?? {};
  const present = Object.keys(counts).filter((key) => count(counts[key]) > 0);
  const known = CATEGORY_ORDER.filter((key) => present.includes(key));
  const others = present.filter((key) => !isCategory(key)).sort();
  return [...known, ...others].map((category) => ({
    category,
    count: count(counts[category]),
    text: `${plural(count(counts[category]), "declaration")}: ${categoryText(category)}`,
  }));
}

/** The stable codes the server recorded, with how many declarations carry each. */
export function codeTags(lifecycle: ProviderLifecycle): { code: string; count: number }[] {
  const counts = lifecycle.by_code ?? {};
  return Object.keys(counts)
    .filter((code) => count(counts[code]) > 0)
    .map((code) => ({ code, count: count(counts[code]) }));
}

/** Said of the repository when no route declaration was recognized in it; null otherwise. */
export function absenceLine(repository: string, lifecycle: ProviderLifecycle): string | null {
  return count(lifecycle.recognized) === 0
    ? `No route declaration was recognized in ${repository}'s indexed source; this is not proof it serves none.`
    : null;
}

/** What the server says it does not read, each as its code and its note. Shown beside the line above. */
export function notExtractedLines(lifecycle: ProviderLifecycle): CoverageLimitation[] {
  return [...(lifecycle.not_extracted ?? [])];
}

/**
 * Said of one request that matched nothing and has no hint to show. `unreached` is the number of served
 * routes a declaration cannot reach (no-declaration-line): one of them may answer the request.
 */
export function requestAbsenceLine(repository: string, unreached: number): string {
  const head = `No route this declaration can reach in ${repository}, and no unproven declaration the index recognized there, matches this request.`;
  const uncompared =
    unreached > 0
      ? ` ${unreached} served ${unreached === 1 ? "route" : "routes"} built from a prefix could not be compared (no-declaration-line), so one of them may answer it.`
      : "";
  return `${head}${uncompared} RepoAtlas does not read every way a route can be declared, so this is not proof that ${repository} has no such route.`;
}

/** Said when analysis limits were reached, so the counts above may be short. */
export function truncationLine(repository: string, lifecycle: ProviderLifecycle): string | null {
  return lifecycle.truncated === true
    ? `Analysis limits were reached in ${repository}; these counts may be incomplete.`
    : null;
}

/** One bound that was reached: its name, its limit, and where. */
export function boundLine(note: BoundNote): string {
  const limit = note.limit != null ? ` (limit ${note.limit})` : "";
  const where = note.path ? ` in ${note.path}` : ` in the ${note.scope}`;
  return `${note.bound}${limit}${where}`;
}

/** Every bound reached, then what the server counted but did not list. */
export function boundLines(lifecycle: ProviderLifecycle): string[] {
  const lines = (lifecycle.bounds_reached ?? []).map(boundLine);
  const notes = count(lifecycle.bound_notes_omitted);
  if (notes > 0) lines.push(`${plural(notes, "more analysis limit")} reached, not listed.`);
  const rows = count(lifecycle.unpublished_omitted);
  if (rows > 0) lines.push(`${plural(rows, "more recognized declaration")} counted above, not listed.`);
  return lines;
}

/** What the declaration could not reach among the routes the repository serves (F1): at most two lines. */
export function reachLines(repository: string, diagnostics: PreviewProviderDiagnostics): string[] {
  const lines: string[] = [];
  const unreached = count(diagnostics.routes_without_declaration_line);
  if (unreached > 0) {
    const many = unreached !== 1;
    lines.push(
      `${unreached} ${many ? "routes" : "route"} ${repository} serves ${many ? "are" : "is"} built from a prefix and ${
        many ? "state" : "states"
      } no single source line, so a declaration cannot reach ${many ? "them" : "it"} (no-declaration-line).`,
    );
  }
  if (diagnostics.exposures_truncated === true) {
    lines.push("Only the first 2,000 routes were offered to this declaration; others were not considered.");
  }
  return lines;
}

/** The possible matches by the request they resemble. A request the server named no match for has no entry. */
export function hintsByRequest(
  diagnostics: PreviewProviderDiagnostics | null | undefined,
): Map<string, UnpublishedMatch[]> {
  const grouped = new Map<string, UnpublishedMatch[]>();
  for (const match of diagnostics?.unpublished_matches ?? []) {
    const list = grouped.get(match.decision_ref);
    if (list) list.push(match);
    else grouped.set(match.decision_ref, [match]);
  }
  return grouped;
}

/** Said when the server found more possible matches than it lists; null when it listed them all. */
export function moreHintsLine(diagnostics: PreviewProviderDiagnostics): string | null {
  const listed = (diagnostics.unpublished_matches ?? []).length;
  const missing = count(diagnostics.unpublished_match_count) - listed;
  return missing > 0 ? `${plural(missing, "more possible match", "more possible matches")} not listed.` : null;
}

export interface HintParts {
  status: string;
  route: string;
  place: string | null;
  category: string;
  code: string | null;
  detail: string | null;
}

/** A possible match as its separate statements. The wording of its category is the server's category, never a guess. */
export function hintParts(match: UnpublishedMatch): HintParts {
  return {
    status: STATUS_TEXT[match.status],
    route: match.route,
    place: match.path ? `${match.path}${match.line != null ? `:${match.line}` : ""}` : null,
    category: categoryText(match.category),
    code: match.code ? match.code : null,
    detail: match.detail ? match.detail : null,
  };
}

/** A possible match as one line of text. */
export function hintLine(match: UnpublishedMatch): string {
  const parts = hintParts(match);
  return [NOT_PROVEN, parts.route, parts.place, parts.status, parts.category, parts.code, parts.detail]
    .filter((part): part is string => !!part)
    .join(" · ");
}

/** What is shown under one preview row: the possible matches it resembles, or why none is shown. */
export function rowEvidence(
  diagnostics: PreviewProviderDiagnostics | null | undefined,
  repository: string,
  row: Pick<PreviewRow, "decision_ref" | "outcome">,
): { matches: UnpublishedMatch[]; absence: string | null } {
  if (!diagnostics) return { matches: [], absence: null };
  const matches = hintsByRequest(diagnostics).get(row.decision_ref) ?? [];
  // when the server cut its list, a request with no match shown may still have one: say nothing of it;
  // when it offered only the first routes to the declaration, say nothing of the rest either
  const cut = count(diagnostics.unpublished_match_count) > (diagnostics.unpublished_matches ?? []).length;
  const unmatched =
    matches.length === 0 &&
    row.outcome === "no-matching-route" &&
    !cut &&
    diagnostics.exposures_truncated !== true;
  return {
    matches,
    absence: unmatched ? requestAbsenceLine(repository, count(diagnostics.routes_without_declaration_line)) : null,
  };
}
