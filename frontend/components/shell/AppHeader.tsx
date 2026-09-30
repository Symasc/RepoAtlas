"use client";

import { ActionIcon, Tooltip, UnstyledButton } from "@mantine/core";
import { SidebarSimpleIcon } from "@phosphor-icons/react";
import Link from "next/link";

import type { RepositoryModel } from "@/lib/model/repositories";

import classes from "./AppHeader.module.css";
import { FreshnessIndicator, type LiveCheck } from "./FreshnessIndicator";
import { Logo } from "./Logo";
import { SearchTrigger } from "./SearchTrigger";
import { ThemeToggle } from "./ThemeToggle";

interface Props {
  repositories: RepositoryModel[] | undefined;
  selected: RepositoryModel | null;
  onSelect: (slug: string | null) => void;
  freshness: LiveCheck;
  offline: boolean;
  inspectorOpened: boolean;
  onToggleInspector: () => void;
}

export function AppHeader(props: Props) {
  const { selected, onSelect } = props;
  return (
    <div className={classes.bar}>
      <div className={classes.start}>
        <Link className={classes.brand} href="/" aria-label="RepoAtlas">
          <Logo />
          <span className={classes.wordmark}>RepoAtlas</span>
        </Link>
        <nav className={classes.trail} aria-label="Location">
          <UnstyledButton
            className={classes.crumb}
            data-current={selected === null || undefined}
            aria-current={selected === null ? "page" : undefined}
            onClick={() => onSelect(null)}
          >
            Workspace
          </UnstyledButton>
          {selected !== null && (
            <>
              <span className={classes.separator} aria-hidden="true">
                /
              </span>
              <span className={classes.crumb} data-current aria-current="page">
                {selected.name}
              </span>
            </>
          )}
        </nav>
      </div>

      <div className={classes.center}>
        <SearchTrigger />
      </div>

      <div className={classes.end}>
        <FreshnessIndicator
          repositories={props.repositories}
          check={props.freshness}
          offline={props.offline}
        />
        <Tooltip label={props.inspectorOpened ? "Hide details" : "Show details"}>
          <ActionIcon
            className={classes.inspectorToggle}
            onClick={props.onToggleInspector}
            aria-label={props.inspectorOpened ? "Hide details" : "Show details"}
            aria-pressed={props.inspectorOpened}
          >
            <SidebarSimpleIcon size={18} mirrored />
          </ActionIcon>
        </Tooltip>
        <ThemeToggle />
      </div>
    </div>
  );
}
