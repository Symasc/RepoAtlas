"use client";

import { ActionIcon, Button, CopyButton, Tooltip } from "@mantine/core";
import { ArrowClockwiseIcon, CheckIcon, CopyIcon, PlugsIcon, WarningCircleIcon } from "@phosphor-icons/react";

import { Inline } from "@/components/common/Inline";
import type { ApiError } from "@/lib/api/client";

import classes from "./ApiState.module.css";

interface Guidance {
  title: string;
  body: string;
  command?: string;
}

/** What to do about each way of not reaching the index, in the interface's words. */
function guidance(error: ApiError): Guidance {
  switch (error.code) {
    case "API_NOT_CONFIGURED":
      return {
        title: "Connect the explorer to RepoAtlas",
        body: "This explorer was started without a connection to RepoAtlas. Stop it and start both together, or start `repoatlas serve` and restart the explorer with `REPOATLAS_API_CONNECTION_FILE` set to the file it reports.",
        command: "repoatlas ui",
      };
    case "API_NOT_RUNNING":
      return {
        title: "RepoAtlas isn't serving",
        body: "The local API isn't running. Start it in a terminal, then try again.",
        command: "repoatlas serve",
      };
    case "API_UNREACHABLE":
      return {
        title: "RepoAtlas stopped answering",
        body: "The local API that wrote the connection file is no longer running. Start it again, then try again.",
        command: "repoatlas serve",
      };
    case "API_CONNECTION_INVALID":
      return {
        title: "The connection file can't be used",
        body: error.error.detail ?? error.message,
        command: "repoatlas serve",
      };
    default:
      return {
        title: "RepoAtlas couldn't answer",
        body: error.error.detail ? `${error.message} ${error.error.detail}` : error.message,
      };
  }
}

export function ApiState({
  error,
  retrying,
  onRetry,
}: {
  error: ApiError;
  retrying: boolean;
  onRetry: () => void;
}) {
  const { title, body, command } = guidance(error);
  const Icon = error.unavailable ? PlugsIcon : WarningCircleIcon;
  return (
    <div className={classes.panel} role="alert">
      <Icon size={22} className={classes.icon} aria-hidden />
      <h1 className={classes.title}>{title}</h1>
      <p className={classes.body}>
        <Inline text={body} />
      </p>
      {command !== undefined && (
        <div className={classes.command}>
          <code>{command}</code>
          <CopyButton value={command}>
            {({ copied, copy }) => (
              <Tooltip label={copied ? "Copied" : "Copy command"}>
                <ActionIcon size="md" onClick={copy} aria-label="Copy command">
                  {copied ? <CheckIcon size={15} /> : <CopyIcon size={15} />}
                </ActionIcon>
              </Tooltip>
            )}
          </CopyButton>
        </div>
      )}
      <Button
        variant="default"
        leftSection={<ArrowClockwiseIcon size={15} />}
        loading={retrying}
        onClick={onRetry}
        className={classes.retry}
      >
        Try again
      </Button>
      <p className={classes.code}>{error.code}</p>
    </div>
  );
}
