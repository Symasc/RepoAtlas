"use client";

import type { ConnectionRequest } from "@/lib/api/generated";
import { refusalText, SCOPE_REFUSAL_TEXT } from "@/lib/model/connections";

import classes from "./Connections.module.css";

/** The requests of a group, each with its method, route, reason and where it is written. */
export function RequestRows({
  requests,
  onOpenPlace,
}: {
  requests: ConnectionRequest[];
  onOpenPlace?: (place: { repository: string; path: string; line_start?: number | null }) => void;
}) {
  return (
    <ul className={classes.requests} aria-label="Requests">
      {requests.map((request) => (
        <li key={request.decision_ref} className={classes.request} data-status={request.status}>
          <p className={classes.requestLine}>
            {request.method && <span className={classes.method}>{request.method}</span>}
            <span className={classes.route}>{request.route || "(path not read)"}</span>
            <span className={classes.tag}>{request.status}</span>
            {request.joined_by != null && (
              <span className={classes.tag} data-tone="success">
                joined by declaration #{request.joined_by}
              </span>
            )}
          </p>
          <p className={classes.place}>
            <span>{request.site.repository}</span>
            {onOpenPlace && request.site.line_start != null ? (
              <button
                type="button"
                className={classes.placeLink}
                aria-label={`Open ${request.site.path} line ${request.site.line_start}`}
                onClick={() => onOpenPlace(request.site)}
              >
                {request.site.path}:{request.site.line_start}
              </button>
            ) : (
              <span>
                {request.site.path}
                {request.site.line_start != null ? `:${request.site.line_start}` : ""}
              </span>
            )}
            <span>{request.language}</span>
          </p>
          {request.reason && (
            <p className={classes.detail}>
              <span className={classes.tag}>{request.reason}</span> {refusalText(request.reason)}
            </p>
          )}
          {request.scopable && (
            <p className={classes.detail}>
              <span className={classes.tag}>can be scoped</span>
              {request.symbol ? (
                <>
                  {" "}
                  written in <span className={classes.mono}>{request.symbol}</span>
                </>
              ) : (
                " written at the file's module level"
              )}
            </p>
          )}
          {!request.scopable && request.scope_refusal && request.scope_refusal !== "not-base-unknown" && (
            <p className={classes.detail}>
              <span className={classes.tag}>{request.scope_refusal}</span> {SCOPE_REFUSAL_TEXT[request.scope_refusal] ?? ""}
            </p>
          )}
          {request.declaration_outcome && request.status !== "linked" && (
            <p className={classes.detail}>
              Your declaration {(request.declaration_ids ?? []).map((id) => `#${id}`).join(", ")} did not join it:{" "}
              <span className={classes.tag}>{request.declaration_outcome}</span>
            </p>
          )}
        </li>
      ))}
    </ul>
  );
}
