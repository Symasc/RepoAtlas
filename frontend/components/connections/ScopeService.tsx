"use client";

import { Button, Radio, Select, TextInput } from "@mantine/core";
import { useState } from "react";

import { useScopedDeclaration, useScopedPreview } from "@/lib/api/connections";
import type { CandidateService, ConnectionGroup, ConnectionRequest } from "@/lib/api/generated";
import {
  baseKeyFor,
  canDeclareScoped,
  choiceLabel,
  choiceOf,
  EMPTY_SCOPE,
  isGood,
  maskedBase,
  OUTCOME_TEXT,
  previewLine,
  scopedCreateRequest,
  scopeButtonLabel,
  scopedPreviewRequest,
  scopeKey,
  scopeLine,
  scopeProblems,
  SCOPE_REFUSAL_TEXT,
  type ScopeChoice,
  type ScopeForm,
} from "@/lib/model/connections";
import { NO_MATCH_NOTE, rowEvidence } from "@/lib/model/providerEvidence";

import classes from "./Connections.module.css";
import { ProviderEvidence, RowHints } from "./ProviderEvidence";

/**
 * Declares that the base a caller's code does not state, written in one file (or one symbol of it),
 * is served by one indexed service. The scope and the service are the operator's choice: neither is
 * preselected. Nothing is stored until the preview has said, request by request, what the sync would
 * do, with no requirement left and at least one request that would link. The base is shown masked
 * (a plain name, or {…}); its text is never sent, stored or displayed.
 */
export function ScopeService({
  group,
  choices,
  requests,
  candidates,
  repositories,
  onDone,
}: {
  group: ConnectionGroup;
  choices: ScopeChoice[];
  /** The group's requests listed so far: the base's identity is read from them. */
  requests: ConnectionRequest[];
  candidates: CandidateService[];
  /** Every indexed repository, so a service the shortlist missed can still be chosen (and refused). */
  repositories: { slug: string }[];
  onDone: () => void;
}) {
  const [form, setForm] = useState<ScopeForm>(EMPTY_SCOPE);
  const declare = useScopedDeclaration();
  const choice = choiceOf(choices, form.choice);
  const base = choice ? baseKeyFor(choice, requests) : null;
  const request = scopedPreviewRequest(group, choice, base, form);
  const preview = useScopedPreview(request);
  const problems = scopeProblems(choice, base, form);
  const set = <K extends keyof ScopeForm>(key: K, value: ScopeForm[K]) => setForm((f) => ({ ...f, [key]: value }));
  const expression = maskedBase(group.base_expression);

  const shortlisted = new Set(candidates.map((c) => c.repository));
  const data = [
    {
      group: "Services whose routes match these requests",
      items: candidates.map((c) => ({
        value: c.repository,
        label: `${c.repository} · ${c.requests_unique} would link${
          c.requests_ambiguous > 0 ? ` · ${c.requests_ambiguous} ambiguous` : ""
        }`,
      })),
    },
    {
      group: "Other indexed repositories",
      items: repositories.filter((r) => !shortlisted.has(r.slug)).map((r) => ({ value: r.slug, label: r.slug })),
    },
  ].filter((section) => section.items.length > 0);

  const unique = [...new Set([...problems, ...(preview.data?.requirements ?? [])])];
  const confirmed = declare.data;
  const canDeclare = canDeclareScoped(preview.data, problems, declare.isPending);

  if (confirmed) {
    return (
      <div className={classes.mapper} role="status">
        <p className={classes.banner} data-tone="success">
          Declared as #{confirmed.link.id}: {confirmed.link.repository} serves{" "}
          {confirmed.link.scope ? scopeLine(confirmed.link.scope) : "this base"}. It is marked declared and
          takes effect at the next sync; until then it is pending.
        </p>
        <div className={classes.actions}>
          <Button size="compact-sm" variant="default" onClick={onDone}>
            Close
          </Button>
        </div>
      </div>
    );
  }

  const refusals = [...new Set(requests.map((r) => r.scope_refusal).filter((code): code is string => !!code))].filter(
    (code) => code !== "not-base-unknown",
  );

  return (
    <form
      className={classes.mapper}
      aria-label={scopeButtonLabel(group)}
      onSubmit={(event) => {
        event.preventDefault();
        const body = choice && preview.data ? scopedCreateRequest(group, choice, form, preview.data) : null;
        if (canDeclare && body) declare.mutate(body);
      }}
    >
      <p className={classes.detail}>
        Declare which indexed service answers the base <span className={classes.mono}>{expression}</span>, in
        one file or symbol of the repository that writes it. This is your statement, not evidence: it joins a
        request only inside that scope, written on that base, where the service serves exactly one matching
        route. No address is stored.
      </p>
      {choices.length === 0 ? (
        <>
          <p className={classes.detail}>No file of this group&apos;s requests can be named as a scope.</p>
          {refusals.length > 0 && (
            <ul className={classes.reasons} aria-label="Why no scope">
              {refusals.map((code) => (
                <li key={code}>
                  <span className={classes.tag}>{code}</span> {SCOPE_REFUSAL_TEXT[code] ?? ""}
                </li>
              ))}
            </ul>
          )}
        </>
      ) : (
        <Radio.Group
          label="Scope"
          description="Choose where the base is written. A wider scope covers more requests and may be refused as too broad."
          value={form.choice || null}
          onChange={(value) => set("choice", value)}
        >
          <div className={classes.scopeChoices}>
            {choices.map((c) => {
              const label = choiceLabel(c);
              return <Radio key={scopeKey(c)} value={scopeKey(c)} label={label.name} description={label.counts} />;
            })}
          </div>
        </Radio.Group>
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
          description="The prefix the service mounts its routes under, e.g. /v1; empty if none"
          value={form.basePath}
          onChange={(event) => set("basePath", event.currentTarget.value)}
        />
        <TextInput
          label="Environment"
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

      {request !== null && preview.isFetching && <p className={classes.detail}>Reading what this would do…</p>}
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
            {preview.data.routes_served === 1 ? "" : "s"} this index holds. Base declared:{" "}
            <span className={classes.mono}>
              {preview.data.scope ? scopeLine(preview.data.scope) : maskedBase(null)}
            </span>
          </p>
          {preview.data.provider_diagnostics && (
            <ProviderEvidence diagnostics={preview.data.provider_diagnostics} repository={preview.data.repository} />
          )}
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
                <RowHints {...rowEvidence(preview.data!.provider_diagnostics, preview.data!.repository, row)} />
              </li>
            ))}
          </ul>
          {(preview.data.rows ?? []).some((row) => row.outcome === "no-matching-route") && (
            <p className={classes.detail}>{NO_MATCH_NOTE(preview.data.repository)}</p>
          )}
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
        <div role="alert" aria-label="What must change first">
          <ul className={classes.problems}>
            {unique.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        </div>
      )}
      {declare.error && (
        <p className={classes.banner} data-tone="danger" role="alert">
          {declare.error.message}
        </p>
      )}
      <div className={classes.actions}>
        <Button type="submit" size="compact-sm" disabled={!canDeclare} loading={declare.isPending}>
          Declare for this scope (applies at the next sync)
        </Button>
        <Button type="button" size="compact-sm" variant="subtle" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
