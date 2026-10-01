"use client";

import { ActionIcon, Tooltip } from "@mantine/core";
import { FlowArrowIcon } from "@phosphor-icons/react";

import { EntityBadge } from "@/components/graph/glyphs";
import { typeNoun } from "@/components/graph/traceNodes";
import type { SymbolIndex, SymbolTarget } from "@/lib/model/symbols";

import { Quiet } from "./Inspector";
import classes from "./Inspector.module.css";

/** The lines of this file each target is written on, as the references say. */
export function linesOf(index: SymbolIndex, key: string): number[] {
  const lines = new Set<number>();
  for (const [line, refs] of index.byLine) if (refs.some((r) => r.target.key === key)) lines.add(line);
  const target = index.targets.get(key);
  if (target?.here && target.line !== null) lines.add(target.line);
  return [...lines].sort((a, b) => a - b);
}

function where(target: SymbolTarget, repository: string): string {
  if (target.here) return target.line !== null ? `line ${target.line}` : "declared here";
  const file = target.path ? target.path.slice(target.path.lastIndexOf("/") + 1) : null;
  if (target.repository && target.repository !== repository) return `${target.repository}${file ? ` / ${file}` : ""}`;
  if (file) return file;
  // a module the index knows only by the name it is imported with
  return target.type === "module" || target.type === "package" ? "named import" : "shared";
}

/**
 * Every name in the file the index resolved: what it declares, then what it
 * refers to elsewhere. The list is the keyboard's way to everything the source
 * makes a link, and each row can follow its chain directly.
 */
export function SymbolList({
  index,
  repository,
  selected,
  onPick,
  onTrace,
}: {
  index: SymbolIndex;
  repository: string;
  selected: string | null;
  onPick: (key: string) => void;
  onTrace: (key: string) => void;
}) {
  const targets = [...index.targets.values()];
  const declared = targets.filter((t) => t.here).sort((a, b) => (a.line ?? 0) - (b.line ?? 0));
  const elsewhere = targets
    .filter((t) => !t.here)
    .sort((a, b) => Number(a.repository === repository) - Number(b.repository === repository) || a.name.localeCompare(b.name));
  if (targets.length === 0) return <Quiet>The index resolved no names in this file.</Quiet>;
  const row = (target: SymbolTarget) => (
    <li key={target.key} className={classes.rowItem}>
      <button
        type="button"
        className={classes.row}
        data-selected={target.key === selected || undefined}
        aria-pressed={target.key === selected}
        onClick={() => onPick(target.key)}
      >
        <EntityBadge entityType={target.type} size={22} />
        <span className={classes.rowText}>
          <span className={classes.rowLabel}>{target.name}</span>
          <span className={classes.rowDetail}>
            {typeNoun(target.type)} · {where(target, repository)}
          </span>
        </span>
        <span className={classes.rowCount}>{linesOf(index, target.key).length || ""}</span>
      </button>
      <Tooltip label={`Trace ${target.name}`}>
        <ActionIcon className={classes.go} size="md" variant="subtle" onClick={() => onTrace(target.key)} aria-label={`Trace ${target.name}`}>
          <FlowArrowIcon size={16} />
        </ActionIcon>
      </Tooltip>
    </li>
  );
  return (
    <div className={classes.sections}>
      {declared.length > 0 && (
        <section className={classes.section}>
          <h3 className={classes.sectionTitle}>
            Declared here <span className={classes.sectionCount}>{declared.length}</span>
          </h3>
          <ul className={classes.rows}>{declared.map(row)}</ul>
        </section>
      )}
      {elsewhere.length > 0 && (
        <section className={classes.section}>
          <h3 className={classes.sectionTitle}>
            Refers to <span className={classes.sectionCount}>{elsewhere.length}</span>
          </h3>
          <ul className={classes.rows}>{elsewhere.map(row)}</ul>
        </section>
      )}
    </div>
  );
}
