"use client";

import { Drawer, Loader, Select, Switch, Tabs, TextInput } from "@mantine/core";
import { useState } from "react";

import { useConnections } from "@/lib/api/connections";
import {
  LANGUAGE_OPTIONS,
  NO_FILTERS,
  pendingCount,
  STATUS_OPTIONS,
  summaryLine,
  type ConnectionFilters,
} from "@/lib/model/connections";
import type { RepositoryModel } from "@/lib/model/repositories";

import classes from "./Connections.module.css";
import { Declarations, History } from "./Declarations";
import { GroupCard } from "./GroupCard";

/**
 * HTTP connections: every request the workspace join could not link, grouped by what it targets,
 * with the reason it was refused and where it is written. A stated origin can be mapped to the one
 * indexed service that answers there; that is the operator's declaration, applied at the next sync,
 * marked declared and never treated as proven. A base the source does not state is listed, not
 * mappable.
 */
export function ConnectionsPanel({
  opened,
  onClose,
  repositories,
  onOpenPlace,
}: {
  opened: boolean;
  onClose: () => void;
  repositories: RepositoryModel[];
  onOpenPlace: (place: { repository: string; path: string; line_start?: number | null }) => void;
}) {
  const [filters, setFilters] = useState<ConnectionFilters>(NO_FILTERS);
  const [tab, setTab] = useState<string>("requests");
  const connections = useConnections(filters, opened);
  const data = connections.data;
  const set = <K extends keyof ConnectionFilters>(key: K, value: ConnectionFilters[K]) =>
    setFilters((f) => ({ ...f, [key]: value }));
  const callers = repositories.map((r) => ({ value: r.slug, label: r.slug }));
  const pending = data ? pendingCount(data.declarations ?? []) : 0;

  return (
    <Drawer
      opened={opened}
      onClose={onClose}
      position="right"
      size="min(980px, 100vw)"
      title="HTTP connections"
      aria-label="HTTP connections"
      closeButtonProps={{ "aria-label": "Close HTTP connections" }}
    >
      <div className={classes.panel}>
        <p className={classes.basis}>
          Requests in your code that RepoAtlas could not link to a handler. Map an origin to the service that
          answers there and the next sync joins what that service serves. A mapping is your declaration, not
          evidence: it is marked <em>declared</em>, applies only where a route matches, and never overrides what
          the source proves.
        </p>
        {connections.error && (
          <p className={classes.banner} data-tone="danger" role="alert">
            {connections.error.message}
          </p>
        )}
        {!data && !connections.error && (
          <p className={classes.detail}>
            <Loader size={12} type="dots" /> Reading the requests
          </p>
        )}
        {data && (
          <>
            <p className={classes.summary} aria-label="Summary">
              {summaryLine(data.summary)}
            </p>
            {!data.built && (
              <p className={classes.banner} data-tone="warning">
                No sync has stored the join&apos;s decisions in this index yet: sync to list requests.
              </p>
            )}
            {pending > 0 && (
              <p className={classes.banner} data-tone="warning" role="status">
                {pending} declaration{pending === 1 ? " is" : "s are"} pending: it takes effect at the next sync.
              </p>
            )}
            <Tabs value={tab} onChange={(value) => setTab(value ?? "requests")} keepMounted={false}>
              <Tabs.List aria-label="HTTP connections views">
                <Tabs.Tab value="requests">Requests to map ({data.summary.unresolved})</Tabs.Tab>
                <Tabs.Tab value="declarations">Your declarations ({(data.declarations ?? []).length})</Tabs.Tab>
                <Tabs.Tab value="history">History</Tabs.Tab>
              </Tabs.List>
              <Tabs.Panel value="requests" pt="md">
                <div className={classes.panel}>
                  <div className={classes.filters} role="group" aria-label="Filters">
                    <Select
                      label="Decision"
                      placeholder="Any unresolved"
                      clearable
                      data={STATUS_OPTIONS}
                      value={filters.status || null}
                      onChange={(value) => set("status", value ?? "")}
                    />
                    <Select
                      label="Language"
                      placeholder="Any"
                      clearable
                      data={LANGUAGE_OPTIONS}
                      value={filters.language || null}
                      onChange={(value) => set("language", value ?? "")}
                    />
                    <Select
                      label="Made in"
                      placeholder="Any repository"
                      clearable
                      searchable
                      data={callers}
                      value={filters.caller || null}
                      onChange={(value) => set("caller", value ?? "")}
                    />
                    <Select
                      label="Declaration"
                      placeholder="Any"
                      clearable
                      data={["pending", "applied", "ineffective", "invalidated"]}
                      value={filters.declarationState || null}
                      onChange={(value) => set("declarationState", value ?? "")}
                    />
                    <TextInput
                      label="Origin contains"
                      value={filters.text}
                      onChange={(event) => set("text", event.currentTarget.value)}
                    />
                    <Switch
                      label="Show origins already joined"
                      checked={filters.includeResolved}
                      onChange={(event) => set("includeResolved", event.currentTarget.checked)}
                    />
                  </div>
                  {(data.groups ?? []).length === 0 ? (
                    <p className={classes.detail}>
                      {data.summary.unresolved === 0
                        ? "Every request the source writes is linked."
                        : "No origin matches these filters."}
                    </p>
                  ) : (
                    <ul className={classes.groups} aria-label="Origins">
                      {(data.groups ?? []).map((group) => (
                        <GroupCard
                          key={group.key}
                          group={group}
                          filters={filters}
                          repositories={repositories}
                          onOpenPlace={(place) => {
                            onClose();
                            onOpenPlace(place);
                          }}
                        />
                      ))}
                    </ul>
                  )}
                </div>
              </Tabs.Panel>
              <Tabs.Panel value="declarations" pt="md">
                <Declarations declarations={data.declarations ?? []} synced={data.synced ?? false} />
              </Tabs.Panel>
              <Tabs.Panel value="history" pt="md">
                <History />
              </Tabs.Panel>
            </Tabs>
          </>
        )}
      </div>
    </Drawer>
  );
}
