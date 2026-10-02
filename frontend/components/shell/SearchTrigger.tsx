"use client";

import { ActionIcon, Kbd, Loader, Switch, UnstyledButton } from "@mantine/core";
import { useDebouncedValue, useHotkeys, useOs } from "@mantine/hooks";
import { Spotlight, spotlight } from "@mantine/spotlight";
import { FileIcon, MagnifyingGlassIcon } from "@phosphor-icons/react";
import { useMemo, useState, type ReactNode } from "react";

import { RepoSwatch } from "@/components/common/RepoSwatch";
import { EntityBadge } from "@/components/graph/glyphs";
import { typeNoun } from "@/components/graph/traceNodes";
import { ApiError } from "@/lib/api/client";
import type { NameMatch } from "@/lib/api/generated";
import { FILE_QUERY_MIN, useFileSearch, useNameSearch } from "@/lib/api/topology";
import { plural } from "@/lib/model/format";
import type { RepositoryModel } from "@/lib/model/repositories";
import { chooseSearchNames, useSearchNames } from "@/lib/useSearchNames";

import classes from "./SearchTrigger.module.css";

function searchLabel(names: boolean): string {
  return names ? "Search repositories, files and variables" : "Search repositories and files";
}

/** The search entry point: repositories by name, files by path, and names when asked for. */
export function SearchTrigger() {
  const os = useOs();
  const mod = os === "macos" || os === "ios" ? "⌘" : "Ctrl";
  const label = searchLabel(useSearchNames());
  return (
    <>
      <UnstyledButton className={classes.trigger} onClick={spotlight.open} aria-keyshortcuts="Control+K Meta+K">
        <MagnifyingGlassIcon size={16} className={classes.icon} aria-hidden />
        <span className={classes.placeholder}>{label}</span>
        <span className={classes.keys} aria-hidden>
          <Kbd>{mod}</Kbd>
          <Kbd>K</Kbd>
        </span>
      </UnstyledButton>
      <ActionIcon className={classes.compact} onClick={spotlight.open} aria-label={label}>
        <MagnifyingGlassIcon size={18} />
      </ActionIcon>
    </>
  );
}

/**
 * "Include variables": whether search also finds the names code declares —
 * variables, functions, classes, methods — and the environment variables it
 * reads. Remembered in this browser.
 */
export function NamesToggle({ className }: { className?: string }) {
  const on = useSearchNames();
  return (
    <Switch
      className={className}
      classNames={{ label: classes.toggleLabel }}
      size="xs"
      label="Include variables"
      checked={on}
      onChange={(event) => chooseSearchNames(event.currentTarget.checked)}
    />
  );
}

/** Repositories shown before anything is typed, and while a query narrows them. */
const REPOSITORY_LIMIT = { idle: 8, typed: 4 };
/** Files listed, fewer when names share the list. */
const FILE_LIMIT = { alone: 12, shared: 6 };

function matchesRepository(repository: RepositoryModel, query: string): boolean {
  return [repository.name, repository.slug, repository.branch ?? ""].some((s) => s.toLowerCase().includes(query));
}

function folderOf(path: string): string {
  const slash = path.lastIndexOf("/");
  return slash === -1 ? "top level" : path.slice(0, slash);
}

/** What a name is and where it is written, for the line under it. */
function whereNamed(match: NameMatch, repositoryName: (slug: string) => string): string {
  const { entity } = match;
  const noun = typeNoun(entity.type);
  if (!entity.repository_slug || !entity.path) return `${noun} · read wherever it is used`;
  const line = entity.line_start != null ? `:${entity.line_start}` : "";
  return `${noun} · ${repositoryName(entity.repository_slug)} / ${entity.path}${line}`;
}

function unavailable(error: Error, what: string): string {
  return error instanceof ApiError && error.code === "UNKNOWN_ENDPOINT"
    ? `${what} cannot be searched yet: restart repoatlas serve to update it.`
    : `${what} could not be searched: ${error.message}`;
}

/**
 * Search across the workspace: repositories by name, filtered here, and files
 * by path, asked of the index once two letters are typed; with names included,
 * the variables and other names code declares too. A file opens where it lives,
 * its code first; a name opens on its trace.
 */
export function WorkspaceSpotlight({
  repositories,
  onRepository,
  onFile,
  onName,
}: {
  repositories: readonly RepositoryModel[];
  onRepository: (slug: string) => void;
  onFile: (slug: string, path: string) => void;
  onName: (match: NameMatch) => void;
}) {
  const [query, setQuery] = useState("");
  const [settled] = useDebouncedValue(query, 150);
  const withNames = useSearchNames();
  // ⌘K opens search from anywhere, a focused switch or the search box itself
  // included (opening it again does nothing); "/" is left to Spotlight, which
  // ignores it in a field, so a path can still be typed
  useHotkeys([["mod+K", () => spotlight.open()]], []);
  const typed = query.trim().toLowerCase();
  const asking = typed.length >= FILE_QUERY_MIN;
  const files = useFileSearch(asking ? settled : "", withNames ? FILE_LIMIT.shared : FILE_LIMIT.alone);
  const nameSearch = useNameSearch(asking && withNames ? settled : "");
  const bySlug = useMemo(() => new Map(repositories.map((r) => [r.slug, r])), [repositories]);
  const repositoryName = (slug: string) => bySlug.get(slug)?.name ?? slug;

  const repos = repositories
    .filter((r) => typed === "" || matchesRepository(r, typed))
    .slice(0, typed === "" ? REPOSITORY_LIMIT.idle : REPOSITORY_LIMIT.typed);
  // a cleared query keeps the last answer as a placeholder: never show it
  const found = asking ? (files.data?.files ?? []) : [];
  const names = asking && withNames ? (nameSearch.data?.names ?? []) : [];
  const behind = settled.trim().toLowerCase() !== typed;
  const waitingFiles = asking && (files.isFetching || behind) && !files.error;
  const waitingNames = asking && withNames && (nameSearch.isFetching || behind) && !nameSearch.error;
  const failed = asking ? (files.error ?? (withNames ? nameSearch.error : null)) : null;
  const what = withNames ? "Files and variables" : "Files";

  let status: ReactNode = null;
  if (typed.length > 0 && !asking) {
    status = `${what} are searched from two letters.`;
  } else if (failed) {
    status = unavailable(failed, files.error ? "Files" : "Variables");
  } else if ((waitingFiles || waitingNames) && found.length === 0 && names.length === 0) {
    status = (
      <>
        <Loader size={12} type="dots" /> Searching {what.toLowerCase()}
      </>
    );
  } else {
    const cut: string[] = [];
    if (asking && files.data?.truncated) cut.push(`${found.length} of ${files.data.total ?? 0} files`);
    if (asking && withNames && nameSearch.data?.truncated) cut.push(`${names.length} of ${nameSearch.data.total ?? 0} names`);
    if (cut.length > 0) {
      status = `${cut.join(" and ")} shown. Add to the name to narrow it.`;
    } else if (asking && files.isSuccess && found.length === 0 && names.length === 0 && repos.length > 0) {
      status = withNames ? "No file path or name holds that." : "No file path contains that.";
    }
  }
  const nothing = repos.length === 0 && found.length === 0 && names.length === 0 && !waitingFiles && !waitingNames && !failed;
  const firstRepository = repos[0];
  const firstFile = found[0];
  const firstName = names[0];
  const first = firstRepository
    ? () => onRepository(firstRepository.slug)
    : firstFile
      ? () => onFile(firstFile.repository, firstFile.path)
      : firstName
        ? () => onName(firstName)
        : null;
  const label = searchLabel(withNames);

  return (
    <Spotlight.Root
      query={query}
      onQueryChange={setQuery}
      shortcut="/"
      radius="lg"
      shadow="md"
      // a plain dim behind the search, no frosted glass
      overlayProps={{ backgroundOpacity: 0.45, blur: 0 }}
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
          // results found after the last keystroke arrive unselected: Enter takes the first
          if (event.key !== "Enter" || first === null) return;
          if (event.currentTarget.closest('[role="dialog"]')?.querySelector("[data-selected]")) return;
          first();
          spotlight.close();
        }}
        leftSection={<MagnifyingGlassIcon size={18} />}
        rightSection={<NamesToggle className={classes.inlineToggle} />}
        rightSectionWidth={164}
        rightSectionPointerEvents="all"
        placeholder={label}
        aria-label={label}
      />
      {(repos.length > 0 || found.length > 0 || names.length > 0) && (
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
                  description={`${repositoryName(f.repository)} · ${folderOf(f.path)}`}
                  leftSection={<FileIcon size={16} />}
                  highlightQuery
                  onClick={() => onFile(f.repository, f.path)}
                />
              ))}
            </Spotlight.ActionsGroup>
          )}
          {names.length > 0 && (
            <Spotlight.ActionsGroup label="Variables and functions">
              {names.map((n) => (
                <Spotlight.Action
                  key={n.entity.key}
                  label={n.qualified_name}
                  description={whereNamed(n, repositoryName)}
                  leftSection={<EntityBadge entityType={n.entity.type} size={16} />}
                  highlightQuery
                  onClick={() => onName(n)}
                />
              ))}
            </Spotlight.ActionsGroup>
          )}
        </Spotlight.ActionsList>
      )}
      {nothing && (
        <Spotlight.Empty>
          {withNames ? "No repository, file or name matches that." : "No repository or file matches that."}
        </Spotlight.Empty>
      )}
      {status !== null && !nothing && <Spotlight.Footer role="status">{status}</Spotlight.Footer>}
    </Spotlight.Root>
  );
}
