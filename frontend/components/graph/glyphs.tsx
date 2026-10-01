"use client";

import {
  BracketsCurlyIcon,
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
import type { CSSProperties } from "react";

import type { Family } from "@/lib/model/graph";

import classes from "./glyphs.module.css";

/**
 * The marks that say what a thing is, used alike on the canvas, in the legend
 * and in the inspector: a line's sample stroke, and a shaped badge per family
 * of thing. Each differs by shape and pattern as well as colour.
 */

export function LineGlyph({ family, width = 24 }: { family: Family | "selected"; width?: number }) {
  return (
    <svg width={width} height="10" viewBox={`0 0 ${width} 10`} aria-hidden className={classes.line} data-family={family}>
      {family === "dependency" ? (
        <>
          <line x1="1" y1="5" x2={width - 1} y2="5" className={classes.outer} />
          <line x1="1" y1="5" x2={width - 1} y2="5" className={classes.inner} />
        </>
      ) : (
        <line x1="1" y1="5" x2={width - 1} y2="5" className={classes.stroke} />
      )}
    </svg>
  );
}

/** What a shared thing is drawn as: its icon, and the badge shape of its family. */
const ENTITIES: Record<string, { icon: Icon; shape: Shape }> = {
  module: { icon: CubeIcon, shape: "square" },
  package: { icon: PackageIcon, shape: "hexagon" },
  env_var: { icon: CurrencyDollarSimpleIcon, shape: "diamond" },
  config: { icon: GearSixIcon, shape: "diamond" },
  command: { icon: TerminalWindowIcon, shape: "terminal" },
  http_endpoint: { icon: GlobeSimpleIcon, shape: "circle" },
  http_route: { icon: GlobeSimpleIcon, shape: "circle" },
  http_deployment: { icon: GlobeSimpleIcon, shape: "circle" },
  // what a file declares: the code a trace walks through
  function: { icon: FunctionIcon, shape: "square" },
  method: { icon: FunctionIcon, shape: "square" },
  class: { icon: CubeTransparentIcon, shape: "square" },
  symbol: { icon: BracketsCurlyIcon, shape: "square" },
  test: { icon: FlaskIcon, shape: "square" },
  script: { icon: TerminalWindowIcon, shape: "terminal" },
  file: { icon: FileIcon, shape: "file" },
};

type Shape = "square" | "hexagon" | "diamond" | "circle" | "terminal" | "folder" | "file";

export function entityShape(entityType: string | null | undefined): Shape {
  return ENTITIES[entityType ?? ""]?.shape ?? "square";
}

export function EntityBadge({ entityType, size = 26 }: { entityType: string | null | undefined; size?: number }) {
  const entry = ENTITIES[entityType ?? ""] ?? ENTITIES.module!;
  const Glyph = entry.icon;
  return (
    <span className={classes.badge} data-shape={entry.shape} style={{ width: size, height: size }} aria-hidden>
      <Glyph size={Math.round(size * 0.55)} weight="bold" className={classes.badgeIcon} />
    </span>
  );
}

/** A name with an extension reads as a file; the index says only "a path". */
export function looksLikeFile(path: string): boolean {
  const name = path.slice(path.lastIndexOf("/") + 1);
  return /\.[A-Za-z0-9]+$/.test(name) && !name.startsWith(".");
}

/** A directory or a file; `file` says which when it is known, else the name decides. */
export function PathBadge({ path, size = 26, file: known }: { path: string; size?: number; file?: boolean }) {
  const file = known ?? looksLikeFile(path);
  const Glyph = file ? FileIcon : FolderSimpleIcon;
  return (
    <span className={classes.badge} data-shape={file ? "file" : "folder"} style={{ width: size, height: size }} aria-hidden>
      <Glyph size={Math.round(size * 0.58)} weight={file ? "regular" : "fill"} className={classes.badgeIcon} />
    </span>
  );
}

/** A repository's monogram, in its identity hue. */
export function Monogram({ name, hue, size = 34 }: { name: string; hue: number | null; size?: number }) {
  return (
    <span
      className={classes.monogram}
      style={{ width: size, height: size, fontSize: Math.round(size * 0.44), ...(hue ? { "--hue": `var(--ra-repo-${hue})` } : {}) } as CSSProperties}
      aria-hidden
    >
      {name.slice(0, 1).toUpperCase()}
    </span>
  );
}
