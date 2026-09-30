import { ArrowsLeftRightIcon, BracketsCurlyIcon, FileTextIcon, GraphIcon, StackIcon, type Icon } from "@phosphor-icons/react";

import { formatCount } from "@/lib/model/format";
import { totals, type RepositoryModel } from "@/lib/model/repositories";

import classes from "./Views.module.css";

/** The workspace in five numbers, all counted from the index. */
export function Metrics({
  repositories,
  crossing,
}: {
  repositories: RepositoryModel[];
  /** Relationships whose ends sit in two repositories; null until known. */
  crossing: number | null;
}) {
  const sum = totals(repositories);
  const tiles: { icon: Icon; label: string; value: number | null }[] = [
    { icon: StackIcon, label: "Repositories", value: sum.repositories },
    { icon: FileTextIcon, label: "Files", value: sum.files },
    { icon: BracketsCurlyIcon, label: "Symbols", value: sum.symbols },
    { icon: GraphIcon, label: "Relationships", value: sum.relationships },
    { icon: ArrowsLeftRightIcon, label: "Across repositories", value: crossing },
  ];
  return (
    <dl className={classes.metrics} aria-label="Workspace totals">
      {/* each tile is exactly one term and one value, as a definition list requires */}
      {tiles.map(({ icon: Glyph, label, value }) => (
        <div key={label} className={classes.tile}>
          <dt className={classes.tileLabel}>
            <Glyph size={18} className={classes.tileIcon} aria-hidden />
            {label}
          </dt>
          <dd className={classes.tileValue}>{value === null ? "…" : formatCount(value)}</dd>
        </div>
      ))}
    </dl>
  );
}
