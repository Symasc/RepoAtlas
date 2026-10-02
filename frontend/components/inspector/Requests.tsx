"use client";

import { Button, Loader, Switch } from "@mantine/core";
import { useState } from "react";

import { useDecision, useHiddenSummary, useRequests } from "@/lib/api/requests";
import { formatCount, plural } from "@/lib/model/format";
import {
  byFile,
  DECISION_MEANING,
  decisionCounts,
  type Crossing,
  type HopPlace,
  type RequestRow,
  type RequestsScope,
} from "@/lib/model/decisions";

import { Quiet } from "./Inspector";
import inspectorClasses from "./Inspector.module.css";
import classes from "./Requests.module.css";
import { Stepper } from "./Stepper";

/**
 * The HTTP requests a repository, directory or file makes, each with the workspace
 * join's stored decisions: linked or refused, by exact status, with the chain each
 * rests on. Rows only the join reads (`join_only`) are counted, not listed, until
 * asked for. Read in repository scope with no evidence allocated; a decision's
 * lines are read only when its evidence is opened.
 */
export function Requests({
  scope,
  subject,
  onOpenPlace,
}: {
  scope: RequestsScope;
  /** The thing inspected: a decision's evidence is recorded in its investigation. */
  subject: string;
  onOpenPlace?: (place: HopPlace) => void;
}) {
  const listed = useRequests(scope);
  const hiddenSummary = useHiddenSummary(scope);
  const [showHidden, setShowHidden] = useState(false);
  const hidden = useRequests(scope, true, showHidden);

  if (listed.error || hiddenSummary.error) {
    return <p className={inspectorClasses.problem}>{(listed.error ?? hiddenSummary.error)!.message}</p>;
  }
  if (!listed.data || !hiddenSummary.data) {
    return (
      <p className={inspectorClasses.loading}>
        <Loader size={12} type="dots" /> Reading the requests
      </p>
    );
  }
  const first = listed.data.pages[0]?.response;
  const rows = listed.data.pages.flatMap((page) => page.rows);
  const listedTotal = first?.summary.total ?? 0;
  const hiddenTotal = hiddenSummary.data.total ?? 0;
  const counts = decisionCounts(first?.summary.by_crossing, hiddenSummary.data.by_crossing);
  const built = first?.basis.crossings.built ?? true;

  return (
    <div className={classes.requests}>
      {!built && (
        <Quiet>No link pass has stored the workspace join&apos;s decisions in this index yet; sync to store them.</Quiet>
      )}
      {counts.length > 0 && (
        <section className={inspectorClasses.section} aria-label="Decisions">
          <h3 className={inspectorClasses.sectionTitle}>Decisions</h3>
          <ul className={classes.counts}>
            {counts.map((c) => (
              <li key={c.status} className={classes.count} title={DECISION_MEANING[c.status]}>
                <span className={classes.decision} data-status={c.status}>
                  {c.status}
                </span>
                <span className={classes.countValue}>{formatCount(c.shown + c.hidden)}</span>
                {c.hidden > 0 && <span className={classes.countHidden}>{formatCount(c.hidden)} on join-only requests</span>}
              </li>
            ))}
          </ul>
        </section>
      )}
      <section className={inspectorClasses.section} aria-label="Requests">
        <h3 className={inspectorClasses.sectionTitle}>
          Requests <span className={inspectorClasses.sectionCount}>{formatCount(listedTotal)}</span>
        </h3>
        {rows.length === 0 ? (
          <Quiet>
            {hiddenTotal > 0
              ? `No request here has a route-matching row. ${plural(hiddenTotal, "request")} only the join reads ${hiddenTotal === 1 ? "is" : "are"} counted above.`
              : "No HTTP request is indexed here."}
          </Quiet>
        ) : (
          <RowGroups repository={scope.repository} rows={rows} subject={subject} onOpenPlace={onOpenPlace} />
        )}
        {listed.hasNextPage && (
          <Button size="compact-xs" variant="subtle" onClick={() => listed.fetchNextPage()} loading={listed.isFetchingNextPage}>
            Load more
          </Button>
        )}
      </section>
      {hiddenTotal > 0 && (
        <section className={inspectorClasses.section} aria-label="Join-only requests">
          <Switch
            size="xs"
            checked={showHidden}
            onChange={(event) => setShowHidden(event.currentTarget.checked)}
            label={`Show join-only requests (${formatCount(hiddenTotal)})`}
          />
          <Quiet>Requests only the workspace join reads: an absolute or deployment-addressed URL, or a host the source does not state. Not route matches, and not unresolved paths.</Quiet>
          {showHidden &&
            (hidden.error ? (
              <p className={inspectorClasses.problem}>{hidden.error.message}</p>
            ) : !hidden.data ? (
              <p className={inspectorClasses.loading}>
                <Loader size={12} type="dots" /> Reading the join-only requests
              </p>
            ) : (
              <>
                <RowGroups
                  repository={scope.repository}
                  rows={hidden.data.pages.flatMap((page) => page.rows)}
                  subject={subject}
                  onOpenPlace={onOpenPlace}
                />
                {hidden.hasNextPage && (
                  <Button size="compact-xs" variant="subtle" onClick={() => hidden.fetchNextPage()} loading={hidden.isFetchingNextPage}>
                    Load more
                  </Button>
                )}
              </>
            ))}
        </section>
      )}
    </div>
  );
}

function RowGroups({
  repository,
  rows,
  subject,
  onOpenPlace,
}: {
  repository: string;
  rows: RequestRow[];
  subject: string;
  onOpenPlace?: (place: HopPlace) => void;
}) {
  return (
    <div className={classes.groups}>
      {byFile(rows).map((group) => (
        <div key={group.path} className={classes.group}>
          <h4 className={classes.file}>{group.path || "(no file)"}</h4>
          <ul className={classes.rows}>
            {group.rows.map((row, i) => (
              <RequestItem
                key={`${row.line_start ?? "-"}:${row.method ?? ""}:${row.route ?? ""}:${i}`}
                repository={repository}
                row={row}
                subject={subject}
                onOpenPlace={onOpenPlace}
              />
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

function RequestItem({
  repository,
  row,
  subject,
  onOpenPlace,
}: {
  repository: string;
  row: RequestRow;
  subject: string;
  onOpenPlace?: (place: HopPlace) => void;
}) {
  const crossings = row.crossings ?? [];
  return (
    <li className={classes.request} data-status={row.status}>
      <p className={classes.requestLine}>
        {row.method && <span className={classes.method}>{row.method}</span>}
        <span className={classes.route}>{row.route || "(route not read)"}</span>
        <span className={classes.rowStatus}>{row.status}</span>
      </p>
      {row.path && (
        <p className={classes.requestAt}>
          {onOpenPlace && row.line_start != null ? (
            <button
              type="button"
              className={classes.placeLink}
              onClick={() => onOpenPlace({ repository, path: row.path!, line_start: row.line_start, role: "called_at" })}
            >
              line {row.line_start}
            </button>
          ) : (
            row.line_start != null && <span>line {row.line_start}</span>
          )}
          {row.detail && <span className={classes.rowDetail}>{row.detail}</span>}
        </p>
      )}
      {crossings.length === 0 ? (
        <p className={classes.noDecision}>No stored decision for this request.</p>
      ) : (
        crossings.map((crossing) => <CrossingItem key={crossing.decision_ref} crossing={crossing} subject={subject} onOpenPlace={onOpenPlace} />)
      )}
    </li>
  );
}

function CrossingItem({
  crossing,
  subject,
  onOpenPlace,
}: {
  crossing: Crossing;
  subject: string;
  onOpenPlace?: (place: HopPlace) => void;
}) {
  const [detailed, setDetailed] = useState(false);
  return (
    <div className={classes.crossing} data-decision={crossing.decision_ref}>
      <CrossingHead crossing={crossing} />
      {detailed ? (
        <DecisionEvidence decisionRef={crossing.decision_ref} subject={subject} onOpenPlace={onOpenPlace} />
      ) : (
        <Stepper crossing={crossing} onOpenPlace={onOpenPlace} />
      )}
      <Button size="compact-xs" variant="subtle" onClick={() => setDetailed((d) => !d)} aria-pressed={detailed}>
        {detailed ? "Hide evidence" : "Evidence"}
      </Button>
    </div>
  );
}

function CrossingHead({ crossing }: { crossing: Crossing }) {
  return (
    <div className={classes.crossingHead}>
      <p className={classes.crossingLine}>
        <span className={classes.decision} data-status={crossing.status}>
          {crossing.status}
        </span>
        {crossing.url && (
          <span className={classes.url}>
            {crossing.method ? `${crossing.method} ` : ""}
            {crossing.url}
          </span>
        )}
      </p>
      <p className={classes.meaning}>{DECISION_MEANING[crossing.status]}</p>
      {crossing.variant && <p className={classes.variant}>{crossing.variant}</p>}
    </div>
  );
}

/**
 * One stored decision read whole, by its exact ref: global scope, every
 * repository's part of the chain shown, and the lines of each proven hop. Fetched
 * only when this is opened.
 */
export function DecisionEvidence({
  decisionRef,
  subject,
  onOpenPlace,
  withHead = false,
}: {
  decisionRef: string;
  subject: string;
  onOpenPlace?: (place: HopPlace) => void;
  /** Whether to say which decision this is, where nothing around it does. */
  withHead?: boolean;
}) {
  const decision = useDecision(decisionRef, subject);
  if (decision.error) return <p className={inspectorClasses.problem}>{decision.error.message}</p>;
  if (!decision.data) {
    return (
      <p className={inspectorClasses.loading}>
        <Loader size={12} type="dots" /> Reading the decision and its evidence
      </p>
    );
  }
  const found = decision.data.found;
  if (found === null) {
    return <p className={inspectorClasses.problem}>This decision is no longer stored: the index changed since this view was read.</p>;
  }
  return (
    <div className={classes.detailed} data-decision={decisionRef}>
      {withHead && <CrossingHead crossing={found.crossing} />}
      <Stepper crossing={found.crossing} evidence={decision.data.evidence} onOpenPlace={onOpenPlace} />
    </div>
  );
}
