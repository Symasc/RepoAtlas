"use client";

import type { PreviewProviderDiagnostics, UnpublishedMatch } from "@/lib/api/generated";
import {
  absenceLine,
  boundLines,
  categoryLines,
  codeTags,
  EVIDENCE_LABEL,
  HINT_HEADING,
  HINT_LABEL,
  hintParts,
  HYPOTHESIS_NOTE,
  moreHintsLine,
  notExtractedLines,
  NOT_PROVEN,
  reachLines,
  summaryLine,
  truncationLine,
} from "@/lib/model/providerEvidence";

import classes from "./Connections.module.css";

/**
 * What the preview knows of the chosen repository's routes beyond the ones it serves: how many
 * route declarations the index recognized and what became of them, why the rest are not served, and
 * which limits were reached. Words only (see lib/model/providerEvidence.ts): it changes no outcome,
 * count or decision of the preview, and carries no action.
 */
export function ProviderEvidence({
  diagnostics,
  repository,
}: {
  diagnostics: PreviewProviderDiagnostics;
  repository: string;
}) {
  const lifecycle = diagnostics.lifecycle;
  const categories = categoryLines(lifecycle);
  const codes = codeTags(lifecycle);
  const absence = absenceLine(repository, lifecycle);
  const unread = absence ? notExtractedLines(lifecycle) : [];
  const truncation = truncationLine(repository, lifecycle);
  const bounds = boundLines(lifecycle);
  const reach = reachLines(repository, diagnostics);
  const more = moreHintsLine(diagnostics);

  return (
    <section className={classes.preview} aria-label={EVIDENCE_LABEL}>
      <p className={classes.detail}>{summaryLine(repository, lifecycle)}</p>
      {categories.length > 0 && (
        <ul className={classes.reasons} aria-label="Recognized routes by reason">
          {categories.map((line) => (
            <li key={line.category}>
              {line.text} <span className={classes.tag}>{line.category}</span>
            </li>
          ))}
        </ul>
      )}
      {codes.length > 0 && (
        <p className={classes.requestLine}>
          <span className={classes.detail}>Recorded as</span>
          {codes.map(({ code, count }) => (
            <span key={code} className={classes.tag}>
              {code} {count}
            </span>
          ))}
        </p>
      )}
      {absence && <p className={classes.detail}>{absence}</p>}
      {unread.length > 0 && (
        <ul className={classes.reasons} aria-label="What RepoAtlas does not read">
          {unread.map((item) => (
            <li key={item.code}>
              <span className={classes.tag}>{item.code}</span> {item.note ?? ""}
            </li>
          ))}
        </ul>
      )}
      {truncation && <p className={classes.detail}>{truncation}</p>}
      {bounds.length > 0 && (
        <ul className={classes.reasons} aria-label="Analysis limits reached">
          {bounds.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}
      {reach.map((line) => (
        <p key={line} className={classes.detail}>
          {line}
        </p>
      ))}
      {more && <p className={classes.detail}>{more}</p>}
    </section>
  );
}

/**
 * Under one preview row: the routes the repository declares or recognizes, does not serve, and the
 * request resembles. They are hypotheses: styled apart (dashed, never the success colour), labelled
 * "not proven", and given no link and no action. A request with none says only that this index
 * recognizes no route that matches it, which is not proof the repository has none.
 */
export function RowHints({ matches, absence }: { matches: readonly UnpublishedMatch[]; absence: string | null }) {
  if (matches.length === 0 && !absence) return null;
  return (
    <div className={classes.hintBlock}>
      {matches.length > 0 && (
        <div className={classes.hypothesis} data-hypothesis="true">
          <p className={classes.detail}>
            <strong>{HINT_HEADING}</strong>
          </p>
          <ul className={classes.reasons} aria-label={HINT_LABEL}>
            {matches.map((match) => {
              const parts = hintParts(match);
              return (
                <li key={`${match.route}\u0000${match.path ?? ""}\u0000${match.line ?? ""}`}>
                  <span className={classes.tag}>{NOT_PROVEN}</span> <span className={classes.route}>{parts.route}</span>
                  {parts.place && <span className={classes.place}> {parts.place}</span>} {parts.status}; {parts.category}
                  {parts.code && (
                    <>
                      {" "}
                      <span className={classes.tag}>{parts.code}</span>
                    </>
                  )}
                  {parts.detail && <> {parts.detail}</>}
                </li>
              );
            })}
          </ul>
          <p className={classes.detail}>{HYPOTHESIS_NOTE}</p>
        </div>
      )}
      {absence && <p className={classes.detail}>{absence}</p>}
    </div>
  );
}
