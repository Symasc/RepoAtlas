"use client";

import { Button, MultiSelect, Select, TextInput } from "@mantine/core";
import { useState } from "react";

import { useDeclarations, useMapPreview } from "@/lib/api/connections";
import type { CandidateService, ConnectionGroup } from "@/lib/api/generated";
import {
  createRequest,
  EMPTY_MAP,
  formProblems,
  isGood,
  OUTCOME_TEXT,
  previewLine,
  previewRequest,
  type MapForm,
} from "@/lib/model/connections";

import classes from "./Connections.module.css";

/**
 * Declares that a group's origin is served by one indexed service. Nothing is stored until the
 * preview has said, request by request, what the sync would do, and the operator confirms. The
 * declaration is stored as a deployment link: marked declared, applied at the next sync, never
 * overriding what the source establishes.
 */
export function MapService({
  group,
  candidates,
  repositories,
  onDone,
}: {
  group: ConnectionGroup;
  candidates: CandidateService[];
  /** Every indexed repository, so a service the shortlist missed can still be chosen (and refused). */
  repositories: { slug: string }[];
  onDone: () => void;
}) {
  const [form, setForm] = useState<MapForm>(EMPTY_MAP);
  const { declare } = useDeclarations();
  const request = previewRequest(group, form);
  const preview = useMapPreview(request);
  const problems = formProblems(group, form);
  const set = <K extends keyof MapForm>(key: K, value: MapForm[K]) => setForm((f) => ({ ...f, [key]: value }));

  const shortlisted = new Set(candidates.map((c) => c.repository));
  const data = [
    {
      group: "Services whose routes match these requests",
      items: candidates.map((c) => ({
        value: c.repository,
        label: `${c.repository} · ${c.requests_unique} of ${group.declarable} would link`,
      })),
    },
    {
      group: "Other indexed repositories",
      items: repositories.filter((r) => !shortlisted.has(r.slug)).map((r) => ({ value: r.slug, label: r.slug })),
    },
  ].filter((section) => section.items.length > 0);

  const requirements = [...problems, ...(preview.data?.requirements ?? [])];
  const unique = [...new Set(requirements)];
  const confirmed = declare.data;
  const canDeclare =
    preview.data !== undefined && preview.data.can_create && problems.length === 0 && !declare.isPending;

  if (confirmed) {
    return (
      <div className={classes.mapper} role="status">
        <p className={classes.banner} data-tone="success">
          Declared as #{confirmed.link.id}: {confirmed.link.repository} serves {confirmed.link.url}. It is
          marked declared and takes effect at the next sync; until then it is pending.
        </p>
        <div className={classes.actions}>
          <Button size="compact-sm" variant="default" onClick={onDone}>
            Close
          </Button>
        </div>
      </div>
    );
  }

  return (
    <form
      className={classes.mapper}
      aria-label={`Map ${group.origin ?? "this origin"} to a service`}
      onSubmit={(event) => {
        event.preventDefault();
        if (canDeclare && preview.data) declare.mutate(createRequest(group, form, preview.data));
      }}
    >
      <p className={classes.detail}>
        Declare which indexed service answers at <span className={classes.mono}>{group.origin}</span>. This is
        your statement, not evidence: it joins a request only where that service serves a matching route.
      </p>
      {group.loopback && (
        <p className={classes.banner} data-tone="warning">
          A loopback origin names a different program in each repository&apos;s development setup. This
          declaration applies only to the caller repositories you name, in the environment you name.
        </p>
      )}
      <div className={classes.fields}>
        <Select
          label="Service"
          placeholder="Choose an indexed service"
          data={data}
          searchable
          value={form.repository || null}
          onChange={(value) => {
            const found = candidates.find((c) => c.repository === value);
            setForm((f) => ({ ...f, repository: value ?? "", basePath: found?.suggested_base_path ?? "" }));
          }}
        />
        <TextInput
          label="Base path"
          description="Empty for the origin's root, or e.g. /v1"
          value={form.basePath}
          onChange={(event) => set("basePath", event.currentTarget.value)}
        />
        <MultiSelect
          label={group.loopback ? "Caller repositories (required)" : "Caller repositories"}
          description={group.loopback ? "Which repositories' requests this serves" : "Empty: every caller"}
          data={group.callers}
          value={form.callers}
          onChange={(value) => set("callers", value)}
        />
        <TextInput
          label={group.loopback ? "Environment (required)" : "Environment"}
          placeholder="dev, staging, prod…"
          value={form.environment}
          onChange={(event) => set("environment", event.currentTarget.value)}
        />
        <TextInput
          label="Note"
          description="Kept in the history; never read by the join"
          maxLength={500}
          value={form.note}
          onChange={(event) => set("note", event.currentTarget.value)}
        />
      </div>

      {form.repository && preview.isFetching && <p className={classes.detail}>Reading what this would do…</p>}
      {preview.error && (
        <p className={classes.banner} data-tone="danger" role="alert">
          {preview.error.message}
        </p>
      )}
      {preview.data && (
        <section aria-label="Preview">
          <p className={classes.previewCounts}>
            <strong>Preview:</strong> {previewLine(preview.data)}
          </p>
          <p className={classes.detail}>
            {preview.data.repository} serves {preview.data.routes_served} route
            {preview.data.routes_served === 1 ? "" : "s"} this index holds. URL declared:{" "}
            <span className={classes.mono}>{preview.data.url}</span>
          </p>
          <ul className={classes.preview}>
            {(preview.data.rows ?? []).map((row) => (
              <li key={row.decision_ref} className={classes.previewRow}>
                <span className={classes.outcome} data-good={isGood(row.outcome)}>
                  {OUTCOME_TEXT[row.outcome]}
                </span>
                {row.method && <span className={classes.method}>{row.method}</span>}
                <span className={classes.route}>{row.route}</span>
                <span className={classes.place}>
                  {row.site.repository}:{row.site.path}
                  {row.site.line_start != null ? `:${row.site.line_start}` : ""}
                </span>
                {row.handler && <span className={classes.detail}>→ handler {row.handler}</span>}
              </li>
            ))}
          </ul>
          {preview.data.has_more && <p className={classes.detail}>More requests than are listed here match.</p>}
          {preview.data.duplicate_of != null && (
            <p className={classes.detail}>Already declared as #{preview.data.duplicate_of}: confirming returns it.</p>
          )}
          {(preview.data.warnings ?? []).length > 0 && (
            <ul className={classes.warnings}>
              {preview.data.warnings!.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          )}
        </section>
      )}
      {unique.length > 0 && (
        <ul className={classes.problems} role="alert" aria-label="What must change first">
          {unique.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      )}
      {declare.error && (
        <p className={classes.banner} data-tone="danger" role="alert">
          {declare.error.message}
        </p>
      )}
      <div className={classes.actions}>
        <Button type="submit" size="compact-sm" disabled={!canDeclare} loading={declare.isPending}>
          Declare (applies at the next sync)
        </Button>
        <Button type="button" size="compact-sm" variant="subtle" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
