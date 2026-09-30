"use client";

import { Inline } from "@/components/common/Inline";
import { ApiState } from "@/components/states/ApiState";
import type { ApiError } from "@/lib/api/client";
import { plural } from "@/lib/model/format";
import { totals, type RepositoryModel } from "@/lib/model/repositories";

import classes from "./Canvas.module.css";

interface Props {
  repositories: RepositoryModel[] | undefined;
  loading: boolean;
  error: ApiError | null;
  retrying: boolean;
  onRetry: () => void;
}

/**
 * The map's surface. P1 draws only the survey grid and the legend; the
 * repository graph arrives in P2 and replaces the note in the middle.
 */
export function Canvas({ repositories, loading, error, retrying, onRetry }: Props) {
  const sum = repositories ? totals(repositories) : null;
  return (
    <section className={classes.canvas} aria-label="Workspace map" aria-busy={loading}>
      {error !== null ? (
        <div className={classes.center}>
          <ApiState error={error} retrying={retrying} onRetry={onRetry} />
        </div>
      ) : (
        sum !== null && (
          <>
            <p className={classes.pending}>
              <Inline
                text={
                  sum.repositories === 0
                    ? "Nothing is indexed yet. Index your repositories with `repoatlas sync`."
                    : "The map of these repositories is drawn here."
                }
              />
            </p>
            <dl className={classes.legend} aria-label="Workspace totals">
              <Figure value={sum.repositories} one="repository" many="repositories" />
              <Figure value={sum.files} one="file" />
              <Figure value={sum.symbols} one="symbol" />
              <Figure value={sum.relationships} one="relationship" />
            </dl>
          </>
        )
      )}
    </section>
  );
}

function Figure({ value, one, many }: { value: number; one: string; many?: string }) {
  const [count, ...noun] = plural(value, one, many).split(" ");
  return (
    <div className={classes.figure}>
      <dt className={classes.noun}>{noun.join(" ")}</dt>
      <dd className={classes.count}>{count}</dd>
    </div>
  );
}
