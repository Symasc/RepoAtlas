"use client";

import { UnstyledButton } from "@mantine/core";

import { childOf, directoryId, FILES_ID, repositoryId } from "@/lib/model/graph";
import type { RepositoryModel } from "@/lib/model/repositories";

import type { Navigation } from "./navigation";
import classes from "./Views.module.css";

/**
 * Where you are, every step a way back out. Climbing out lands on the step you
 * came from, selected, so the view you return to says where you were.
 */
export function Trail({ repository, nav, file }: { repository: RepositoryModel; nav: Navigation; file: boolean }) {
  const path = nav.place.path;
  const segments = path ? path.split("/") : [];
  const out = (at: string | null) => {
    if (at === null) {
      nav.go({ repository: null, path: "" }, { select: repositoryId(repository.slug) });
      return;
    }
    const child = childOf(at, path);
    // the child is a card there: a directory's own, or the files card for a file
    const last = child === path && file;
    nav.go({ repository: repository.slug, path: at }, { select: child === null ? null : last ? FILES_ID : directoryId(child) });
  };
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
