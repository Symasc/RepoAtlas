"use client";

import { ArrowRightIcon, CaretRightIcon, FileIcon, FolderSimpleIcon, PuzzlePieceIcon } from "@phosphor-icons/react";
import { Handle, Position, type Node, type NodeProps, type NodeTypes } from "@xyflow/react";
import { Fragment, memo, type MouseEvent, type ReactNode } from "react";

import { RepoSwatch } from "@/components/common/RepoSwatch";
import type { ChildEntry } from "@/lib/api/generated";
import { compactCount, formatCount, plural, shortSha } from "@/lib/model/format";
import type { FreshnessModel } from "@/lib/model/freshness";
import {
  FAMILIES,
  FILE_ROWS,
  type DirectoryCard,
  type FamilyCounts,
  type FilesCard,
  type FrameNode as FrameModel,
  type MoreNode as MoreModel,
  type Neighbour,
  type PeerNode as PeerModel,
  type PlaceNode as PlaceModel,
  type SharedNode as SharedModel,
} from "@/lib/model/graph";
import type { RepositoryModel } from "@/lib/model/repositories";

import { FAMILY_NOUN } from "./edges";
import { EntityBadge, LineGlyph, PathBadge } from "./glyphs";
import classes from "./Graph.module.css";
import type { TraceNode } from "./traceNodes";

/** What a node can ask of the view; the view owns selection and navigation. */
export interface NodeActions {
  /** A first press selects; a press on what is selected goes into it. */
  activate: (id: string) => void;
  /** Straight to a place: a row inside a card names exactly where it goes. */
  go: (to: { repository: string; path: string }) => void;
}

type Common = { actions: NodeActions; selected?: boolean };

export type RepositoryNodeData = Common & { repository: RepositoryModel; partners: number };
export type HubNodeData = { repositories: number; files: number; note: string | null };
export type GroupNodeData = { count: number };
/** Out to a directory on the way here, `""` for the repository's root (see useClimb). */
export type Climb = (at: string) => void;

export type FrameNodeData = { frame: FrameModel; repository: RepositoryModel; climb: Climb };
export type DirectoryNodeData = Common & { card: DirectoryCard; repository: string };
export type FilesNodeData = Common & { card: FilesCard; repository: string };
export type SharedNodeData = Common & { node: SharedModel };
export type PeerNodeData = Common & { node: PeerModel; hue: number | null };
export type PlaceNodeData = Common & { node: PlaceModel; repository: string };
export type MoreNodeData = Common & { node: MoreModel; noun: string };
export type AnchorNodeData = Common & {
  name: string;
  path: string;
  repository: RepositoryModel;
  language: string | null;
  declarations: number;
  lines: number | null;
  climb: Climb;
};
export type NeighbourNodeData = Common & { node: Neighbour };

export type GraphNode =
  | Node<RepositoryNodeData, "repository">
  | Node<HubNodeData, "hub">
  | Node<GroupNodeData, "group">
  | Node<FrameNodeData, "frame">
  | Node<DirectoryNodeData, "directory">
  | Node<FilesNodeData, "files">
  | Node<SharedNodeData, "shared">
  | Node<PeerNodeData, "peer">
  | Node<PlaceNodeData, "place">
  | Node<MoreNodeData, "more">
  | Node<AnchorNodeData, "anchor">
  | Node<NeighbourNodeData, "neighbour">
  | TraceNode;

/** Nodes that are backdrop, not things: never faded, never selected. */
export const BACKDROP: ReadonlySet<string> = new Set(["group", "frame", "hub", "traceFrame", "traceLabel"]);

/** React Flow wants handles to draw edges from; ours compute their own ends, so one hidden pair will do. */
export function Anchors() {
  return (
    <>
      <Handle type="source" position={Position.Right} className={classes.anchor} isConnectable={false} />
      <Handle type="target" position={Position.Left} className={classes.anchor} isConnectable={false} />
    </>
  );
}

export function stop(run: () => void) {
  return (event: MouseEvent) => {
    event.stopPropagation();
    run();
  };
}

/**
 * The whole node as one real button. Its name says what pressing it does:
 * select it, or, once selected, open it.
 */
function Cover({
  id,
  name,
  selected,
  openable,
  actions,
}: {
  id: string;
  name: string;
  selected: boolean;
  openable: boolean;
  actions: NodeActions;
}) {
  const label = selected ? (openable ? `Open ${name}` : `${name}, selected`) : `Select ${name}`;
  return (
    <button
      type="button"
      className={`${classes.cover} nodrag`}
      onClick={stop(() => actions.activate(id))}
      aria-label={label}
    />
  );
}

/** Shown on a selected node that can be opened: says the next press goes in. */
function OpenHint() {
  return (
    <span className={classes.openHint} aria-hidden>
      Open <ArrowRightIcon size={12} weight="bold" />
    </span>
  );
}

/** A repository's freshness, said in words in its own tone: never a bare dot. */
function FreshnessWord({ freshness }: { freshness: FreshnessModel }) {
  return (
    <span className={classes.freshness} data-tone={freshness.tone} title={freshness.explanation.replace(/`/g, "")}>
      {freshness.label}
    </span>
  );
}

/**
 * Formats, configuration and shell glue say little about what a repository is
 * written in; they are listed only when nothing else is.
 */
const NOT_LANGUAGES = new Set([
  "JSON", "YAML", "TOML", "Markdown", "XML", "INI", "Dockerfile", "Jinja", "Salt", "Bash", "Shell", "Makefile",
]);

export function languagesOf(repository: RepositoryModel, max = 3): string {
  const code = repository.languages.filter((l) => !NOT_LANGUAGES.has(l));
  const list = code.length > 0 ? code : repository.languages;
  const shown = list.slice(0, max).join(", ");
  return list.length > max ? `${shown} +${list.length - max}` : shown;
}

// -- the overview ---------------------------------------------------------------------

function RepositoryNode({ id, data }: NodeProps<Node<RepositoryNodeData, "repository">>) {
  const { repository: r, selected = false, actions, partners } = data;
  return (
    <div className={classes.repository} data-selected={selected || undefined}>
      <Anchors />
      <Cover id={id} name={r.name} selected={selected} openable actions={actions} />
      <span className={classes.body}>
        <span className={classes.titleLine}>
          <RepoSwatch hue={r.hue} size={12} />
          <span className={classes.name}>{r.name}</span>
          {selected ? <OpenHint /> : <FreshnessWord freshness={r.freshness} />}
        </span>
        <span className={classes.meta}>{languagesOf(r) || "No languages recorded"}</span>
        <span className={classes.stats}>
          <span>{plural(r.files, "file")}</span>
          {partners > 0 ? <span>{plural(partners, "partner")}</span> : <span>{plural(r.symbols, "symbol")}</span>}
        </span>
      </span>
    </div>
  );
}

/** The workspace itself, at the centre when no repository is: a label on the map, not a badge. */
function HubNode({ data }: NodeProps<Node<HubNodeData, "hub">>) {
  return (
    <div className={classes.hub}>
      <Anchors />
      <span className={classes.hubTitle}>Workspace</span>
      <span className={classes.hubMeta}>
        {plural(data.repositories, "repository", "repositories")} · {plural(data.files, "file")}
      </span>
      {data.note && <span className={classes.hubNote}>{data.note}</span>}
    </div>
  );
}

function GroupNode({ data }: NodeProps<Node<GroupNodeData, "group">>) {
  return (
    <div className={classes.group}>
      <span className={classes.groupLabel}>
        No links recorded with the others · {formatCount(data.count)}
      </span>
    </div>
  );
}

// -- an opened repository ---------------------------------------------------------------

/**
 * `repository / a/b/c` where a node says where it is, each step a way straight
 * there: no trip to the trail in the corner. `here` is the last step when the
 * reader is in it, so it is not a link.
 */
function PathCrumbs({ repository, path, here, climb }: { repository: string; path: string; here: boolean; climb: Climb }) {
  const segments = path ? path.split("/") : [];
  const step = (label: string, at: string) => (
    <button type="button" className={`${classes.crumb} nodrag nopan`} onClick={stop(() => climb(at))} title={`Go to ${at || repository}`}>
      {label}
    </button>
  );
  return (
    <span className={classes.crumbs}>
      {step(repository, "")}
      {segments.length > 0 && <span aria-hidden> / </span>}
      {segments.map((segment, i) => {
        const at = segments.slice(0, i + 1).join("/");
        return (
          <Fragment key={at}>
            {i > 0 && <span aria-hidden>/</span>}
            {here && i === segments.length - 1 ? (
              <span className={classes.crumbHere} aria-current="location">
                {segment}
              </span>
            ) : (
              step(segment, at)
            )}
          </Fragment>
        );
      })}
    </span>
  );
}

/** The level itself: what it encloses is what is in it. */
function FrameNode({ data }: NodeProps<Node<FrameNodeData, "frame">>) {
  const { frame, repository, climb } = data;
  const parts: string[] = [];
  if (frame.directories) parts.push(plural(frame.directories, "directory", "directories"));
  if (frame.leaves) parts.push(plural(frame.leaves, "file"));
  return (
    <div className={classes.frame}>
      <Anchors />
      <div className={classes.frameHeader}>
        {frame.path ? (
          <FolderSimpleIcon size={18} className={classes.folder} aria-hidden />
        ) : (
          <RepoSwatch hue={repository.hue} size={12} />
        )}
        <span className={classes.frameTitle}>
          <span className={classes.frameName}>{frame.name}</span>
          <span className={classes.frameMeta}>
            {frame.path ? (
              <PathCrumbs repository={repository.name} path={frame.path} here climb={climb} />
            ) : (
              `${repository.branch ?? "detached"} ${shortSha(repository.headSha) ?? ""}`
            )}
          </span>
        </span>
        <span className={classes.frameCount}>
          {plural(frame.files, "file")}
          {parts.length > 0 && <span className={classes.frameParts}>{parts.join(" · ")}</span>}
        </span>
      </div>
    </div>
  );
}

/** Per family, how many links touch a card: the card says so without drawing them. */
function Tally({ summary }: { summary: FamilyCounts }) {
  const present = FAMILIES.filter((f) => (summary[f] ?? 0) > 0);
  if (present.length === 0) return null;
  return (
    <span className={classes.tally}>
      {present.map((family) => (
        <span key={family} className={classes.tallyItem} title={`${formatCount(summary[family]!)} ${FAMILY_NOUN[family]} links`}>
          <LineGlyph family={family} width={14} />
          {compactCount(summary[family]!)}
          <span className={classes.srOnly}> {FAMILY_NOUN[family]} links</span>
        </span>
      ))}
    </span>
  );
}

function EntryRow({ entry, repository, actions }: { entry: ChildEntry; repository: string; actions: NodeActions }) {
  const directory = entry.kind === "directory";
  const icon = directory ? (
    <FolderSimpleIcon size={14} className={classes.folder} aria-hidden />
  ) : entry.kind === "declaration" ? (
    <PuzzlePieceIcon size={14} className={classes.fileIcon} aria-hidden />
  ) : (
    <FileIcon size={14} className={classes.fileIcon} aria-hidden />
  );
  if (entry.kind === "declaration") {
    return (
      <li className={classes.row} data-static>
        {icon}
        <span className={classes.rowName}>{entry.name}</span>
      </li>
    );
  }
  return (
    <li>
      <button
        type="button"
        className={`${classes.row} nodrag`}
        onClick={stop(() => actions.go({ repository, path: entry.path }))}
      >
        {icon}
        <span className={classes.rowName}>{entry.name}</span>
        <CaretRightIcon size={12} className={classes.caret} aria-hidden />
      </button>
    </li>
  );
}

function CardShell({
  id,
  selected,
  header,
  children,
  summary,
}: {
  id: string;
  selected: boolean;
  header: ReactNode;
  children: ReactNode;
  summary: FamilyCounts;
}) {
  return (
    <div className={classes.card} data-selected={selected || undefined} data-id={id}>
      <Anchors />
      {header}
      <ul className={classes.rows}>{children}</ul>
      <Tally summary={summary} />
    </div>
  );
}

function DirectoryNode({ id, data }: NodeProps<Node<DirectoryNodeData, "directory">>) {
  const { card, actions, repository, selected = false } = data;
  const more = card.preview ? card.previewTotal - card.preview.length : 0;
  return (
    <CardShell
      id={id}
      selected={selected}
      summary={card.summary}
      header={
        <button
          type="button"
          className={`${classes.cardHeader} nodrag`}
          onClick={stop(() => actions.activate(id))}
          aria-label={selected ? `Open ${card.name}` : `Select ${card.name}, ${plural(card.files, "file")}`}
        >
          <FolderSimpleIcon size={16} className={classes.folder} aria-hidden />
          <span className={classes.cardTitle}>{card.name}</span>
          {selected ? <OpenHint /> : <span className={classes.cardCount}>{formatCount(card.files)}</span>}
        </button>
      }
    >
      {card.preview === null
        ? [0, 1, 2].map((i) => <li key={i} className={classes.placeholder} aria-hidden />)
        : card.preview.map((entry) => <EntryRow key={entry.id} entry={entry} repository={repository} actions={actions} />)}
      {more > 0 && (
        <li>
          <button type="button" className={`${classes.more} nodrag`} onClick={stop(() => actions.go({ repository, path: card.path }))}>
            {`+${formatCount(more)} more`}
          </button>
        </li>
      )}
    </CardShell>
  );
}

function FilesNode({ id, data }: NodeProps<Node<FilesNodeData, "files">>) {
  const { card, actions, repository, selected = false } = data;
  const shown = card.entries.slice(0, FILE_ROWS);
  const more = card.total - shown.length;
  return (
    <CardShell
      id={id}
      selected={selected}
      summary={card.summary}
      header={
        <button
          type="button"
          className={`${classes.cardHeader} nodrag`}
          onClick={stop(() => actions.activate(id))}
          aria-label={selected ? "Files here, selected" : `Select the files here, ${plural(card.total, "file")}`}
        >
          <FileIcon size={16} className={classes.fileIcon} aria-hidden />
          <span className={classes.cardTitle}>Files</span>
          <span className={classes.cardCount}>{formatCount(card.total)}</span>
        </button>
      }
    >
      {shown.map((entry) => (
        <EntryRow key={entry.id} entry={entry} repository={repository} actions={actions} />
      ))}
      {more > 0 && (
        <li>
          <button type="button" className={`${classes.more} nodrag`} onClick={stop(() => actions.activate(id))}>
            {`+${formatCount(more)} more`}
          </button>
        </li>
      )}
    </CardShell>
  );
}

/** Things many files share: modules, packages, variables, routes. Never opened, only inspected. */
function SharedNodeView({ id, data }: NodeProps<Node<SharedNodeData, "shared">>) {
  const { node, actions, selected = false } = data;
  return (
    <div className={classes.shared} data-entity={node.entityType} data-selected={selected || undefined}>
      <Anchors />
      <Cover id={id} name={node.label} selected={selected} openable={false} actions={actions} />
      <EntityBadge entityType={node.entityType} />
      <span className={classes.sharedLabel}>{node.label}</span>
      <span className={classes.sharedCount}>{compactCount(node.count)}</span>
    </div>
  );
}

function PeerNodeView({ id, data }: NodeProps<Node<PeerNodeData, "peer">>) {
  const { node, hue, actions, selected = false } = data;
  return (
    <div className={classes.peer} data-selected={selected || undefined}>
      <Anchors />
      <Cover id={id} name={node.repository} selected={selected} openable actions={actions} />
      <RepoSwatch hue={hue} size={12} />
      <span className={classes.body}>
        <span className={classes.name}>{node.repository}</span>
        <span className={classes.meta}>Another repository · {compactCount(node.count)}</span>
      </span>
      {selected && <OpenHint />}
    </div>
  );
}

/** Elsewhere in this repository. */
function PlaceNodeView({ id, data }: NodeProps<Node<PlaceNodeData, "place">>) {
  const { node, actions, selected = false } = data;
  const name = node.path.slice(node.path.lastIndexOf("/") + 1);
  const cut = node.path.lastIndexOf("/");
  return (
    <div className={classes.place} data-selected={selected || undefined}>
      <Anchors />
      <Cover id={id} name={node.path} selected={selected} openable actions={actions} />
      <PathBadge path={node.path} />
      <span className={classes.body}>
        <span className={classes.name}>{name}</span>
        <span className={classes.meta}>{cut > 0 ? `${node.path.slice(0, cut)}/` : `${data.repository} root`}</span>
      </span>
      {selected && <OpenHint />}
    </div>
  );
}

function MoreNodeView({ id, data }: NodeProps<Node<MoreNodeData, "more">>) {
  const { node, actions, selected = false, noun } = data;
  const label = `${formatCount(node.nodes)} more ${noun}`;
  return (
    <div className={classes.moreNode} data-selected={selected || undefined}>
      <Anchors />
      <Cover id={id} name={label} selected={selected} openable={false} actions={actions} />
      <span className={classes.moreLabel}>{label}</span>
      <span className={classes.sharedCount}>{compactCount(node.count)}</span>
    </div>
  );
}

// -- a file and its neighbourhood -----------------------------------------------------

function AnchorNode({ id, data }: NodeProps<Node<AnchorNodeData, "anchor">>) {
  const { name, path, repository, language, declarations, lines, actions, climb } = data;
  const cut = path.lastIndexOf("/");
  return (
    <div className={classes.fileAnchor}>
      <Anchors />
      <button
        type="button"
        className={`${classes.cover} nodrag`}
        onClick={stop(() => actions.activate(id))}
        aria-label={`${name}, this file`}
      />
      <FileIcon size={22} className={classes.anchorIcon} aria-hidden />
      <span className={classes.body}>
        <span className={classes.anchorName}>{name}</span>
        <span className={classes.meta}>
          <PathCrumbs repository={repository.name} path={cut > 0 ? path.slice(0, cut) : ""} here={false} climb={climb} />
        </span>
        <span className={classes.stats}>
          {language && <span>{language}</span>}
          <span>{plural(declarations, "declaration")}</span>
          {lines !== null && <span>{plural(lines, "line")}</span>}
        </span>
      </span>
    </div>
  );
}

function NeighbourNode({ id, data }: NodeProps<Node<NeighbourNodeData, "neighbour">>) {
  const { node, actions, selected = false } = data;
  return (
    <div className={classes.place} data-neighbour data-selected={selected || undefined}>
      <Anchors />
      <Cover id={id} name={node.detail ? `${node.detail}/${node.label}` : node.label} selected={selected} openable actions={actions} />
      <PathBadge path={node.target} />
      <span className={classes.body}>
        <span className={classes.name}>{node.label}</span>
        <span className={classes.meta}>{node.detail ? `${node.detail}/` : "repository root"}</span>
      </span>
      {selected ? <OpenHint /> : <span className={classes.sharedCount}>{compactCount(node.count)}</span>}
    </div>
  );
}

// module scope, so React Flow never sees a new object and remounts every node
export const nodeTypes: NodeTypes = {
  repository: memo(RepositoryNode),
  hub: memo(HubNode),
  group: memo(GroupNode),
  frame: memo(FrameNode),
  directory: memo(DirectoryNode),
  files: memo(FilesNode),
  shared: memo(SharedNodeView),
  peer: memo(PeerNodeView),
  place: memo(PlaceNodeView),
  more: memo(MoreNodeView),
  anchor: memo(AnchorNode),
  neighbour: memo(NeighbourNode),
};
