"use client";

import { Button, Popover, UnstyledButton } from "@mantine/core";
import { ArrowClockwiseIcon } from "@phosphor-icons/react";

import { Inline } from "@/components/common/Inline";
import { RepoSwatch } from "@/components/common/RepoSwatch";
import { absoluteTime, plural, relativeTime } from "@/lib/model/format";
import { summarize, type FreshnessModel, type FreshnessTone } from "@/lib/model/freshness";
import type { RepositoryModel } from "@/lib/model/repositories";

import classes from "./FreshnessIndicator.module.css";

export interface LiveCheck {
  /** The live check has answered at least once. */
  live: boolean;
  checking: boolean;
  failed: boolean;
  checkedAt: number;
  recheck: () => void;
}

interface Props {
  repositories: RepositoryModel[] | undefined;
  check: LiveCheck;
  offline: boolean;
}

/**
 * The index's freshness as one strip: a segment per repository, as wide as its
 * share of indexed files, coloured by its own state. The worst state sets the
 * headline, so one stale repository is never averaged away by current ones.
 */
export function FreshnessIndicator({ repositories, check, offline }: Props) {
  if (offline) {
    return (
      <span className={classes.trigger} data-static>
        <span className={classes.strip} aria-hidden>
          <span className={classes.segment} data-tone="unknown" style={{ flexGrow: 1 }} />
        </span>
        <span className={classes.headline}>Not connected</span>
      </span>
    );
  }
  if (repositories === undefined) {
    return (
      <span className={classes.trigger} data-static aria-busy="true">
        <span className={classes.strip} data-loading aria-hidden />
        <span className={classes.headline}>Loading index</span>
      </span>
    );
  }

  const models = repositories.map((r) => r.freshness);
  const summary = summarize(models);
  const files = repositories.reduce((sum, r) => sum + r.files, 0);
  const pending = !check.live;
  const headline = pending ? (check.failed ? "Not checked" : "Checking") : summary.headline;

  return (
    <Popover position="bottom-end" width={360} trapFocus returnFocus>
      <Popover.Target>
        <UnstyledButton
          className={classes.trigger}
          aria-label={`Index freshness: ${headline}`}
          data-tone={pending ? "unverified" : summary.tone}
        >
          <span className={classes.strip} data-dense={repositories.length > 12 || undefined} aria-hidden>
            {repositories.map((r) => (
              <span
                key={r.slug}
                className={classes.segment}
                data-tone={r.freshness.tone}
                style={{ flexGrow: Math.max(r.files, files * 0.04) }}
              />
            ))}
          </span>
          <span className={classes.headline}>{headline}</span>
        </UnstyledButton>
      </Popover.Target>
      <Popover.Dropdown aria-label="Index freshness">
        <div className={classes.head}>
          <div>
            <h2 className={classes.title}>Index freshness</h2>
            <p className={classes.note}>
              {check.live
                ? `Checked against git and the files on disk ${relativeTime(new Date(check.checkedAt).toISOString())}.`
                : check.failed
                  ? "The live check failed. Showing the state stored with the index."
                  : "Showing the state stored with the index while git is checked."}
            </p>
          </div>
          <Button
            className={classes.recheck}
            variant="default"
            size="compact-sm"
            leftSection={<ArrowClockwiseIcon size={14} />}
            loading={check.checking}
            onClick={check.recheck}
          >
            Check again
          </Button>
        </div>
        <ul className={classes.list}>
          {repositories.map((r) => (
            <li key={r.slug} className={classes.row}>
              <RepoSwatch hue={r.hue} />
              <div className={classes.body}>
                <div className={classes.line}>
                  <span className={classes.name}>{r.name}</span>
                  <State tone={r.freshness.tone} label={r.freshness.label} />
                </div>
                <p className={classes.explanation}>
                  <Inline text={r.freshness.explanation} />
                </p>
                <When freshness={r.freshness} indexedAt={r.lastIndexedAt} />
              </div>
            </li>
          ))}
        </ul>
      </Popover.Dropdown>
    </Popover>
  );
}

/** When a repository was indexed, and what of it an older build wrote, as the index records it. */
function When({ freshness, indexedAt }: { freshness: FreshnessModel; indexedAt: string | null }) {
  const at = freshness.indexedAt ?? indexedAt;
  const parts: string[] = [];
  if (freshness.outdatedFiles > 0) parts.push(`${plural(freshness.outdatedFiles, "file")} indexed by an older build`);
  if (at === null && parts.length === 0) return null;
  return (
    <p className={classes.when}>
      {at && <time dateTime={at} title={absoluteTime(at)}>Indexed {relativeTime(at)}</time>}
      {parts.map((part) => (
        <span key={part}>{part}</span>
      ))}
    </p>
  );
}

export function State({ tone, label }: { tone: FreshnessTone; label: string }) {
  return (
    <span className={classes.state} data-tone={tone}>
      <span className={classes.dot} aria-hidden />
      {label}
    </span>
  );
}
