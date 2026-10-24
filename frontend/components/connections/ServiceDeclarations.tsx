"use client";

import { Button, Loader, TextInput } from "@mantine/core";
import { useState } from "react";

import { useServiceDeclarations, useServiceHistory } from "@/lib/api/connections";
import type { ServiceDeclarationView } from "@/lib/api/generated";
import {
  authenticatesBasis,
  authTargetLine,
  EXTERNAL_REASON_TEXT,
  EXTERNAL_STATE_LABEL,
  EXTERNAL_STATE_MEANING,
  externalOf,
  externalServices,
  serviceHistoryLine,
} from "@/lib/model/connections";
import { absoluteTime } from "@/lib/model/format";

import { ClassifyExternal } from "./ClassifyExternal";
import classes from "./Connections.module.css";

/**
 * What the operator classified as an external service: its words, where it stands against the
 * stored decisions, and the repositories it authenticates for, each marked as the operator's
 * statement. A classified service has no deployment, handler or route here: it is outside the index.
 */
export function ExternalServiceDetail({ view }: { view: ServiceDeclarationView }) {
  const fields = externalOf(view);
  if (!fields) return null;
  const targets = fields.authenticates ?? [];
  const reason = view.reason ?? "";
  return (
    <>
      <p className={classes.detail}>
        {fields.label}
        {fields.provider ? ` · provider ${fields.provider}` : ""}
        {fields.category ? ` · ${fields.category}` : ""} ·{" "}
        <span className={classes.state} data-state={view.state} title={EXTERNAL_STATE_MEANING[view.state]}>
          {EXTERNAL_STATE_LABEL[view.state]}
        </span>
        {reason ? ` · ${EXTERNAL_REASON_TEXT[reason] ?? reason}` : ""}
      </p>
      {fields.note && <p className={classes.detail}>“{fields.note}”</p>}
      {targets.length > 0 && (
        <ul className={classes.reasons} aria-label="Authenticates">
          {targets.map((target) => (
            <li key={`${target.repository}\u0000${target.environment ?? ""}`}>
              Authenticates {authTargetLine(target)} <span className={classes.tag}>{authenticatesBasis(view.basis)}</span>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

/** The operator's external services, each with an edit and a revoke. */
export function ServiceDeclarations({
  declarations,
  repositories,
}: {
  declarations: ServiceDeclarationView[];
  repositories: { slug: string }[];
}) {
  const services = externalServices(declarations);
  return (
    <section className={classes.panel} aria-label="External services">
      <p className={classes.basis}>
        An external service is an origin you classified as a service outside the indexed repositories. It is
        your statement, not evidence: it takes effect when saved, joins no request and changes no decision. The
        index shows it apart from what the source proves.
      </p>
      {services.length === 0 ? (
        <p className={classes.detail}>No origin is classified as an external service.</p>
      ) : (
        <ul className={classes.declarations}>
          {services.map((view) => (
            <ExternalService key={view.id} view={view} repositories={repositories} />
          ))}
        </ul>
      )}
    </section>
  );
}

function ExternalService({
  view,
  repositories,
}: {
  view: ServiceDeclarationView;
  repositories: { slug: string }[];
}) {
  const [mode, setMode] = useState<"view" | "edit" | "revoke">("view");
  const [note, setNote] = useState("");
  const { revoke } = useServiceDeclarations();
  const fields = externalOf(view);
  if (!fields) return null;
  return (
    <li className={classes.declaration} aria-label={`External service ${view.id}`}>
      <div className={classes.groupHead}>
        <span className={classes.origin}>
          #{view.id} {fields.label}
        </span>
        <span className={classes.tag}>classified by you</span>
      </div>
      <p className={classes.mono}>{fields.origin}</p>
      <ExternalServiceDetail view={view} />
      {view.requests != null && view.requests > 0 && (
        <p className={classes.detail}>Shown as a known external dependency on {view.requests} unmatched request{view.requests === 1 ? "" : "s"}.</p>
      )}
      {mode === "edit" && (
        <ClassifyExternal
          origin={fields.origin}
          groupKey={null}
          existing={view}
          repositories={repositories}
          onDone={() => setMode("view")}
        />
      )}
      {mode === "revoke" && (
        <form
          className={classes.mapper}
          aria-label={`Revoke external service ${view.id}`}
          onSubmit={(event) => {
            event.preventDefault();
            revoke.mutate({ id: view.id, note: note.trim() || null });
          }}
        >
          <TextInput
            label="Why (kept in the history)"
            maxLength={500}
            value={note}
            onChange={(event) => setNote(event.currentTarget.value)}
          />
          <p className={classes.detail}>
            Revoking stops showing this origin&apos;s requests as a known external dependency. The history
            keeps the classification.
          </p>
          <div className={classes.actions}>
            <Button type="submit" size="compact-sm" color="red" loading={revoke.isPending}>
              Confirm revoking external service {view.id}
            </Button>
            <Button type="button" size="compact-sm" variant="subtle" onClick={() => setMode("view")}>
              Cancel
            </Button>
          </div>
          {revoke.error && (
            <p className={classes.banner} data-tone="danger" role="alert">
              {revoke.error.message}
            </p>
          )}
        </form>
      )}
      {mode === "view" && (
        <div className={classes.actions}>
          <Button
            size="compact-xs"
            variant="subtle"
            aria-label={`Edit external service ${view.id}`}
            onClick={() => setMode("edit")}
          >
            Edit
          </Button>
          <Button
            size="compact-xs"
            variant="subtle"
            color="red"
            aria-label={`Revoke external service ${view.id}`}
            onClick={() => setMode("revoke")}
          >
            Revoke
          </Button>
        </div>
      )}
    </li>
  );
}

/** What was done to external services, newest first. Actions only: what a read made of them is each state. */
export function ServiceHistory() {
  const history = useServiceHistory(true);
  if (history.error) return <p className={classes.banner} data-tone="danger">{history.error.message}</p>;
  if (!history.data) {
    return (
      <p className={classes.detail}>
        <Loader size={12} type="dots" /> Reading the audit trail of external services
      </p>
    );
  }
  const entries = history.data.entries ?? [];
  return (
    <section className={classes.panel} aria-label="Audit of external services">
      <p className={classes.basis}>{history.data.basis}</p>
      {entries.length === 0 ? (
        <p className={classes.detail}>No external service has been classified, changed or revoked.</p>
      ) : (
        <ul className={classes.history}>
          {entries.map((entry) => (
            <li key={entry.id} className={classes.entry}>
              <p className={classes.detail}>
                <span className={classes.tag}>{absoluteTime(entry.at)}</span> {serviceHistoryLine(entry)}{" "}
                <span className={classes.tag}>via {entry.via}</span>
              </p>
              {entry.note && <p className={classes.detail}>“{entry.note}”</p>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
