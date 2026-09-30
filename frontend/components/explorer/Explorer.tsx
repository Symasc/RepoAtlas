"use client";

import { AppShell, Button, Loader } from "@mantine/core";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useMemo } from "react";

import { AppHeader } from "@/components/shell/AppHeader";
import { RepositorySpotlight } from "@/components/shell/SearchTrigger";
import { ApiState } from "@/components/states/ApiState";
import { OverviewLevel } from "@/components/views/OverviewLevel";
import { RepositoryLevel } from "@/components/views/RepositoryLevel";
import { ApiError } from "@/lib/api/client";
import { useLiveFreshness, useRepositories } from "@/lib/api/queries";
import type { RepositoryModel } from "@/lib/model/repositories";

import classes from "./Explorer.module.css";

/**
 * The one explorer screen. Where you are lives in the URL, `?repo=<slug>&path=<dir>`,
 * so a view can be shared and Back climbs out: no repository is the overview,
 * a repository is its root, a path is a directory inside it.
 */
export function Explorer() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();

  const repositories = useRepositories();
  const live = useLiveFreshness();

  // stored freshness renders at once; the live check replaces it when it lands
  const merged = useMemo<RepositoryModel[] | undefined>(
    () =>
      repositories.data?.map((r) => ({
        ...r,
        freshness: live.data?.get(r.slug) ?? r.freshness,
      })),
    [repositories.data, live.data],
  );

  const slug = params.get("repo");
  const path = params.get("path") ?? "";
  const selected = slug === null ? null : (merged?.find((r) => r.slug === slug) ?? null);

  const navigate = useCallback(
    (next: string | null, nextPath = "") => {
      const search = new URLSearchParams();
      if (next !== null) search.set("repo", next);
      if (next !== null && nextPath) search.set("path", nextPath);
      const query = search.toString();
      router.push(query ? `${pathname}?${query}` : pathname, { scroll: false });
    },
    [pathname, router],
  );
  const open = useCallback((next: string) => navigate(next), [navigate]);

  const error = repositories.error instanceof ApiError ? repositories.error : null;

  let body;
  if (error !== null) {
    body = (
      <div className={classes.center}>
        <ApiState error={error} retrying={repositories.isFetching} onRetry={() => void repositories.refetch()} />
      </div>
    );
  } else if (merged === undefined) {
    body = (
      <div className={classes.center} aria-busy="true">
        <Loader size="sm" type="dots" />
      </div>
    );
  } else if (slug !== null && selected === null) {
    body = (
      <div className={classes.center} role="alert">
        <p className={classes.missing}>No indexed repository is called “{slug}”.</p>
        <Button variant="default" onClick={() => navigate(null)}>
          Show all repositories
        </Button>
      </div>
    );
  } else if (selected !== null) {
    body = <RepositoryLevel repository={selected} path={path} repositories={merged} onNavigate={navigate} />;
  } else {
    body = <OverviewLevel repositories={merged} onOpen={open} />;
  }

  return (
    <AppShell header={{ height: 52 }} padding={0} classNames={{ header: classes.header, main: classes.main }}>
      <AppShell.Header>
        <AppHeader
          repositories={merged}
          freshness={{
            live: live.isSuccess,
            checking: live.isFetching,
            failed: live.isError,
            checkedAt: live.dataUpdatedAt,
            recheck: () => void live.refetch(),
          }}
          offline={error?.unavailable ?? false}
        />
      </AppShell.Header>
      <AppShell.Main>{body}</AppShell.Main>
      <RepositorySpotlight repositories={merged ?? []} onSelect={open} />
    </AppShell>
  );
}
