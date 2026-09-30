"use client";

import { ActionIcon, Kbd, UnstyledButton } from "@mantine/core";
import { useOs } from "@mantine/hooks";
import { Spotlight, spotlight, type SpotlightActionData } from "@mantine/spotlight";
import { MagnifyingGlassIcon } from "@phosphor-icons/react";
import { useMemo } from "react";

import { RepoSwatch } from "@/components/common/RepoSwatch";
import { plural } from "@/lib/model/format";
import type { RepositoryModel } from "@/lib/model/repositories";

import classes from "./SearchTrigger.module.css";

/**
 * The search entry point. For now it finds repositories by name, filtered in
 * the browser; files, symbols, routes and packages join it when search is built.
 */
export function SearchTrigger() {
  const os = useOs();
  const mod = os === "macos" || os === "ios" ? "⌘" : "Ctrl";
  return (
    <>
      <UnstyledButton className={classes.trigger} onClick={spotlight.open} aria-keyshortcuts="Control+K Meta+K">
        <MagnifyingGlassIcon size={16} className={classes.icon} aria-hidden />
        <span className={classes.placeholder}>Search repositories</span>
        <span className={classes.keys} aria-hidden>
          <Kbd>{mod}</Kbd>
          <Kbd>K</Kbd>
        </span>
      </UnstyledButton>
      <ActionIcon className={classes.compact} onClick={spotlight.open} aria-label="Search repositories">
        <MagnifyingGlassIcon size={18} />
      </ActionIcon>
    </>
  );
}

export function RepositorySpotlight({
  repositories,
  onSelect,
}: {
  repositories: readonly RepositoryModel[];
  onSelect: (slug: string) => void;
}) {
  const actions = useMemo<SpotlightActionData[]>(
    () =>
      repositories.map((r) => ({
        id: r.slug,
        label: r.name,
        description: r.branch
          ? `${plural(r.files, "file")} on ${r.branch}`
          : plural(r.files, "file"),
        keywords: [r.slug, ...(r.branch ? [r.branch] : [])],
        leftSection: <RepoSwatch hue={r.hue} />,
        onClick: () => onSelect(r.slug),
      })),
    [repositories, onSelect],
  );
  return (
    <Spotlight
      actions={actions}
      shortcut={["mod + K", "/"]}
      limit={8}
      highlightQuery
      nothingFound="No repository has that name."
      radius="md"
      classNames={{
        content: classes.content,
        search: classes.search,
        action: classes.action,
        actionDescription: classes.actionDescription,
        empty: classes.empty,
      }}
      searchProps={{
        leftSection: <MagnifyingGlassIcon size={18} />,
        placeholder: "Search repositories",
        "aria-label": "Search repositories",
      }}
    />
  );
}
