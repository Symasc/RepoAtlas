"use client";

import { ActionIcon, Tooltip } from "@mantine/core";
import { ArrowRightIcon } from "@phosphor-icons/react";
import { useState } from "react";

import { RepoSwatch } from "@/components/common/RepoSwatch";
import { EntityBadge, LineGlyph, PathBadge } from "@/components/graph/glyphs";
import { compactCount, formatCount } from "@/lib/model/format";
import { FAMILIES, type Connection, type Family, type Partner } from "@/lib/model/graph";

import { Quiet } from "./Inspector";
import classes from "./Inspector.module.css";

const FAMILY_LABEL: Record<Family, string> = {
  code: "Code",
  http: "HTTP",
  dependency: "Package dependencies",
  config: "Config & env",
};

/** Rows listed per family before "show all". */
const SHOWN = 12;

export function PartnerIcon({ partner, hue = null }: { partner: Partner; hue?: number | null }) {
  switch (partner.kind) {
    case "repository":
      return (
        <span className={classes.markCell}>
          <RepoSwatch hue={hue} />
        </span>
      );
    case "shared":
      return <EntityBadge entityType={partner.entityType} />;
    case "directory":
      return <PathBadge path={partner.path ?? partner.label} file={false} />;
    case "file":
      return <PathBadge path={partner.path ?? partner.label} file />;
    case "place":
      return <PathBadge path={partner.path ?? partner.label} />;
    default:
      return <PathBadge path={partner.label} />;
  }
}

function types(connection: Connection): string {
  return connection.link.types
    .slice(0, 3)
    .map((t) => t.type.toLowerCase().replace(/_/g, " "))
    .join(" · ");
}

/**
 * What the inspected thing links to, and what links to it: the list the
 * canvas draws, readable in full. Pressing a row picks that connection, which
 * the canvas draws as the selected path and Evidence explains.
 */
export function Connections({
  connections,
  chosen,
  onChoose,
  onGo,
  hues,
}: {
  connections: readonly Connection[];
  chosen: string | null;
  onChoose: (connection: Connection) => void;
  onGo?: (partner: Partner) => void;
  hues?: ReadonlyMap<string, number>;
}) {
  if (connections.length === 0) {
    return <Quiet>The index records no links here.</Quiet>;
  }
  return (
    <div className={classes.sections}>
      {(["outgoing", "incoming"] as const).map((direction) => {
        const rows = connections.filter((c) => c.direction === direction);
        if (rows.length === 0) return null;
        return (
          <section key={direction} className={classes.section} aria-label={direction === "outgoing" ? "Uses" : "Used by"}>
            <h3 className={classes.sectionTitle}>
              {direction === "outgoing" ? "Uses" : "Used by"}
              <span className={classes.sectionCount}>{formatCount(rows.length)}</span>
            </h3>
            {FAMILIES.map((family) => {
              const list = rows.filter((c) => c.link.family === family);
              return list.length === 0 ? null : (
                <FamilyGroup
                  key={family}
                  family={family}
                  rows={list}
                  chosen={chosen}
                  onChoose={onChoose}
                  onGo={onGo}
                  hues={hues}
                />
              );
            })}
          </section>
        );
      })}
    </div>
  );
}

function FamilyGroup({
  family,
  rows,
  chosen,
  onChoose,
  onGo,
  hues,
}: {
  family: Family;
  rows: Connection[];
  chosen: string | null;
  onChoose: (connection: Connection) => void;
  onGo?: (partner: Partner) => void;
  hues?: ReadonlyMap<string, number>;
}) {
  const [all, setAll] = useState(false);
  const shown = all ? rows : rows.slice(0, SHOWN);
  const total = rows.reduce((sum, c) => sum + c.link.count, 0);
  return (
    <div className={classes.family}>
      <div className={classes.familyTitle}>
        <LineGlyph family={family} width={20} />
        <span>{FAMILY_LABEL[family]}</span>
        <span className={classes.familyCount}>{formatCount(total)}</span>
      </div>
      <ul className={classes.rows}>
        {shown.map((c) => {
          const goable = onGo && c.partner.repository !== undefined && c.partner.path !== undefined;
          return (
            <li key={c.id} className={classes.rowItem}>
              <button
                type="button"
                className={classes.row}
                data-selected={chosen === c.id || undefined}
                aria-pressed={chosen === c.id}
                onClick={() => onChoose(c)}
              >
                <PartnerIcon partner={c.partner} hue={c.partner.repository ? (hues?.get(c.partner.repository) ?? null) : null} />
                <span className={classes.rowText}>
                  <span className={classes.rowLabel}>{c.partner.label}</span>
                  <span className={classes.rowDetail}>
                    {[c.partner.detail, c.via && `via ${c.via}`, types(c)].filter(Boolean).join(" · ")}
                  </span>
                </span>
                <span className={classes.rowCount} title={formatCount(c.link.count)}>
                  {compactCount(c.link.count)}
                </span>
              </button>
              {goable && (
                <Tooltip label={`Go to ${c.partner.label}`}>
                  <ActionIcon
                    size="md"
                    className={classes.go}
                    onClick={() => onGo(c.partner)}
                    aria-label={`Go to ${c.partner.label}`}
                  >
                    <ArrowRightIcon size={14} />
                  </ActionIcon>
                </Tooltip>
              )}
            </li>
          );
        })}
      </ul>
      {rows.length > SHOWN && (
        <button type="button" className={classes.showAll} onClick={() => setAll((v) => !v)}>
          {all ? "Show fewer" : `Show all ${formatCount(rows.length)}`}
        </button>
      )}
    </div>
  );
}
