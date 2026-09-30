"use client";

import Link from "next/link";

import type { RepositoryModel } from "@/lib/model/repositories";

import classes from "./AppHeader.module.css";
import { FreshnessIndicator, type LiveCheck } from "./FreshnessIndicator";
import { Logo } from "./Logo";
import { SearchTrigger } from "./SearchTrigger";
import { ThemeToggle } from "./ThemeToggle";

interface Props {
  repositories: RepositoryModel[] | undefined;
  freshness: LiveCheck;
  offline: boolean;
}

export function AppHeader({ repositories, freshness, offline }: Props) {
  return (
    <div className={classes.bar}>
      <div className={classes.start}>
        <Link className={classes.brand} href="/" aria-label="RepoAtlas, all repositories">
          <Logo />
          <span className={classes.wordmark}>RepoAtlas</span>
        </Link>
        <span className={classes.title}>Graph Explorer</span>
      </div>

      <div className={classes.center}>
        <SearchTrigger />
      </div>

      <div className={classes.end}>
        <FreshnessIndicator repositories={repositories} check={freshness} offline={offline} />
        <ThemeToggle />
      </div>
    </div>
  );
}
