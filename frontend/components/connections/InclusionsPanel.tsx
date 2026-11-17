"use client";

import { Button, Drawer, Loader, Select, Tabs } from "@mantine/core";
import { useState } from "react";

import type { InclusionDeclaration, UnsettledInclusion } from "@/lib/api/generated";
import { useInclusionDeclarations, useInclusionDeclarationWrites } from "@/lib/api/inclusions";
import { entryKey, unsettledRowKey, unsettledSummary } from "@/lib/model/inclusions";
import type { RepositoryModel } from "@/lib/model/repositories";

import classes from "./Connections.module.css";

const FORMAT: Record<string, string> = {
  gitmodules: "Git submodule",
  "git-subrepo": "git-subrepo",
  vcstool: "vcstool .repos",
  west: "west manifest",
  "repo-manifest": "repo manifest",
};

const STATUS: Record<string, { label: string; tone?: string }> = {
  applied: { label: "applied", tone: "success" },
  superseded: { label: "superseded: the index now finds it by itself" },
  stale: { label: "stale: the entry or the repository is no longer indexed", tone: "warning" },
};

/**
 * Inclusions (submodules, manifest projects) the index could not link to one indexed
 * repository, and the declarations that settle them. A declaration is a fallback: the next
 * sync applies it, and a checkout path or remote the index proves always wins over it.
 */
export function InclusionsPanel({
  opened,
  onClose,
  unsettled,
  total,
  repositories,
}: {
  opened: boolean;
  onClose: () => void;
  unsettled: readonly UnsettledInclusion[];
  total: number;
  repositories: readonly RepositoryModel[];
}) {
  const declarations = useInclusionDeclarations(opened);
  const listed = declarations.data?.declarations ?? [];
  const byEntry = new Map(listed.map((d) => [entryKey(d), d]));
  const pending = listed.filter((d) => d.status == null).length;
  const summary = unsettledSummary(unsettled.length, total);
  return (
    <Drawer
      opened={opened}
      onClose={onClose}
      position="right"
      size="min(760px, 100vw)"
      title="Unlinked submodules"
      closeButtonProps={{ "aria-label": "Close unlinked submodules" }}
    >
      <div className={classes.panel}>
        <p className={classes.basis}>
          Repositories your repositories include (Git submodules, subrepos and workspace manifests) that RepoAtlas
          could not match to exactly one indexed repository, by checkout path or by remote. Say which one it is and
          the next sync links it, marked <em>declared</em>. A declaration never overrides a path or remote the index
          proves.
        </p>
        {pending > 0 && (
          <p className={classes.banner} data-tone="warning" role="status">
            {pending} declaration{pending === 1 ? " is" : "s are"} pending: it takes effect at the next sync.
          </p>
        )}
        <Tabs defaultValue="unsettled" keepMounted={false}>
          <Tabs.List aria-label="Submodule views">
            <Tabs.Tab value="unsettled">Not linked ({total})</Tabs.Tab>
            <Tabs.Tab value="declarations">Your declarations ({listed.length})</Tabs.Tab>
          </Tabs.List>
          <Tabs.Panel value="unsettled" pt="md">
            {unsettled.length > 0 && (
              <ul className={classes.declarations} aria-label="Unlinked inclusions">
                {unsettled.map((entry) => {
                  const declared = byEntry.get(entryKey(entry)) ?? null;
                  return (
                    <UnsettledRow
                      key={unsettledRowKey(entry, declared)}
                      entry={entry}
                      declared={declared}
                      repositories={repositories}
                    />
                  );
                })}
              </ul>
            )}
            {summary && <p className={classes.detail}>{summary}</p>}
          </Tabs.Panel>
          <Tabs.Panel value="declarations" pt="md">
            {declarations.isLoading && (
              <p className={classes.detail}>
                <Loader size={12} type="dots" /> Reading your declarations
              </p>
            )}
            {declarations.error && (
              <p className={classes.banner} data-tone="danger" role="alert">
                {declarations.error.message}
              </p>
            )}
            {declarations.data && listed.length === 0 && (
              <p className={classes.detail}>You have declared no inclusion.</p>
            )}
            {listed.length > 0 && (
              <ul className={classes.declarations} aria-label="Your inclusion declarations">
                {listed.map((d) => (
                  <DeclarationRow key={d.id} declaration={d} />
                ))}
              </ul>
            )}
          </Tabs.Panel>
        </Tabs>
      </div>
    </Drawer>
  );
}

function UnsettledRow({
  entry,
  declared,
  repositories,
}: {
  entry: UnsettledInclusion;
  declared: InclusionDeclaration | null;
  repositories: readonly RepositoryModel[];
}) {
  const { create } = useInclusionDeclarationWrites();
  const candidates = entry.candidates ?? [];
  // an ambiguous entry is one of the clones its URL names; an unresolved one may be any repository
  const options = (candidates.length > 0 ? candidates : repositories.map((r) => r.slug)).filter(
    (slug) => slug !== entry.repository,
  );
  const [target, setTarget] = useState<string | null>(declared?.target ?? null);
  const where = `${entry.repository}/${entry.file}${entry.line ? `:${entry.line}` : ""}`;
  return (
    <li className={classes.declaration}>
      <div className={classes.groupHead}>
        <p className={classes.origin}>{entry.path}</p>
        <span className={classes.tag}>{FORMAT[entry.format] ?? entry.format}</span>
        <span className={classes.tag} data-tone="warning">
          {entry.decision === "ambiguous" ? `${candidates.length} clones match` : "no indexed match"}
        </span>
      </div>
      <p className={classes.detail}>
        <span className={classes.mono}>{where}</span>
        {entry.url && (
          <>
            <br />
            <span className={classes.mono}>{entry.url}</span>
          </>
        )}
      </p>
      {entry.declared && (
        <p className={classes.detail}>
          Your declaration names <span className={classes.mono}>{entry.declared}</span>, which is no longer indexed.
        </p>
      )}
      <div className={classes.filters}>
        <Select
          label="This is"
          placeholder="Choose a repository"
          searchable
          data={options}
          value={target}
          onChange={setTarget}
          aria-label={`Repository included at ${entry.path}`}
        />
        <Button
          size="sm"
          disabled={target === null || target === declared?.target}
          loading={create.isPending}
          onClick={() =>
            target && create.mutate({ repository: entry.repository, file: entry.file, path: entry.path, target })
          }
        >
          Link to repository
        </Button>
      </div>
      {declared && declared.target === target && (
        <p className={classes.detail}>Declared: it is linked at the next sync.</p>
      )}
      {create.error && (
        <p className={classes.banner} data-tone="danger" role="alert">
          {create.error.message}
        </p>
      )}
    </li>
  );
}

function DeclarationRow({ declaration: d }: { declaration: InclusionDeclaration }) {
  const { remove } = useInclusionDeclarationWrites();
  const status = d.status ? STATUS[d.status] : { label: "pending: applied at the next sync", tone: "warning" };
  return (
    <li className={classes.declaration}>
      <div className={classes.groupHead}>
        <p className={classes.origin}>
          {d.path} → {d.target}
        </p>
        <span className={classes.tag} data-tone={status?.tone}>
          {status?.label}
        </span>
      </div>
      <p className={classes.detail}>
        <span className={classes.mono}>
          {d.repository}/{d.file}
        </span>
      </p>
      <div className={classes.actions}>
        <Button size="xs" variant="default" loading={remove.isPending} onClick={() => remove.mutate(d.id)}>
          Remove
        </Button>
      </div>
      {remove.error && (
        <p className={classes.banner} data-tone="danger" role="alert">
          {remove.error.message}
        </p>
      )}
    </li>
  );
}
