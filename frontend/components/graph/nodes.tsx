"use client";

import {
  CaretRightIcon,
  CubeIcon,
  FileIcon,
  FolderSimpleIcon,
  GearSixIcon,
  GlobeSimpleIcon,
  PackageIcon,
  PuzzlePieceIcon,
  TerminalWindowIcon,
  CurrencyDollarSimpleIcon,
  type Icon,
} from "@phosphor-icons/react";
import { Handle, Position, type Node, type NodeProps, type NodeTypes } from "@xyflow/react";
import { memo, type CSSProperties } from "react";

import type { ChildEntry } from "@/lib/api/generated";
import { formatCount, plural, shortSha } from "@/lib/model/format";
import type { FreshnessModel } from "@/lib/model/freshness";
import type { AwayNode, DirectoryCard, FilesCard, PeerNode, SharedNode } from "@/lib/model/graph";
import type { RepositoryModel } from "@/lib/model/repositories";

import classes from "./Graph.module.css";

/** What a node can ask the view to do; the view owns navigation. */
export interface Actions {
  openRepository: (slug: string) => void;
  openPath: (path: string) => void;
}

export type RepositoryNodeData = {
  repository: RepositoryModel;
  /** At the top of an opened repository, it names where you are. */
  current: boolean;
  actions: Actions;
};
export type PathNodeData = { name: string; path: string; files: number | null; current: boolean; actions: Actions };
export type DirectoryNodeData = { card: DirectoryCard; actions: Actions };
export type FilesNodeData = { card: FilesCard };
export type SharedNodeData = { node: SharedNode };
export type PeerNodeData = { node: PeerNode; hue: number | null; actions: Actions };
export type AwayNodeData = { node: AwayNode; repository: string; actions: Actions };

export type GraphNode =
  | Node<RepositoryNodeData, "repository">
  | Node<PathNodeData, "path">
  | Node<DirectoryNodeData, "directory">
  | Node<FilesNodeData, "files">
  | Node<SharedNodeData, "shared">
  | Node<PeerNodeData, "peer">
  | Node<AwayNodeData, "away">;

/** Invisible anchors on every side: an edge picks the sides that face its other end. */
function Anchors() {
  const sides = [
    ["top", Position.Top],
    ["right", Position.Right],
    ["bottom", Position.Bottom],
    ["left", Position.Left],
  ] as const;
  return (
    <>
      {sides.map(([side, position]) => (
        <Handle key={`s-${side}`} id={`s-${side}`} type="source" position={position} className={classes.anchor} isConnectable={false} />
      ))}
      {sides.map(([side, position]) => (
        <Handle key={`t-${side}`} id={`t-${side}`} type="target" position={position} className={classes.anchor} isConnectable={false} />
      ))}
    </>
  );
}

function hueStyle(hue: number): CSSProperties {
  return { "--hue": `var(--ra-repo-${hue})` } as CSSProperties;
}

/**
 * Formats, configuration and shell glue say little about what a repository is
 * written in; they are listed only when nothing else is.
 */
const NOT_LANGUAGES = new Set([
  "JSON", "YAML", "TOML", "Markdown", "XML", "INI", "Dockerfile", "Jinja", "Salt", "Bash", "Shell", "Makefile",
]);

function languagesOf(repository: RepositoryModel): string {
  const code = repository.languages.filter((l) => !NOT_LANGUAGES.has(l));
  const list = code.length > 0 ? code : repository.languages;
  const shown = list.slice(0, 3).join(", ");
  return list.length > 3 ? `${shown} +${list.length - 3}` : shown;
}

function FreshnessDot({ freshness }: { freshness: FreshnessModel }) {
  return (
    <span className={classes.freshness} data-tone={freshness.tone} title={freshness.label}>
      <span className={classes.srOnly}>{freshness.label}</span>
    </span>
  );
}

function RepositoryNode({ data }: NodeProps<Node<RepositoryNodeData, "repository">>) {
  const { repository: r, current, actions } = data;
  return (
    <div className={classes.repository} data-current={current || undefined} style={hueStyle(r.hue)}>
      <Anchors />
      <button
        type="button"
        className={classes.cover}
        onClick={() => actions.openRepository(r.slug)}
        aria-label={current ? `${r.name}, repository root` : `Open ${r.name}`}
      />
      <span className={classes.badge} aria-hidden>
        {r.name.slice(0, 1).toUpperCase()}
      </span>
      <span className={classes.body}>
        <span className={classes.titleLine}>
          <span className={classes.name}>{r.name}</span>
          <FreshnessDot freshness={r.freshness} />
        </span>
        {current ? (
          <span className={classes.meta}>
            {r.branch ?? "detached"}
            {r.headSha && <span className={classes.sha}>{shortSha(r.headSha)}</span>}
            <span className={classes.state}>{r.freshness.label}</span>
          </span>
        ) : (
          <>
            <span className={classes.meta}>{languagesOf(r)}</span>
            <span className={classes.stats}>
              <span>{plural(r.files, "file")}</span>
              <span>{plural(r.symbols, "symbol")}</span>
            </span>
          </>
        )}
      </span>
    </div>
  );
}

function PathNode({ data }: NodeProps<Node<PathNodeData, "path">>) {
  return (
    <div className={classes.path} data-current={data.current || undefined}>
      <Anchors />
      <button
        type="button"
        className={classes.cover}
        onClick={() => data.actions.openPath(data.path)}
        aria-label={data.current ? `${data.name}, current directory` : `Go to ${data.name}`}
      />
      <FolderSimpleIcon size={20} weight="fill" className={classes.folder} aria-hidden />
      <span className={classes.body}>
        <span className={classes.name}>{data.name}</span>
        {data.files !== null && <span className={classes.meta}>{plural(data.files, "file")}</span>}
      </span>
    </div>
  );
}

function EntryRow({ entry, onOpen }: { entry: ChildEntry; onOpen?: (path: string) => void }) {
  const directory = entry.kind === "directory";
  const content = (
    <>
      {directory ? (
        <FolderSimpleIcon size={15} weight="fill" className={classes.folder} aria-hidden />
      ) : entry.kind === "declaration" ? (
        <PuzzlePieceIcon size={15} className={classes.fileIcon} aria-hidden />
      ) : (
        <FileIcon size={15} className={classes.fileIcon} aria-hidden />
      )}
      <span className={classes.rowName}>{entry.name}</span>
      {directory && <CaretRightIcon size={12} className={classes.caret} aria-hidden />}
    </>
  );
  return directory && onOpen ? (
    <li>
      <button type="button" className={`${classes.row} nodrag`} onClick={() => onOpen(entry.path)}>
        {content}
      </button>
    </li>
  ) : (
    <li className={classes.row} data-static>
      {content}
    </li>
  );
}

function DirectoryNode({ data }: NodeProps<Node<DirectoryNodeData, "directory">>) {
  const { card, actions } = data;
  const more = card.preview ? card.previewTotal - card.preview.length : 0;
  return (
    <div className={classes.card}>
      <Anchors />
      <button type="button" className={`${classes.cardHeader} nodrag`} onClick={() => actions.openPath(card.path)} aria-label={`Open ${card.name}, ${plural(card.files, "file")}`}>
        <FolderSimpleIcon size={20} weight="fill" className={classes.folder} aria-hidden />
        <span className={classes.cardTitle}>{card.name}</span>
        <span className={classes.cardCount}>{formatCount(card.files)}</span>
      </button>
      <ul className={classes.rows}>
        {card.preview === null
          ? [0, 1, 2].map((i) => <li key={i} className={classes.placeholder} aria-hidden />)
          : card.preview.map((entry) => <EntryRow key={entry.id} entry={entry} onOpen={actions.openPath} />)}
        {more > 0 && <li className={classes.more}>{`+${formatCount(more)} more`}</li>}
      </ul>
    </div>
  );
}

function FilesNode({ data }: NodeProps<Node<FilesNodeData, "files">>) {
  const { card } = data;
  const more = card.total - card.entries.length;
  return (
    <div className={classes.card}>
      <Anchors />
      <div className={classes.cardHeader} data-static>
        <FileIcon size={18} className={classes.fileIcon} aria-hidden />
        <span className={classes.cardTitle}>{card.label}</span>
        <span className={classes.cardCount}>{formatCount(card.total)}</span>
      </div>
      <ul className={classes.rows}>
        {card.entries.map((entry) => (
          <EntryRow key={entry.id} entry={entry} />
        ))}
        {more > 0 && <li className={classes.more}>{`+${formatCount(more)} more`}</li>}
      </ul>
    </div>
  );
}

const SHARED_ICONS: Record<string, Icon> = {
  module: CubeIcon,
  package: PackageIcon,
  env_var: CurrencyDollarSimpleIcon,
  command: TerminalWindowIcon,
  config: GearSixIcon,
  http_endpoint: GlobeSimpleIcon,
  http_route: GlobeSimpleIcon,
  http_deployment: GlobeSimpleIcon,
};

function family(entityType: string): string {
  if (entityType === "package") return "package";
  if (entityType.startsWith("http_")) return "endpoint";
  return "external";
}

function SharedNodeView({ data }: NodeProps<Node<SharedNodeData, "shared">>) {
  const { node } = data;
  const Glyph = SHARED_ICONS[node.entityType] ?? CubeIcon;
  return (
    <div className={classes.shared} data-family={family(node.entityType)} title={`${formatCount(node.count)} relationships reach ${node.label.toLowerCase()} shared across the index`}>
      <Anchors />
      <Glyph size={16} className={classes.sharedIcon} aria-hidden />
      <span className={classes.sharedLabel}>{node.label}</span>
      <span className={classes.sharedCount}>{formatCount(node.count)}</span>
    </div>
  );
}

function PeerNodeView({ data }: NodeProps<Node<PeerNodeData, "peer">>) {
  const { node, hue, actions } = data;
  return (
    <div className={classes.peer} style={hue ? hueStyle(hue) : undefined}>
      <Anchors />
      <button type="button" className={classes.cover} onClick={() => actions.openRepository(node.repository)} aria-label={`Open ${node.repository}`} />
      <span className={classes.badge} data-small aria-hidden>
        {node.repository.slice(0, 1).toUpperCase()}
      </span>
      <span className={classes.body}>
        <span className={classes.name}>{node.repository}</span>
        <span className={classes.meta}>{node.path ? node.path : "Another repository"}</span>
      </span>
    </div>
  );
}

/** Elsewhere in this repository: opens the directory that holds it. */
function AwayNodeView({ data }: NodeProps<Node<AwayNodeData, "away">>) {
  const { node, repository, actions } = data;
  const name = node.path.slice(node.path.lastIndexOf("/") + 1);
  return (
    <div className={classes.peer}>
      <Anchors />
      <button
        type="button"
        className={classes.cover}
        onClick={() => actions.openPath(node.opens)}
        aria-label={`Go to ${node.path} in ${repository}`}
      />
      {/* the index says only "a path": a name with an extension reads as a file */}
      {/\.[A-Za-z0-9]+$/.test(name) && !name.startsWith(".") ? (
        <FileIcon size={18} className={classes.fileIcon} aria-hidden />
      ) : (
        <FolderSimpleIcon size={18} weight="fill" className={classes.folder} aria-hidden />
      )}
      <span className={classes.body}>
        <span className={classes.name}>{name}</span>
        <span className={classes.meta}>{node.opens ? `${node.opens}/` : `${repository} root`}</span>
      </span>
    </div>
  );
}

// module scope, so React Flow never sees a new object and remounts every node
export const nodeTypes: NodeTypes = {
  repository: memo(RepositoryNode),
  path: memo(PathNode),
  directory: memo(DirectoryNode),
  files: memo(FilesNode),
  shared: memo(SharedNodeView),
  peer: memo(PeerNodeView),
  away: memo(AwayNodeView),
};
