"use client";

import { AppShell, Button, Loader } from "@mantine/core";
import { usePathname, useSearchParams } from "next/navigation";
import { useCallback, useMemo, useRef, useState } from "react";

import { TransitionContext, type Exit, type RegisterExit } from "@/components/graph/transition";
import { AppHeader } from "@/components/shell/AppHeader";
import { RepositorySpotlight } from "@/components/shell/SearchTrigger";
import { ApiState } from "@/components/states/ApiState";
import type { Navigation } from "@/components/views/navigation";
import { OverviewLevel } from "@/components/views/OverviewLevel";
import { RepositoryLevel } from "@/components/views/RepositoryLevel";
import { ApiError } from "@/lib/api/client";
import { useLiveFreshness, useRepositories } from "@/lib/api/queries";
import { direction, placeKey, search, type Place } from "@/lib/model/location";
import type { RepositoryModel } from "@/lib/model/repositories";

import classes from "./Explorer.module.css";

/**
 * The one explorer screen. Where you are lives in the address
 * (`?repo=<slug>&path=<dir or file>&sel=<node>`), so a view can be shared and
 * Back climbs out. Going somewhere pushes an entry once the canvas has zoomed
 * on its way; choosing something replaces the current one.
 */
export function Explorer() {
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
  const selected = params.get("sel");
  const here = useMemo<Place>(() => ({ repository: slug, path: slug === null ? "" : path }), [slug, path]);
  const current = slug === null ? null : (merged?.find((r) => r.slug === slug) ?? null);

  // how the reader got here: deeper, back out, or across, and from where
  const key = placeKey(here);
  const [track, setTrack] = useState<{ key: string; place: Place; arrival: Navigation["arrival"] }>({
    key,
    place: here,
    arrival: { direction: "none", previous: null },
  });
  let arrival = track.arrival;
  if (track.key !== key) {
    arrival = { direction: direction(track.place, here), previous: track.place };
    setTrack({ key, place: here, arrival });
  }

  const exit = useRef<Exit | null>(null);
  const register = useCallback<RegisterExit>((next) => {
    exit.current = next;
    return () => {
      if (exit.current === next) exit.current = null;
    };
  }, []);
  const moving = useRef(false);
  const select = useCallback(
    (id: string | null) => window.history.replaceState(null, "", `${pathname}${search(here, id)}`),
    [pathname, here],
  );
  const go = useCallback<Navigation["go"]>(
    async (to, options = {}) => {
      if (moving.current) return;
      if (placeKey(to) === placeKey(here)) {
        if (options.select !== undefined) select(options.select);
        return;
      }
      moving.current = true;
      try {
        // the canvas zooms on its way out; a transition that never settles
        // (the canvas unmounting under it) must not hold the reader here
        const leaving = exit.current?.(direction(here, to), options.via ?? null);
        if (leaving) await Promise.race([leaving, new Promise((resolve) => setTimeout(resolve, 700))]);
        // the way back lands on what was gone into, chosen
        if (options.via) window.history.replaceState(null, "", `${pathname}${search(here, options.via)}`);
        window.history.pushState(null, "", `${pathname}${search(to, options.select ?? null)}`);
      } finally {
        moving.current = false;
      }
    },
    [here, pathname, select],
  );
  const nav = useMemo<Navigation>(
    () => ({
      place: here,
      selected,
      arrival,
      go: (to, options) => void go(to, options),
      select,
      // the address itself: a second press can land before the view re-renders
      isSelected: (id) => new URLSearchParams(window.location.search).get("sel") === id,
    }),
    [here, selected, arrival, go, select],
  );
  const openRepository = useCallback((next: string) => nav.go({ repository: next, path: "" }), [nav]);

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
  } else if (slug !== null && current === null) {
    body = (
      <div className={classes.center} role="alert">
        <p className={classes.missing}>No indexed repository is called “{slug}”.</p>
        <Button variant="default" onClick={() => nav.go({ repository: null, path: "" })}>
          Show all repositories
        </Button>
      </div>
    );
  } else if (current !== null) {
    body = <RepositoryLevel repository={current} repositories={merged} nav={nav} />;
  } else {
    body = <OverviewLevel repositories={merged} nav={nav} />;
  }

  return (
    <TransitionContext.Provider value={register}>
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
        <RepositorySpotlight repositories={merged ?? []} onSelect={openRepository} />
      </AppShell>
    </TransitionContext.Provider>
  );
}
