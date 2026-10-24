"use client";

import { Button, Select, TextInput } from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { useState } from "react";

import { useServiceDeclarations, useServicePreview } from "@/lib/api/connections";
import type { ServiceDeclarationView } from "@/lib/api/generated";
import {
  authenticatesBasis,
  classifyCreateRequest,
  classifyFormFrom,
  classifyPreviewRequest,
  classifyProblems,
  classifyUpdateRequest,
  EMPTY_CLASSIFY,
  externalOf,
  identityLabel,
  servicePreviewLine,
  type AuthTargetForm,
  type ClassifyForm,
} from "@/lib/model/connections";

import classes from "./Connections.module.css";

/**
 * Classifies an origin no indexed repository answers as an external service, or edits such a
 * classification (what it is about, the origin, never changes). It is the operator's statement: it
 * joins no request and changes no decision. Nothing is stored until the preview has said which
 * requests would be shown as a known external dependency, with no requirement left and no problem
 * the form can see.
 */
export function ClassifyExternal({
  origin,
  groupKey,
  existing,
  repositories,
  onDone,
}: {
  /** The origin of the group it is made from; an edit reads its own. */
  origin: string | null;
  /** The worklist group it is made from: the server checks its origin and its unmatched requests. */
  groupKey: string | null;
  /** The classification being edited, or null to classify a new one. */
  existing: ServiceDeclarationView | null;
  /** Every indexed repository: an authenticated service must be one. */
  repositories: { slug: string }[];
  onDone: () => void;
}) {
  const stored = existing ? externalOf(existing) : null;
  const target = stored ? stored.origin : origin;
  const [form, setForm] = useState<ClassifyForm>(() => (stored ? classifyFormFrom(stored) : EMPTY_CLASSIFY));
  const { create, update } = useServiceDeclarations();
  // the preview is asked once typing pauses; Save waits until it answers for what the form now holds
  const [settled] = useDebouncedValue(form, 300);
  const current = settled === form;
  const saved = existing ? update.data : create.data;
  const failure = existing ? update.error : create.error;
  const pending = create.isPending || update.isPending;
  const request = saved
    ? null
    : classifyPreviewRequest(target, settled, { group: existing ? null : groupKey, id: existing?.id ?? null });
  const preview = useServicePreview(request);
  const problems = classifyProblems(target, form);
  const requirements = [...new Set([...problems, ...(current ? (preview.data?.requirements ?? []) : [])])];
  const canSave =
    !saved && current && preview.data !== undefined && preview.data.can_create && problems.length === 0 && !pending;

  const set = <K extends keyof ClassifyForm>(key: K, value: ClassifyForm[K]) =>
    setForm((f) => ({ ...f, [key]: value }));
  const setTarget = (index: number, patch: Partial<AuthTargetForm>) =>
    setForm((f) => ({
      ...f,
      authenticates: f.authenticates.map((t, i) => (i === index ? { ...t, ...patch } : t)),
    }));
  const addTarget = () =>
    setForm((f) => ({ ...f, authenticates: [...f.authenticates, { repository: "", environment: "" }] }));
  const removeTarget = (index: number) =>
    setForm((f) => ({ ...f, authenticates: f.authenticates.filter((_, i) => i !== index) }));

  const slugs = [
    ...new Set([...repositories.map((r) => r.slug), ...form.authenticates.map((t) => t.repository).filter(Boolean)]),
  ];

  if (saved) {
    const view = saved.declaration;
    return (
      <div className={classes.mapper} role="status">
        <p className={classes.banner} data-tone="success">
          {existing ? "Updated" : "Classified as"} external service #{view.id}: {externalOf(view)?.label ?? ""}. It
          takes effect now: the unmatched requests at {externalOf(view)?.origin ?? target} are shown as a known
          external dependency. Their decision is unchanged and nothing is joined.
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
      aria-label={existing ? `Edit external service ${existing.id}` : `Classify ${target ?? "this origin"} as external service`}
      onSubmit={(event) => {
        event.preventDefault();
        if (!canSave || !target) return;
        if (existing) update.mutate(classifyUpdateRequest(existing.id, target, form));
        else create.mutate(classifyCreateRequest(target, form, groupKey));
      }}
    >
      <p className={classes.detail}>
        Say that <span className={classes.mono}>{target}</span> is a service outside the indexed repositories. This
        is your statement, not evidence: it joins no request and changes no decision. The requests keep their
        decision and their chain, and are shown as a known external dependency.
      </p>
      <div className={classes.fields}>
        <TextInput
          label="Label"
          description="Required: what you call this service"
          maxLength={300}
          value={form.label}
          onChange={(event) => set("label", event.currentTarget.value)}
        />
        <TextInput
          label="Provider"
          description="Optional: who runs it"
          maxLength={100}
          value={form.provider}
          onChange={(event) => set("provider", event.currentTarget.value)}
        />
        <TextInput
          label="Category"
          description="Optional: e.g. identity provider, payments"
          maxLength={100}
          value={form.category}
          onChange={(event) => set("category", event.currentTarget.value)}
        />
        <TextInput
          label="Note"
          description="Kept in the history; never read by the join"
          maxLength={500}
          value={form.note}
          onChange={(event) => set("note", event.currentTarget.value)}
        />
      </div>

      <div role="group" aria-label="Authentication targets" className={classes.mapper}>
        <p className={classes.detail}>
          Optional: the indexed repositories this service authenticates for. Each is recorded as{" "}
          {authenticatesBasis("declared")}, your statement; it is never read as a deployment or a handler.
        </p>
        {form.authenticates.map((t, index) => (
          <div key={index} className={classes.fields}>
            <Select
              label={`Authenticates repository ${index + 1}`}
              placeholder="Choose an indexed repository"
              data={slugs}
              searchable
              value={t.repository || null}
              onChange={(value) => setTarget(index, { repository: value ?? "" })}
            />
            <TextInput
              label={`Environment of target ${index + 1}`}
              description="Optional: empty means any environment"
              value={t.environment}
              onChange={(event) => setTarget(index, { environment: event.currentTarget.value })}
            />
            <div className={classes.actions}>
              <Button type="button" size="compact-xs" variant="subtle" color="red" onClick={() => removeTarget(index)}>
                Remove target {index + 1}
              </Button>
            </div>
          </div>
        ))}
        <div className={classes.actions}>
          <Button type="button" size="compact-xs" variant="default" onClick={addTarget}>
            Add authentication target
          </Button>
        </div>
      </div>

      {request !== null && (preview.isFetching || !current) && (
        <p className={classes.detail}>Reading what this would do…</p>
      )}
      {preview.error && (
        <p className={classes.banner} data-tone="danger" role="alert">
          {preview.error.message}
        </p>
      )}
      {preview.data && current && (
        <section aria-label="Preview">
          <p className={classes.previewCounts}>
            <strong>Preview:</strong> {servicePreviewLine(preview.data)}
          </p>
          <ul className={classes.preview}>
            {(preview.data.rows ?? []).map((row) => (
              <li key={row.decision_ref} className={classes.previewRow}>
                <span className={classes.outcome} data-good={row.identity_after === "known-external"}>
                  {identityLabel(row.identity_before)} → {identityLabel(row.identity_after)}
                </span>
                {row.method && <span className={classes.method}>{row.method}</span>}
                <span className={classes.route}>{row.route}</span>
                <span className={classes.place}>
                  {row.site.repository}:{row.site.path}
                  {row.site.line_start != null ? `:${row.site.line_start}` : ""}
                </span>
              </li>
            ))}
          </ul>
          {preview.data.has_more && <p className={classes.detail}>More requests than are listed here are at this origin.</p>}
          {preview.data.duplicate_of != null && preview.data.duplicate_of !== existing?.id && (
            <p className={classes.detail}>Already classified as #{preview.data.duplicate_of}: saving returns it.</p>
          )}
          <p className={classes.detail}>{preview.data.basis}</p>
        </section>
      )}
      {requirements.length > 0 && (
        <ul className={classes.problems} role="alert" aria-label="What must change first">
          {requirements.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      )}
      {failure && (
        <p className={classes.banner} data-tone="danger" role="alert">
          {failure.message}
        </p>
      )}
      <div className={classes.actions}>
        <Button type="submit" size="compact-sm" disabled={!canSave} loading={pending}>
          {existing ? "Save changes" : "Save classification"}
        </Button>
        <Button type="button" size="compact-sm" variant="subtle" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
