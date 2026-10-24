"use client";

import { Button, Loader, TextInput } from "@mantine/core";
import { useState } from "react";

import { useDeclarations, useHistory } from "@/lib/api/connections";
import type { DeclarationView } from "@/lib/api/generated";
import {
  declarationDetail,
  historyLine,
  REASON_TEXT,
  scopeSummary,
  STATE_LABEL,
  STATE_MEANING,
} from "@/lib/model/connections";
import { absoluteTime } from "@/lib/model/format";

import classes from "./Connections.module.css";

/** The operator's own declarations, each with where it stands against the last sync. */
export function Declarations({ declarations, synced }: { declarations: DeclarationView[]; synced: boolean }) {
  return (
    <section className={classes.panel} aria-label="Your declarations">
      <p className={classes.basis}>
        A declaration is your statement about which service answers at an origin. It is stored apart from the
        index, survives re-indexing, and takes effect at the next sync.{" "}
        {synced ? "A declaration takes effect at the next sync after it is stated." : "No sync has read declarations yet."}
      </p>
      {declarations.length === 0 ? (
        <p className={classes.detail}>You have declared nothing.</p>
      ) : (
        <ul className={classes.declarations}>
          {declarations.map((d) => (
            <Declaration key={d.id} declaration={d} />
          ))}
        </ul>
      )}
    </section>
  );
}

function Declaration({ declaration: d }: { declaration: DeclarationView }) {
  const [revoking, setRevoking] = useState(false);
  const [note, setNote] = useState("");
  const { revoke } = useDeclarations();
  const detail = declarationDetail(d);
  return (
    <li className={classes.declaration} aria-label={`Declaration ${d.id}`}>
      <div className={classes.groupHead}>
        <span className={classes.origin}>
          #{d.id} {d.repository}
        </span>
        <span className={classes.state} data-state={d.state} title={STATE_MEANING[d.state]}>
          {STATE_LABEL[d.state]}
        </span>
        <span className={classes.tag}>{d.verification === "user_confirmed" ? "confirmed by you" : "unconfirmed"}</span>
        {d.environment && <span className={classes.tag}>{d.environment}</span>}
      </div>
      {d.url && <p className={classes.mono}>{d.url}</p>}
      {d.scope && (
        <p className={classes.mono}>Scope: {scopeSummary(d.scope).join(" · ")}</p>
      )}
      <p className={classes.detail}>
        {d.label}
        {(d.callers ?? []).length > 0 ? ` · for ${d.callers!.join(", ")}` : " · for every caller"}
      </p>
      {d.reason && <p className={classes.detail}>{REASON_TEXT[d.reason] ?? d.reason}</p>}
      {detail && <p className={classes.detail}>{detail}</p>}
      {revoking ? (
        <form
          className={classes.mapper}
          aria-label={`Revoke declaration ${d.id}`}
          onSubmit={(event) => {
            event.preventDefault();
            revoke.mutate({ repository: d.repository, id: d.id, note: note.trim() || null });
          }}
        >
          <TextInput
            label="Why (kept in the history)"
            maxLength={500}
            value={note}
            onChange={(event) => setNote(event.currentTarget.value)}
          />
          <p className={classes.detail}>
            Revoking restores what the source alone decided, at the next sync. The history keeps this
            declaration.
          </p>
          <div className={classes.actions}>
            <Button type="submit" size="compact-sm" color="red" loading={revoke.isPending}>
              Revoke declaration {d.id}
            </Button>
            <Button type="button" size="compact-sm" variant="subtle" onClick={() => setRevoking(false)}>
              Cancel
            </Button>
          </div>
          {revoke.error && (
            <p className={classes.banner} data-tone="danger" role="alert">
              {revoke.error.message}
            </p>
          )}
        </form>
      ) : (
        <div className={classes.actions}>
          <Button size="compact-xs" variant="subtle" color="red" aria-label={`Revoke ${d.id}`} onClick={() => setRevoking(true)}>
            Revoke
          </Button>
        </div>
      )}
    </li>
  );
}

/** What was done to declarations, newest first. Actions only: what a sync made of them is each state. */
export function History() {
  const history = useHistory(true);
  if (history.error) return <p className={classes.banner} data-tone="danger">{history.error.message}</p>;
  if (!history.data) {
    return (
      <p className={classes.detail}>
        <Loader size={12} type="dots" /> Reading the history
      </p>
    );
  }
  const entries = history.data.entries ?? [];
  return (
    <section className={classes.panel} aria-label="History">
      <p className={classes.basis}>{history.data.basis}</p>
      {entries.length === 0 ? (
        <p className={classes.detail}>Nothing has been declared, changed or revoked.</p>
      ) : (
        <ul className={classes.history}>
          {entries.map((entry) => (
            <li key={entry.id} className={classes.entry}>
              <p className={classes.detail}>
                <span className={classes.tag}>{absoluteTime(entry.at)}</span> {historyLine(entry)}{" "}
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
