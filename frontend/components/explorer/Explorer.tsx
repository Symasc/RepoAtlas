"use client";

import { AppShell } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useMemo } from "react";

import { Canvas } from "@/components/canvas/Canvas";
import { Inspector } from "@/components/inspector/Inspector";
import { AppHeader } from "@/components/shell/AppHeader";
import { RepositorySpotlight } from "@/components/shell/SearchTrigger";
import { ApiError } from "@/lib/api/client";
import { useLiveFreshness, useRepositories } from "@/lib/api/queries";
import type { RepositoryModel } from "@/lib/model/repositories";

import classes from "./Explorer.module.css";

/**
 * The one explorer screen. Navigation lives in the URL (`?repo=<slug>`), so a
 * view can be shared and Back undoes a selection; P2 onwards add the graph to
 * the canvas and deeper levels to the same parameters.
 */
export function Explorer() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const [inspectorOpened, inspector] = useDisclosure(false);

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
  const selected = merged?.find((r) => r.slug === slug) ?? null;

  const select = useCallback(
    (next: string | null) => {
      const search = new URLSearchParams(params.toString());
      if (next === null) search.delete("repo");
      else search.set("repo", next);
      const query = search.toString();
      router.push(query ? `${pathname}?${query}` : pathname, { scroll: false });
      if (next !== null) inspector.open();
    },
    [params, pathname, router, inspector],
  );

  const error = repositories.error instanceof ApiError ? repositories.error : null;

  return (
    <AppShell
      header={{ height: 52 }}
      aside={{
        width: { md: 360, lg: 400 },
        breakpoint: "md",
        collapsed: { mobile: !inspectorOpened, desktop: false },
      }}
      padding={0}
      classNames={{ header: classes.header, main: classes.main, aside: classes.aside }}
    >
      <AppShell.Header>
        <AppHeader
          repositories={merged}
          selected={selected}
          onSelect={select}
          freshness={{
            live: live.isSuccess,
            checking: live.isFetching,
            failed: live.isError,
            checkedAt: live.dataUpdatedAt,
            recheck: () => void live.refetch(),
          }}
          offline={error?.unavailable ?? false}
          inspectorOpened={inspectorOpened}
          onToggleInspector={inspector.toggle}
        />
      </AppShell.Header>

      <AppShell.Main>
        <Canvas
          repositories={merged}
          loading={repositories.isPending}
          error={error}
          retrying={repositories.isFetching}
          onRetry={() => void repositories.refetch()}
        />
      </AppShell.Main>

      <AppShell.Aside aria-label="Inspector">
        <Inspector
          repositories={merged}
          loading={repositories.isPending}
          failed={error !== null}
          selected={selected}
          onSelect={select}
        />
      </AppShell.Aside>

      <RepositorySpotlight repositories={merged ?? []} onSelect={select} />
    </AppShell>
  );
}
