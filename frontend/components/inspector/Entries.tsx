"use client";

import { Loader } from "@mantine/core";
import { CaretRightIcon, PuzzlePieceIcon } from "@phosphor-icons/react";

import { PathBadge } from "@/components/graph/glyphs";
import type { ChildEntry } from "@/lib/api/generated";
import { useChildren } from "@/lib/api/topology";
import { formatCount, plural } from "@/lib/model/format";

import { Quiet } from "./Inspector";
import classes from "./Inspector.module.css";

/** Directories, files or declarations, each a way in (a declaration, a line to read). */
export function EntryList({
  entries,
  total,
  onGo,
  onLine,
}: {
  entries: readonly ChildEntry[];
  total?: number;
  /** Into a directory or file; a list of declarations has none. */
  onGo?: (path: string) => void;
  onLine?: (line: number) => void;
}) {
  if (entries.length === 0) return <Quiet>Nothing is indexed here.</Quiet>;
  const sorted = [...entries].sort((a, b) => Number(b.kind === "directory") - Number(a.kind === "directory"));
  return (
    <>
      <ul className={classes.rows}>
        {sorted.map((entry) => (
          <li key={entry.id}>
            {entry.kind === "declaration" ? (
              <button
                type="button"
                className={classes.entry}
                data-static={onLine && entry.line_start ? undefined : true}
                onClick={() => entry.line_start && onLine?.(entry.line_start)}
              >
                <PuzzlePieceIcon size={16} className={classes.entryIcon} aria-hidden />
                <span className={classes.entryName}>{entry.name}</span>
                <span className={classes.entryMeta}>
                  {entry.type?.replace(/_/g, " ")}
                  {entry.line_start ? ` · line ${entry.line_start}` : ""}
                </span>
              </button>
            ) : (
              <button type="button" className={classes.entry} onClick={() => onGo?.(entry.path)}>
                <PathBadge path={entry.name} file={entry.kind !== "directory"} />
                <span className={classes.entryName}>{entry.name}</span>
                <span className={classes.entryMeta}>
                  {entry.kind === "directory" ? plural(entry.files ?? 0, "file") : (entry.language ?? "")}
                </span>
                <CaretRightIcon size={12} className={classes.rowDetail} aria-hidden />
              </button>
            )}
          </li>
        ))}
      </ul>
      {total !== undefined && total > entries.length && (
        <Quiet>
          Showing {formatCount(entries.length)} of {formatCount(total)}.
        </Quiet>
      )}
    </>
  );
}

/** What a directory holds, read when it is inspected (the same read opening it makes). */
export function Contents({
  repository,
  path,
  onGo,
}: {
  repository: string;
  path: string;
  onGo: (path: string) => void;
}) {
  const children = useChildren(repository, path);
  if (children.isPending) {
    return (
      <p className={classes.loading}>
        <Loader size={12} type="dots" /> Reading {path || repository}
      </p>
    );
  }
  if (children.error) return <p className={classes.problem}>{children.error.message}</p>;
  return <EntryList entries={children.data.entries ?? []} total={children.data.entries_total} onGo={onGo} />;
}
