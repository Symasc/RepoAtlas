/** Where an entry is stated: the stable identity a declaration is keyed by. */
export function entryKey(e: { repository: string; file: string; path: string }): string {
  return `${e.repository}\u0000${e.file}\u0000${e.path}`;
}

/**
 * What the "not linked" list says about itself, or null when it says nothing more. `listed` is
 * how many entries the list holds and `total` how many the index counts: they differ when the
 * list is cut at its limit, and when a candidate repository is outside this view.
 */
export function unsettledSummary(listed: number, total: number): string | null {
  if (total === 0) return "Every inclusion the index reads is linked.";
  if (total > listed) {
    return `Showing ${listed} of ${total}. The others are not listed: past the list's limit, or a candidate repository is outside this view.`;
  }
  return null;
}

/**
 * The React key of an unsettled entry's row. It includes the declared target, so the row
 * remounts when a declaration arrives and its picker starts from the declared repository
 * rather than keeping the choice it was first mounted with.
 */
export function unsettledRowKey(
  entry: { repository: string; file: string; path: string },
  declared: { target: string } | null | undefined,
): string {
  return `${entryKey(entry)}:${declared?.target ?? ""}`;
}
