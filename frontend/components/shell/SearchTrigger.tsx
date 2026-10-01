"use client";

import { ActionIcon, Kbd, Loader, UnstyledButton } from "@mantine/core";
import { useDebouncedValue, useOs } from "@mantine/hooks";
import { Spotlight, spotlight } from "@mantine/spotlight";
import { FileIcon, MagnifyingGlassIcon } from "@phosphor-icons/react";
import { useMemo, useState, type ReactNode } from "react";

import { RepoSwatch } from "@/components/common/RepoSwatch";
import { ApiError } from "@/lib/api/client";
import { FILE_QUERY_MIN, useFileSearch } from "@/lib/api/topology";
import { plural } from "@/lib/model/format";
import type { RepositoryModel } from "@/lib/model/repositories";

import classes from "./SearchTrigger.module.css";

/**
 * The search entry point: repositories by name and files by path. Symbols,
 * routes and packages join it when search covers them.
 */
export function SearchTrigger() {
  const os = useOs();
  const mod = os === "macos" || os === "ios" ? "⌘" : "Ctrl";
  return (
    <>
      <UnstyledButton className={classes.trigger} onClick={spotlight.open} aria-keyshortcuts="Control+K Meta+K">
        <MagnifyingGlassIcon size={16} className={classes.icon} aria-hidden />
        <span className={classes.placeholder}>Search repositories and files</span>
        <span className={classes.keys} aria-hidden>
          <Kbd>{mod}</Kbd>
          <Kbd>K</Kbd>
        </span>
      </UnstyledButton>
      <ActionIcon className={classes.compact} onClick={spotlight.open} aria-label="Search repositories and files">
        <MagnifyingGlassIcon size={18} />
      </ActionIcon>
    </>
  );
}

/** Repositories shown before anything is typed, and while a query narrows them. */
const REPOSITORY_LIMIT = { idle: 8, typed: 4 };

function matchesRepository(repository: RepositoryModel, query: string): boolean {
  return [repository.name, repository.slug, repository.branch ?? ""].some((s) => s.toLowerCase().includes(query));
}

function folderOf(path: string): string {
  const slash = path.lastIndexOf("/");
  return slash === -1 ? "top level" : path.slice(0, slash);
}

/**
 * Search across the workspace: repositories by name, filtered here, and files
 * by path, asked of the index once two letters are typed. A file opens where
 * it lives; its code is the first thing shown.
 */
export function WorkspaceSpotlight({
  repositories,
  onRepository,
  onFile,
}: {
  repositories: readonly RepositoryModel[];
  onRepository: (slug: string) => void;
  onFile: (slug: string, path: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [settled] = useDebouncedValue(query, 150);
  const typed = query.trim().toLowerCase();
  const asking = typed.length >= FILE_QUERY_MIN;
  const files = useFileSearch(asking ? settled : "");
  const bySlug = useMemo(() => new Map(repositories.map((r) => [r.slug, r])), [repositories]);

  const repos = repositories
    .filter((r) => typed === "" || matchesRepository(r, typed))
    .slice(0, typed === "" ? REPOSITORY_LIMIT.idle : REPOSITORY_LIMIT.typed);
  // a cleared query keeps the last answer as a placeholder: never show it
  const found = asking ? (files.data?.files ?? []) : [];
  const waiting = asking && (files.isFetching || settled.trim().toLowerCase() !== typed) && !files.error;

  let status: ReactNode = null;
  if (typed.length > 0 && !asking) {
    status = "Files are searched from two letters.";
  } else if (asking && files.error) {
    status =
      files.error instanceof ApiError && files.error.code === "UNKNOWN_ENDPOINT"
        ? "Files cannot be searched yet: restart repoatlas serve to update it."
        : `Files could not be searched: ${files.error.message}`;
  } else if (waiting && found.length === 0) {
    status = (
      <>
        <Loader size={12} type="dots" /> Searching files
      </>
    );
  } else if (asking && files.data?.truncated) {
    status = `${files.data.files?.length ?? 0} of ${files.data.total ?? 0} files shown. Add to the name to narrow it.`;
  } else if (asking && files.isSuccess && found.length === 0 && repos.length > 0) {
    status = "No file path contains that.";
  }
  const nothing = repos.length === 0 && found.length === 0 && !waiting && !files.error;
  const firstRepository = repos[0];
  const firstFile = found[0];
  const first = firstRepository
    ? () => onRepository(firstRepository.slug)
    : firstFile
      ? () => onFile(firstFile.repository, firstFile.path)
      : null;

  return (
    <Spotlight.Root
      query={query}
      onQueryChange={setQuery}
      shortcut={["mod + K", "/"]}
      radius="md"
      scrollable
      maxHeight="min(560px, 70vh)"
      classNames={{
        content: classes.content,
        search: classes.search,
        action: classes.action,
        actionDescription: classes.actionDescription,
        actionsGroup: classes.group,
        empty: classes.empty,
        footer: classes.footer,
      }}
    >
      <Spotlight.Search
        onKeyDown={(event) => {
          // files found after the last keystroke arrive unselected: Enter takes the first
          if (event.key !== "Enter" || first === null) return;
          if (event.currentTarget.closest('[role="dialog"]')?.querySelector("[data-selected]")) return;
          first();
          spotlight.close();
        }}
        leftSection={<MagnifyingGlassIcon size={18} />}
        placeholder="Search repositories and files"
        aria-label="Search repositories and files"
      />
      {(repos.length > 0 || found.length > 0) && (
        <Spotlight.ActionsList>
          {repos.length > 0 && (
            <Spotlight.ActionsGroup label="Repositories">
              {repos.map((r) => (
                <Spotlight.Action
                  key={r.slug}
                  label={r.name}
                  description={r.branch ? `${plural(r.files, "file")} on ${r.branch}` : plural(r.files, "file")}
                  leftSection={<RepoSwatch hue={r.hue} />}
                  highlightQuery
                  onClick={() => onRepository(r.slug)}
                />
              ))}
            </Spotlight.ActionsGroup>
          )}
          {found.length > 0 && (
            <Spotlight.ActionsGroup label="Files">
              {found.map((f) => (
                <Spotlight.Action
                  key={`${f.repository}/${f.path}`}
                  label={f.name}
                  description={`${bySlug.get(f.repository)?.name ?? f.repository} · ${folderOf(f.path)}`}
                  leftSection={<FileIcon size={18} />}
                  highlightQuery
                  onClick={() => onFile(f.repository, f.path)}
                />
              ))}
            </Spotlight.ActionsGroup>
          )}
        </Spotlight.ActionsList>
      )}
      {nothing && <Spotlight.Empty>No repository or file matches that.</Spotlight.Empty>}
      {status !== null && !nothing && (
        <Spotlight.Footer role="status">{status}</Spotlight.Footer>
      )}
    </Spotlight.Root>
  );
}
