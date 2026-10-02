"use client";

import { Button, Loader } from "@mantine/core";
import { ArrowDownIcon, ArrowRightIcon, CodeIcon } from "@phosphor-icons/react";

import { LineGlyph } from "@/components/graph/glyphs";
import { ApiError } from "@/lib/api/client";
import type { EvidenceItem, TopologyEdgeResponse } from "@/lib/api/generated";
import { useEdges } from "@/lib/api/topology";
import { decisionRefOf, type HopPlace } from "@/lib/model/decisions";
import { formatCount, plural } from "@/lib/model/format";
import { relationOf, type Connection, type Family } from "@/lib/model/graph";

import { Quiet } from "./Inspector";
import classes from "./Inspector.module.css";
import { DecisionEvidence } from "./Requests";

export interface LineSpan {
  start: number;
  end: number;
}

/**
 * Why a connection is there: each example the index keeps for it, read back as
 * the exact stored relationship, where it is asserted, and the lines that say so.
 */
export function Evidence({
  connection,
  subject,
  here,
  onShowInCode,
  onOpenPlace,
}: {
  connection: Connection | null;
  /** The thing inspected: its evidence is recorded in one investigation. */
  subject: string;
  /** The file inspected, if one is: its own lines can be shown in Code. */
  here?: { repository: string; path: string };
  onShowInCode?: (span: LineSpan) => void;
  /** Opens a place a handler link's decision cites. */
  onOpenPlace?: (place: HopPlace) => void;
}) {
  const refs = connection?.link.examples ?? [];
  const results = useEdges(refs, subject);
  if (connection === null) {
    return <Quiet>Choose a connection to read the lines that assert it.</Quiet>;
  }
  const { link, partner, direction } = connection;
  return (
    <div className={classes.evidence}>
      <div className={classes.evidenceHead}>
        <LineGlyph family={link.family} width={20} />
        <span className={classes.evidenceTitle}>
          {direction === "outgoing" ? "Uses" : "Used by"} <strong>{partner.label}</strong>
        </span>
        <span className={classes.familyCount}>{plural(link.count, "relationship")}</span>
      </div>
      {link.types.length > 0 && (
        <p className={classes.typeLine}>
          {link.types.map((t) => (
            <span key={t.type} className={classes.typeItem}>
              {t.type.toLowerCase().replace(/_/g, " ")} <span>{formatCount(t.count)}</span>
            </span>
          ))}
        </p>
      )}
      {link.packages.length > 0 && <p className={classes.typeLine}>Packages: {link.packages.join(", ")}</p>}
      {refs.length === 0 ? (
        <Quiet>The index keeps no example of this link to show.</Quiet>
      ) : (
        <ol className={classes.examples}>
          {results.map((result, i) => (
            <li key={refs[i]}>
              {result.data ? (
                <EdgeCard
                  edge={result.data}
                  family={link.family}
                  here={here}
                  subject={subject}
                  onShowInCode={onShowInCode}
                  onOpenPlace={onOpenPlace}
                />
              ) : result.error ? (
                <p className={classes.problem}>
                  {result.error instanceof ApiError && result.error.code === "EDGE_NOT_FOUND"
                    ? "This relationship is no longer in the index; it changed since this view was read."
                    : result.error.message}
                </p>
              ) : (
                <p className={classes.loading}>
                  <Loader size={12} type="dots" /> Reading the relationship
                </p>
              )}
            </li>
          ))}
        </ol>
      )}
      {link.count > refs.length && refs.length > 0 && (
        <Quiet>
          Showing {formatCount(refs.length)} {refs.length === 1 ? "example" : "examples"} of {plural(link.count, "relationship")}.
        </Quiet>
      )}
    </div>
  );
}

function EdgeCard({
  edge,
  family,
  here,
  subject,
  onShowInCode,
  onOpenPlace,
}: {
  edge: TopologyEdgeResponse;
  family: Family;
  here?: { repository: string; path: string };
  subject: string;
  onShowInCode?: (span: LineSpan) => void;
  onOpenPlace?: (place: HopPlace) => void;
}) {
  const decisionRef = edge.edge.type === "CALLS_HTTP_HANDLER" ? decisionRefOf(edge) : null;
  const { source, target, type, provenance, confidence } = edge.edge;
  const at = provenance?.path
    ? `${provenance.repository_slug ?? ""}/${provenance.path}${provenance.line_start ? `:${provenance.line_start}${provenance.line_end && provenance.line_end !== provenance.line_start ? `–${provenance.line_end}` : ""}` : ""}`
    : null;
  const inHere =
    here !== undefined &&
    provenance?.repository_slug === here.repository &&
    provenance.path === here.path &&
    provenance.line_start != null;
  return (
    <article className={classes.edgeCard}>
      <p className={classes.edgeLine}>
        <span className={classes.edgeEnd}>{source.name || source.key}</span>
        <span className={classes.edgeType}>
          <LineGlyph family={family} relation={relationOf(family, type)} width={12} />
          {type.toLowerCase().replace(/_/g, " ")}
          <ArrowRightIcon size={11} weight="bold" aria-hidden />
        </span>
        <span className={classes.edgeEnd}>{target.name || target.key}</span>
      </p>
      {at && (
        <p className={classes.edgeAt}>
          {at}
          {provenance?.parser && <span> · {provenance.parser}</span>}
          {edge.multiplicity > 1 && <span> · stated {edge.multiplicity} times</span>}
          {confidence !== undefined && confidence < 1 && <span> · confidence {confidence}</span>}
        </p>
      )}
      {(edge.evidence ?? []).map((item) => (
        <Snippet key={item.id} item={item} />
      ))}
      {edge.crossing && (
        <dl className={classes.crossing}>
          <dt>Package</dt>
          <dd>{edge.crossing.package}{edge.crossing.subpath ? `/${edge.crossing.subpath}` : ""}</dd>
          <dt>Provided by</dt>
          <dd>{edge.crossing.provided_by}</dd>
          <dt>Declared by</dt>
          <dd>
            {edge.crossing.declared_by}
            {edge.crossing.constraint ? ` (${edge.crossing.constraint})` : ""}
          </dd>
          {edge.crossing.imported_at && (
            <>
              <dt>Imported at</dt>
              <dd>{edge.crossing.imported_at}</dd>
            </>
          )}
        </dl>
      )}
      {edge.http && (
        <dl className={classes.crossing}>
          <dt>Request</dt>
          <dd>
            {edge.http.method} {edge.http.url}
          </dd>
          <dt>Called at</dt>
          <dd>{edge.http.called_at}</dd>
          <dt>Served at</dt>
          <dd>{edge.http.served_at}</dd>
          <dt>Endpoint</dt>
          <dd>{edge.http.endpoint}</dd>
        </dl>
      )}
      {edge.edge.type === "CALLS_HTTP_HANDLER" &&
        (decisionRef ? (
          <section className={classes.decision} aria-label="The stored decision">
            <p className={classes.decisionTitle}>
              Decision <span className={classes.mono}>{decisionRef}</span>
            </p>
            <DecisionEvidence decisionRef={decisionRef} subject={subject} onOpenPlace={onOpenPlace} withHead />
          </section>
        ) : (
          <Quiet>This link names no stored decision; a sync records one.</Quiet>
        ))}
      {inHere && onShowInCode && (
        <Button
          size="compact-xs"
          variant="subtle"
          leftSection={<CodeIcon size={13} />}
          rightSection={<ArrowDownIcon size={11} />}
          onClick={() =>
            onShowInCode({ start: provenance!.line_start!, end: provenance!.line_end ?? provenance!.line_start! })
          }
        >
          Show in code
        </Button>
      )}
    </article>
  );
}

/** Lines the index supplied, numbered as they are in the file. */
function Snippet({ item }: { item: EvidenceItem }) {
  const start = item.ref.line_start ?? null;
  const lines = (item.text ?? "").replace(/\n$/, "").split("\n");
  if (item.declaration || start === null) {
    return <pre className={classes.snippet}>{item.text}</pre>;
  }
  return (
    <pre className={classes.snippet}>
      {lines.map((line, i) => (
        <span key={i} className={classes.snippetLine}>
          <span className={classes.lineNumber}>{start + i}</span>
          <span>{line || " "}</span>
        </span>
      ))}
    </pre>
  );
}
