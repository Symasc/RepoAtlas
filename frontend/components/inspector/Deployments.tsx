"use client";

import { Button, Checkbox, Loader, Select, Textarea, TextInput } from "@mantine/core";
import { useState } from "react";

import { useDeploymentLinks, useDeploymentTargets } from "@/lib/api/deployments";
import type { DeploymentLink, DeploymentResolution, DeploymentTarget } from "@/lib/api/generated";
import {
  CONDITIONS_MEANING,
  EMPTY_FORM,
  fieldsOf,
  formOf,
  holesOf,
  KIND_LABEL,
  STATE_MEANING,
  statedAt,
  VERIFICATION_LABEL,
  type LinkForm,
} from "@/lib/model/deployments";
import { plural } from "@/lib/model/format";

import { Quiet } from "./Inspector";
import inspectorClasses from "./Inspector.module.css";
import classes from "./Deployments.module.css";

/**
 * Where a repository's source says it is reached (Ingress hosts, OpenAPI servers),
 * resolved for a branch, and the links the user recorded. A URL here is what the
 * configuration states, never a check that it answers.
 */
export function Deployments({ repository }: { repository: string }) {
  const [branch, setBranch] = useState("");
  const [asked, setAsked] = useState<string | null>(null);
  const targets = useDeploymentTargets(repository, asked);
  const [editing, setEditing] = useState<{ id: number | null; form: LinkForm } | null>(null);

  if (targets.error) return <p className={inspectorClasses.problem}>{targets.error.message}</p>;
  if (!targets.data) {
    return (
      <p className={inspectorClasses.loading}>
        <Loader size={12} type="dots" /> Reading the deployment targets
      </p>
    );
  }
  const data = targets.data;
  const stated = (data.targets ?? []).filter((t) => t.kind !== "link");
  const recorded = (data.targets ?? []).filter((t) => t.kind === "link");

  return (
    <div className={classes.deployments}>
      <form
        className={classes.context}
        onSubmit={(event) => {
          event.preventDefault();
          setAsked(branch.trim() || null);
        }}
      >
        <TextInput
          size="xs"
          label="Branch"
          description={
            data.branch_basis === "checkout"
              ? `Conditions are read for ${data.branch ?? "no branch"}, the checkout's branch.`
              : `Conditions are read for ${data.branch ?? "no branch"}.`
          }
          placeholder={data.branch ?? "main"}
          value={branch}
          onChange={(event) => setBranch(event.currentTarget.value)}
        />
        <Button size="xs" variant="default" type="submit">
          Resolve
        </Button>
      </form>

      <section className={inspectorClasses.section} aria-label="Stated targets">
        <h3 className={inspectorClasses.sectionTitle}>
          Stated in source <span className={inspectorClasses.sectionCount}>{stated.length}</span>
        </h3>
        {stated.length === 0 && (
          <Quiet>
            No indexed Ingress or OpenAPI server states where this repository is reached. An OAuth callback or
            redirect URL is not a deployment target; record the deployment as a link below.
          </Quiet>
        )}
        {stated.map((target) => (
          <TargetCard
            key={target.target_id}
            target={target}
            onLink={() => setEditing({ id: null, form: { ...EMPTY_FORM, targetId: target.target_id } })}
          />
        ))}
      </section>

      <section className={inspectorClasses.section} aria-label="Your links">
        <h3 className={inspectorClasses.sectionTitle}>
          Your links <span className={inspectorClasses.sectionCount}>{(data.links ?? []).length}</span>
        </h3>
        {recorded.map((target) => (
          <TargetCard key={target.target_id} target={target} />
        ))}
        {(data.links ?? []).length === 0 && <Quiet>No links recorded for this repository.</Quiet>}
        <p className={classes.detail}>
          From the next sync, a link with a URL (or a target its values resolve) also joins requests the source leaves
          unlinked to this repository&apos;s handlers, marked declared. It never overrides a link the source proves.
        </p>
        <LinkList
          links={data.links ?? []}
          onEdit={(link) => setEditing({ id: link.id, form: formOf(link) })}
          repository={repository}
        />
        {editing ? (
          <LinkEditor
            key={editing.id ?? `new:${editing.form.targetId}`}
            repository={repository}
            id={editing.id}
            initial={editing.form}
            targets={stated}
            onDone={() => setEditing(null)}
          />
        ) : (
          <div>
            <Button size="xs" variant="default" onClick={() => setEditing({ id: null, form: EMPTY_FORM })}>
              Add a link
            </Button>
          </div>
        )}
      </section>

      {(data.diagnostics ?? []).length > 0 && (
        <section className={inspectorClasses.section} aria-label="Not read">
          <h3 className={inspectorClasses.sectionTitle}>Not read</h3>
          <ul className={classes.notes}>
            {(data.diagnostics ?? []).map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </section>
      )}
      <p className={classes.basis}>{data.basis}</p>
    </div>
  );
}

function TargetCard({ target, onLink }: { target: DeploymentTarget; onLink?: () => void }) {
  const at = statedAt(target);
  return (
    <article className={classes.target} aria-label={`${KIND_LABEL[target.kind]} ${target.pattern}`}>
      <header className={classes.targetHead}>
        <span className={classes.kind}>{KIND_LABEL[target.kind]}</span>
        {at && <span className={classes.at}>{at}</span>}
        {target.detached && <span className={classes.flag}>target no longer stated</span>}
      </header>
      {target.runs && (
        <p className={classes.detail}>
          Stated in <strong>{target.repository}</strong>, whose release is configured to run the image{" "}
          {target.runs}&apos;s CI is configured to publish.
        </p>
      )}
      <p className={classes.pattern} title="As configured">
        {target.pattern}
      </p>
      {target.scheme_basis && target.kind === "ingress" && (
        <p className={classes.detail}>Scheme from {target.scheme_basis}.</p>
      )}
      {(target.guards ?? []).length > 0 && (
        <ul className={classes.guards} aria-label="Rendered when">
          {(target.guards ?? []).map((guard) => (
            <li key={`${guard.line}:${guard.expression}`} data-state={guard.state}>
              <code>{guard.expression}</code> <span className={classes.state}>{guard.state}</span>
            </li>
          ))}
        </ul>
      )}
      <ul className={classes.resolutions}>
        {(target.resolutions ?? []).map((resolution, index) => (
          <Resolution key={`${resolution.label}:${index}`} resolution={resolution} />
        ))}
      </ul>
      {(target.diagnostics ?? []).length > 0 && (
        <ul className={classes.notes}>
          {(target.diagnostics ?? []).map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
      {onLink && (
        <div>
          <Button size="compact-xs" variant="subtle" onClick={onLink}>
            Record a link for this target
          </Button>
        </div>
      )}
    </article>
  );
}

function Resolution({ resolution }: { resolution: DeploymentResolution }) {
  const holes = holesOf(resolution);
  const conditions = resolution.conditions ?? "none";
  const verification = resolution.verification ?? "unverified";
  return (
    <li className={classes.resolution} data-state={resolution.state}>
      <div className={classes.resolutionHead}>
        <span className={classes.label}>{resolution.label}</span>
        <span className={classes.state} title={STATE_MEANING[resolution.state]}>
          {resolution.state}
        </span>
        {conditions !== "none" && (
          <span className={classes.state} title={CONDITIONS_MEANING[conditions]}>
            conditions: {conditions}
          </span>
        )}
        {resolution.enabled && resolution.enabled !== "met" && (
          <span className={classes.state}>enabled: {resolution.enabled}</span>
        )}
        <span className={classes.verification} data-verification={verification}>
          {VERIFICATION_LABEL[verification]}
        </span>
      </div>
      <p className={classes.url}>{resolution.url ?? resolution.rendered}</p>
      {resolution.duplicate_of && <p className={classes.detail}>Same URL as another resolution.</p>}
      {(resolution.variables ?? []).length > 0 && (
        <dl className={classes.variables}>
          {(resolution.variables ?? []).map((variable) => (
            <div key={variable.name} className={classes.variable}>
              <dt>{variable.name}</dt>
              <dd>
                {variable.withheld ? "withheld" : (variable.value ?? "not set")}
                {variable.basis && <span className={classes.basisTag}> · {variable.basis}</span>}
                {variable.at && <span className={classes.basisTag}> · {variable.at}</span>}
              </dd>
            </div>
          ))}
        </dl>
      )}
      {(resolution.notes ?? []).length > 0 && (
        <ul className={classes.notes}>
          {(resolution.notes ?? []).map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
      {holes.length > 0 && (
        <p className={classes.detail}>
          {plural(holes.length, "variable")} open: a link can supply {holes.length === 1 ? "it" : "them"}.
        </p>
      )}
    </li>
  );
}

function LinkList({
  links,
  onEdit,
  repository,
}: {
  links: DeploymentLink[];
  onEdit: (link: DeploymentLink) => void;
  repository: string;
}) {
  const { remove } = useDeploymentLinks(repository);
  if (links.length === 0) return null;
  return (
    <ul className={classes.links}>
      {links.map((link) => (
        <li key={link.id} className={classes.link}>
          <div className={classes.resolutionHead}>
            <span className={classes.label}>{link.label}</span>
            {link.environment && <span className={classes.state}>{link.environment}</span>}
            <span className={classes.verification} data-verification={link.verification ?? "unverified"}>
              {VERIFICATION_LABEL[link.verification ?? "unverified"]}
            </span>
          </div>
          {link.url && <p className={classes.url}>{link.url}</p>}
          <div className={classes.linkActions}>
            <Button size="compact-xs" variant="subtle" aria-label={`Edit ${link.label}`} onClick={() => onEdit(link)}>
              Edit
            </Button>
            <Button
              size="compact-xs"
              variant="subtle"
              color="red"
              aria-label={`Remove ${link.label}`}
              loading={remove.isPending && remove.variables === link.id}
              onClick={() => remove.mutate(link.id)}
            >
              Remove
            </Button>
          </div>
        </li>
      ))}
      {remove.error && <p className={inspectorClasses.problem}>{remove.error.message}</p>}
    </ul>
  );
}

function LinkEditor({
  repository,
  id,
  initial,
  targets,
  onDone,
}: {
  repository: string;
  id: number | null;
  initial: LinkForm;
  targets: DeploymentTarget[];
  onDone: () => void;
}) {
  const [form, setForm] = useState(initial);
  const [problems, setProblems] = useState<string[]>([]);
  const { create, update } = useDeploymentLinks(repository);
  const pending = create.isPending || update.isPending;
  const failure = create.error ?? update.error;
  const set = <K extends keyof LinkForm>(key: K, value: LinkForm[K]) => setForm((f) => ({ ...f, [key]: value }));
  const options = [
    { value: "", label: "None: a link of its own" },
    ...targets.map((t) => ({ value: t.target_id, label: t.pattern })),
  ];

  return (
    <form
      className={classes.editor}
      aria-label={id === null ? "Add a deployment link" : "Edit the deployment link"}
      onSubmit={(event) => {
        event.preventDefault();
        const parsed = fieldsOf(form);
        if ("problems" in parsed) {
          setProblems(parsed.problems);
          return;
        }
        setProblems([]);
        const done = { onSuccess: onDone };
        if (id === null) create.mutate(parsed.fields, done);
        else update.mutate({ id, link: parsed.fields }, done);
      }}
    >
      <TextInput size="xs" label="Label" required value={form.label} onChange={(e) => set("label", e.currentTarget.value)} />
      <TextInput
        size="xs"
        label="Environment"
        placeholder="staging"
        value={form.environment}
        onChange={(e) => set("environment", e.currentTarget.value)}
      />
      <TextInput
        size="xs"
        label="URL"
        description="http or https; no credentials, query or fragment."
        placeholder="https://app.example.test/"
        value={form.url}
        onChange={(e) => set("url", e.currentTarget.value)}
      />
      <Select
        size="xs"
        label="Resolves target"
        data={options}
        value={form.targetId}
        allowDeselect={false}
        onChange={(value) => set("targetId", value ?? "")}
      />
      <Textarea
        size="xs"
        label="Variables"
        description="One name=value per line. {branch} or {environment} takes the context's value."
        autosize
        minRows={2}
        value={form.variables}
        onChange={(e) => set("variables", e.currentTarget.value)}
      />
      <Textarea
        size="xs"
        label="Conditions"
        description="One per line: branch=main, branch~release-*, environment=staging, pull_request=false, var:NAME=value."
        autosize
        minRows={2}
        value={form.conditions}
        onChange={(e) => set("conditions", e.currentTarget.value)}
      />
      <Textarea
        size="xs"
        label="Callers"
        description="One repository slug per line: only their requests join this link. Required for a localhost URL; empty serves every caller."
        autosize
        minRows={1}
        value={form.callers}
        onChange={(e) => set("callers", e.currentTarget.value)}
      />
      <Checkbox
        size="xs"
        label="I confirmed this deployment myself"
        checked={form.confirmed}
        onChange={(e) => set("confirmed", e.currentTarget.checked)}
      />
      {problems.length > 0 && (
        <ul className={inspectorClasses.problem} role="alert">
          {problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      )}
      {failure && (
        <p className={inspectorClasses.problem} role="alert">
          {failure.message}
        </p>
      )}
      <div className={classes.linkActions}>
        <Button size="xs" type="submit" loading={pending}>
          {id === null ? "Add link" : "Save link"}
        </Button>
        <Button size="xs" variant="default" onClick={onDone} disabled={pending}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
