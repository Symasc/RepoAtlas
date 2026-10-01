"use client";

import {
  BracketsCurlyIcon,
  CloudIcon,
  CubeIcon,
  CubeTransparentIcon,
  CurrencyDollarSimpleIcon,
  FileIcon,
  FlaskIcon,
  FolderSimpleIcon,
  FunctionIcon,
  GearSixIcon,
  GlobeSimpleIcon,
  PackageIcon,
  TerminalWindowIcon,
  type Icon,
} from "@phosphor-icons/react";

import type { Family, Relation } from "@/lib/model/graph";

import classes from "./glyphs.module.css";

/**
 * The marks that say what a thing is, used alike on the canvas, in the legend
 * and in the inspector: a line's sample stroke, and an icon per kind of thing,
 * drawn bare in its family's ink. Each kind differs by its icon's outline as
 * well as its colour, so colour is never the only difference.
 */

/** The code relationships a bare code mark stands for, in the order they are drawn. */
const CODE_SAMPLE: readonly Relation[] = ["imports", "calls", "uses"];

/**
 * A line's sample: its family's pattern, in its relationship's colour. Code
 * with no one relationship named (a card's tally, a family's heading) is drawn
 * in the colours of the relationships it gathers, end to end.
 */
export function LineGlyph({
  family,
  relation,
  width = 24,
}: {
  family: Family | "selected";
  relation?: Relation;
  width?: number;
}) {
  // a bare code mark's three segments, with a hairline gap so each colour reads
  const gap = 1.5;
  const segment = (width - 2 - gap * (CODE_SAMPLE.length - 1)) / CODE_SAMPLE.length;
  return (
    <svg
      width={width}
      height="10"
      viewBox={`0 0 ${width} 10`}
      aria-hidden
      className={classes.line}
      data-family={family}
      data-relation={relation ?? (family === "code" ? undefined : family)}
    >
      {family === "dependency" ? (
        <>
          <line x1="1" y1="5" x2={width - 1} y2="5" className={classes.outer} />
          <line x1="1" y1="5" x2={width - 1} y2="5" className={classes.inner} />
        </>
      ) : family === "code" && !relation ? (
        CODE_SAMPLE.map((r, i) => (
          <line
            key={r}
            x1={1 + (segment + gap) * i}
            y1="5"
            x2={1 + (segment + gap) * i + segment}
            y2="5"
            className={classes.segment}
            data-relation={r}
          />
        ))
      ) : (
        <line x1="1" y1="5" x2={width - 1} y2="5" className={classes.stroke} />
      )}
    </svg>
  );
}

type Tone = "dependency" | "http" | "config" | "code";

/** What a shared thing is drawn as: its icon, in the ink of the family of link that reaches it. */
const ENTITIES: Record<string, { icon: Icon; tone: Tone }> = {
  module: { icon: CubeIcon, tone: "code" },
  package: { icon: PackageIcon, tone: "dependency" },
  env_var: { icon: CurrencyDollarSimpleIcon, tone: "config" },
  config: { icon: GearSixIcon, tone: "config" },
  command: { icon: TerminalWindowIcon, tone: "code" },
  http_endpoint: { icon: GlobeSimpleIcon, tone: "http" },
  http_route: { icon: GlobeSimpleIcon, tone: "http" },
  // where an API is published: HTTP's ink, with a cloud of its own
  http_deployment: { icon: CloudIcon, tone: "http" },
  // what a file declares: the code a trace walks through
  function: { icon: FunctionIcon, tone: "code" },
  method: { icon: FunctionIcon, tone: "code" },
  class: { icon: CubeTransparentIcon, tone: "code" },
  symbol: { icon: BracketsCurlyIcon, tone: "code" },
  test: { icon: FlaskIcon, tone: "code" },
  script: { icon: TerminalWindowIcon, tone: "code" },
  file: { icon: FileIcon, tone: "code" },
};

export function EntityBadge({ entityType, size = 16 }: { entityType: string | null | undefined; size?: number }) {
  const entry = ENTITIES[entityType ?? ""] ?? ENTITIES.module!;
  const Glyph = entry.icon;
  return <Glyph size={size} className={classes.mark} data-tone={entry.tone} aria-hidden />;
}

/** A name with an extension reads as a file; the index says only "a path". */
export function looksLikeFile(path: string): boolean {
  const name = path.slice(path.lastIndexOf("/") + 1);
  return /\.[A-Za-z0-9]+$/.test(name) && !name.startsWith(".");
}

/** A directory or a file; `file` says which when it is known, else the name decides. */
export function PathBadge({ path, size = 16, file: known }: { path: string; size?: number; file?: boolean }) {
  const file = known ?? looksLikeFile(path);
  const Glyph = file ? FileIcon : FolderSimpleIcon;
  return <Glyph size={size} className={classes.mark} data-tone={file ? "file" : "folder"} aria-hidden />;
}
