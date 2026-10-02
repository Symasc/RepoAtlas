import { formatCount } from "@/lib/model/format";
import { totals, type RepositoryModel } from "@/lib/model/repositories";

import classes from "./Views.module.css";

/** The workspace in one line of figures, all counted from the index. */
export function Totals({
  repositories,
  crossing,
}: {
  repositories: RepositoryModel[];
  /** Relationships whose ends sit in two repositories; null until known. */
  crossing: number | null;
}) {
  const sum = totals(repositories);
  const figures: { label: string; value: number | null }[] = [
    { label: sum.repositories === 1 ? "repository" : "repositories", value: sum.repositories },
    { label: sum.files === 1 ? "file" : "files", value: sum.files },
    { label: sum.symbols === 1 ? "symbol" : "symbols", value: sum.symbols },
    { label: sum.relationships === 1 ? "relationship" : "relationships", value: sum.relationships },
    { label: "across repositories", value: crossing },
  ];
  return (
    <dl className={classes.totals} aria-label="Workspace totals">
      {/* each figure is exactly one term and one value, as a definition list requires;
          the value is drawn first, so it reads "4 repositories" */}
      {figures.map(({ label, value }) => (
        <div key={label} className={classes.figure}>
          <dt>{label}</dt>
          <dd>{value === null ? "…" : formatCount(value)}</dd>
        </div>
      ))}
    </dl>
  );
}
