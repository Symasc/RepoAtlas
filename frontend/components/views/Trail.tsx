"use client";

import { UnstyledButton } from "@mantine/core";
import { useCallback } from "react";

import { childOf, directoryId, FILES_ID, repositoryId } from "@/lib/model/graph";
import type { RepositoryModel } from "@/lib/model/repositories";

import type { Navigation } from "./navigation";
import classes from "./Views.module.css";

/**
 * Out to one step of where the reader is: `null` for every repository, `""` for
 * this one's root, else a directory on the way here. It lands on the step the
 * reader came from, selected, so the view returned to says where they were. A
 * path names a file when `file` is set: its own step is then the files card.
 */
export function useClimb(repository: RepositoryModel, nav: Navigation, file: boolean): (at: string | null) => void {
  return useCallback(
    (at: string | null) => {
      if (at === null) {
        nav.go({ repository: null, path: "" }, { select: repositoryId(repository.slug) });
        return;
      }
      const path = nav.place.path;
      const child = childOf(at, path);
      // the child is a card there: a directory's own, or the files card for a file
      const last = child === path && file;
      nav.go({ repository: repository.slug, path: at }, { select: child === null ? null : last ? FILES_ID : directoryId(child) });
    },
    [repository.slug, nav, file],
  );
}

/** Where you are, every step a way back out (see useClimb). */
export function Trail({ repository, nav, file }: { repository: RepositoryModel; nav: Navigation; file: boolean }) {
  const path = nav.place.path;
  const segments = path ? path.split("/") : [];
  const out = useClimb(repository, nav, file);
  return (
    <nav className={classes.trail} aria-label="Location">
      <UnstyledButton className={classes.crumb} onClick={() => out(null)}>
        All repositories
      </UnstyledButton>
      <span className={classes.separator} aria-hidden>
        /
      </span>
      {segments.length === 0 ? (
        <span className={classes.crumb} data-current aria-current="page">
          {repository.name}
        </span>
      ) : (
        <UnstyledButton className={classes.crumb} onClick={() => out("")}>
          {repository.name}
        </UnstyledButton>
      )}
      {segments.map((segment, i) => {
        const at = segments.slice(0, i + 1).join("/");
        const last = i === segments.length - 1;
        return (
          <span key={at} className={classes.step}>
            <span className={classes.separator} aria-hidden>
              /
            </span>
            {last ? (
              <span className={classes.crumb} data-current aria-current="page">
                {segment}
              </span>
            ) : (
              <UnstyledButton className={classes.crumb} onClick={() => out(at)}>
                {segment}
              </UnstyledButton>
            )}
          </span>
        );
      })}
    </nav>
  );
}
