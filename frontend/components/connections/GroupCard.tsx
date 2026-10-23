"use client";

import { Button, Loader } from "@mantine/core";
import { useState } from "react";

import { useGroup } from "@/lib/api/connections";
import type { ConnectionGroup } from "@/lib/api/generated";
import {
  groupTitle,
  refusalText,
  REASON_TEXT,
  STATE_LABEL,
  STATE_MEANING,
  unmappableReason,
  type ConnectionFilters,
} from "@/lib/model/connections";
import { plural } from "@/lib/model/format";

import classes from "./Connections.module.css";
import { MapService } from "./MapService";
import { RequestRows } from "./RequestRows";

/** One target (a stated origin, or a base the source does not state) and what can be done about it. */
export function GroupCard({
  group,
  filters,
  repositories,
  onOpenPlace,
}: {
  group: ConnectionGroup;
  filters: ConnectionFilters;
  repositories: { slug: string }[];
  onOpenPlace: (place: { repository: string; path: string; line_start?: number | null }) => void;
}) {
  const [showAll, setShowAll] = useState(false);
  const [mapping, setMapping] = useState(false);
  const expanded = showAll || mapping;
  const page = useGroup(expanded ? group.key : null, filters);
  const requests = page.data?.pages.flatMap((p) => p.requests ?? []) ?? [];
  const candidates = page.data?.pages[0]?.candidates ?? [];
  const blocked = unmappableReason(group);
  const title = groupTitle(group);

  return (
    <li className={classes.group} aria-label={title}>
      <header className={classes.groupHead}>
        <h3 className={classes.origin}>{title}</h3>
        <span className={classes.tag}>{plural(group.requests, "request")}</span>
        {Object.keys(group.languages ?? {}).map((language) => (
          <span key={language} className={classes.tag}>
            {language} {group.languages[language]}
          </span>
        ))}
        {group.loopback && (
          <span className={classes.tag} data-tone="warning" title="A loopback origin names a different service in each repository">
            loopback
          </span>
        )}
        {Object.entries(group.statuses ?? {}).map(([status, n]) => (
          <span key={status} className={classes.tag}>
            {status} {n}
          </span>
        ))}
      </header>
      <p className={classes.detail}>
        Made by {group.callers.join(", ") || "no repository"}.
        {group.declarable > 0
          ? ` ${plural(group.declarable, "request")} can be mapped.`
          : ""}
      </p>
      {Object.keys(group.reasons ?? {}).length > 0 && (
        <ul className={classes.reasons} aria-label="Why they are unresolved">
          {Object.entries(group.reasons).map(([reason, n]) => (
            <li key={reason}>
              <span className={classes.tag}>{reason}</span> {plural(n ?? 0, "request")}
              {refusalText(reason) ? `: ${refusalText(reason)}` : ""}
            </li>
          ))}
        </ul>
      )}
      {(group.declarations ?? []).map((d) => (
        <p key={d.id} className={classes.detail}>
          Declaration #{d.id}: {d.repository} at {d.url ?? "its target"} ·{" "}
          <span className={classes.state} data-state={d.state} title={STATE_MEANING[d.state]}>
            {STATE_LABEL[d.state]}
          </span>
          {d.reason ? ` · ${REASON_TEXT[d.reason] ?? d.reason}` : ""}
        </p>
      ))}
      {!expanded && <RequestRows requests={group.samples ?? []} onOpenPlace={onOpenPlace} />}
      {showAll && (
        <>
          {page.isLoading && (
            <p className={classes.detail}>
              <Loader size={12} type="dots" /> Reading the requests
            </p>
          )}
          <RequestRows requests={requests} onOpenPlace={onOpenPlace} />
          {page.hasNextPage && (
            <Button size="compact-xs" variant="subtle" onClick={() => page.fetchNextPage()} loading={page.isFetchingNextPage}>
              Show more
            </Button>
          )}
        </>
      )}
      <div className={classes.actions}>
        {group.requests > (group.samples ?? []).length && (
          <Button
            size="compact-xs"
            variant="subtle"
            aria-pressed={showAll}
            onClick={() => setShowAll((value) => !value)}
          >
            {showAll ? "Show fewer requests" : `Show all ${group.requests} requests`}
          </Button>
        )}
        {blocked === null ? (
          <Button
            size="compact-sm"
            variant="default"
            aria-label={`Map ${title} to a service`}
            aria-expanded={mapping}
            onClick={() => setMapping((value) => !value)}
          >
            Map to a service…
          </Button>
        ) : (
          <span className={classes.detail}>{blocked}</span>
        )}
      </div>
      {mapping && (
        <>
          {page.isLoading && <p className={classes.detail}>Looking for services whose routes match…</p>}
          {!page.isLoading && (
            <MapService
              group={group}
              candidates={candidates}
              repositories={repositories}
              onDone={() => setMapping(false)}
            />
          )}
        </>
      )}
    </li>
  );
}
