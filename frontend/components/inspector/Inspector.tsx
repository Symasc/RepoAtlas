"use client";

import { ActionIcon, CopyButton, Skeleton, Tooltip, UnstyledButton } from "@mantine/core";
import { ArrowLeftIcon, CheckIcon, CopyIcon } from "@phosphor-icons/react";

import { Inline } from "@/components/common/Inline";
import { PathText } from "@/components/common/PathText";
import { RepoSwatch } from "@/components/common/RepoSwatch";
import { State } from "@/components/shell/FreshnessIndicator";
import { absoluteTime, formatCount, plural, relativeTime, shortSha } from "@/lib/model/format";
import type { RepositoryModel } from "@/lib/model/repositories";

import classes from "./Inspector.module.css";

interface Props {
  repositories: RepositoryModel[] | undefined;
  loading: boolean;
  failed: boolean;
  selected: RepositoryModel | null;
  onSelect: (slug: string | null) => void;
}

export function Inspector({ repositories, loading, failed, selected, onSelect }: Props) {
  return (
    <div className={classes.scroll}>
      {selected !== null ? (
        <RepositoryDetails repository={selected} onBack={() => onSelect(null)} />
      ) : (
        <WorkspaceList
          repositories={repositories}
          loading={loading}
          failed={failed}
          onSelect={onSelect}
        />
      )}
    </div>
  );
}

function WorkspaceList({
  repositories,
  loading,
  failed,
  onSelect,
}: {
  repositories: RepositoryModel[] | undefined;
  loading: boolean;
  failed: boolean;
  onSelect: (slug: string) => void;
}) {
  return (
    <>
      <header className={classes.header}>
        <h2 className={classes.title}>Workspace</h2>
        <p className={classes.subtitle}>
          {repositories
            ? `${plural(repositories.length, "repository", "repositories")} indexed on this machine`
            : failed
              ? "Connect RepoAtlas to see what is indexed."
              : "Reading the index"}
        </p>
      </header>
      {loading && (
        <div className={classes.list} aria-hidden>
          {[0, 1, 2].map((i) => (
            <div key={i} className={classes.skeleton}>
              <Skeleton height={12} width="45%" radius="xs" />
              <Skeleton height={10} width="70%" radius="xs" mt={8} />
            </div>
          ))}
        </div>
      )}
      {repositories && (
        <ul className={classes.list} aria-label="Repositories">
          {repositories.map((r) => (
            <li key={r.slug}>
              <UnstyledButton
                className={classes.row}
                onClick={() => onSelect(r.slug)}
                style={{ "--hue": `var(--ra-repo-${r.hue})` } as React.CSSProperties}
              >
                <span className={classes.rowLine}>
                  <span className={classes.rowName}>{r.name}</span>
                  <State tone={r.freshness.tone} label={r.freshness.label} />
                </span>
                <span className={classes.rowMeta}>
                  <span className={classes.rowBranch}>
                    {r.branch ?? "detached"}
                    {r.headSha && <span className={classes.sha}>{shortSha(r.headSha)}</span>}
                  </span>
                  <span className={classes.rowCount}>{plural(r.files, "file")}</span>
                </span>
              </UnstyledButton>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

function RepositoryDetails({
  repository: r,
  onBack,
}: {
  repository: RepositoryModel;
  onBack: () => void;
}) {
  const manifests = r.manifests.slice(0, 6);
  return (
    <>
      <header className={classes.header}>
        <UnstyledButton className={classes.back} onClick={onBack}>
          <ArrowLeftIcon size={14} aria-hidden />
          Workspace
        </UnstyledButton>
        <h2 className={classes.title} data-with-swatch>
          <RepoSwatch hue={r.hue} size={12} />
          {r.name}
        </h2>
        <div className={classes.freshness}>
          <State tone={r.freshness.tone} label={r.freshness.label} />
          <p className={classes.explanation}>
            <Inline text={r.freshness.explanation} />
          </p>
        </div>
      </header>

      <dl className={classes.facts}>
        <Fact term="Branch">{r.branch ?? "Detached HEAD"}</Fact>
        {r.headSha && (
          <Fact term="Commit">
            <span className={classes.commit}>
              <code className={classes.mono}>{shortSha(r.headSha)}</code>
              <CopyButton value={r.headSha}>
                {({ copied, copy }) => (
                  <Tooltip label={copied ? "Copied" : "Copy full SHA"}>
                    <ActionIcon size="sm" onClick={copy} aria-label="Copy full commit SHA">
                      {copied ? <CheckIcon size={13} /> : <CopyIcon size={13} />}
                    </ActionIcon>
                  </Tooltip>
                )}
              </CopyButton>
            </span>
          </Fact>
        )}
        {r.lastIndexedAt && (
          <Fact term="Last indexed">
            <time dateTime={r.lastIndexedAt} title={absoluteTime(r.lastIndexedAt)}>
              {relativeTime(r.lastIndexedAt)}
            </time>
          </Fact>
        )}
        <Fact term="Files">{formatCount(r.files)}</Fact>
        <Fact term="Symbols">{formatCount(r.symbols)}</Fact>
        <Fact term="Relationships">{formatCount(r.relationships)}</Fact>
        {r.languages.length > 0 && (
          <Fact term="Languages">
            <span className={classes.tags}>
              {r.languages.map((language) => (
                <span key={language} className={classes.tag}>
                  {language}
                </span>
              ))}
            </span>
          </Fact>
        )}
        {r.manifests.length > 0 && (
          <Fact term={r.manifests.length === 1 ? "Manifest" : `Manifests (${r.manifests.length})`}>
            <ul className={classes.paths}>
              {manifests.map((path) => (
                <li key={path} className={classes.mono}>
                  <PathText path={path} />
                </li>
              ))}
              {r.manifests.length > manifests.length && (
                <li className={classes.more}>
                  and {plural(r.manifests.length - manifests.length, "more", "more")}
                </li>
              )}
            </ul>
          </Fact>
        )}
      </dl>
    </>
  );
}

function Fact({ term, children }: { term: string; children: React.ReactNode }) {
  return (
    <div className={classes.fact}>
      <dt>{term}</dt>
      <dd>{children}</dd>
    </div>
  );
}
