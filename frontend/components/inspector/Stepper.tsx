"use client";

import { CaretDownIcon, CaretRightIcon } from "@phosphor-icons/react";
import { useState } from "react";

import type { EvidenceItem } from "@/lib/api/generated";
import { placeLabel, stationsOf, type Crossing, type HopPlace, type Station } from "@/lib/model/decisions";

import classes from "./Requests.module.css";

/**
 * A crossing's chain as a plain evidence stepper: the join's seven hops in order,
 * each with its exact state. Emphasis stops at the first hop that is not proven;
 * the hops the join never reached are subdued and cite nothing. A proven or
 * unresolved hop opens out to its places, and, when the decision was read with
 * its evidence, to the lines of each.
 */
export function Stepper({
  crossing,
  evidence,
  onOpenPlace,
}: {
  crossing: Crossing;
  /** The lines supplied for this decision, by evidence id: only a detailed read has them. */
  evidence?: ReadonlyMap<string, EvidenceItem>;
  onOpenPlace?: (place: HopPlace) => void;
}) {
  return (
    <ol className={classes.stepper} aria-label={`The chain of ${crossing.decision_ref}`}>
      {stationsOf(crossing).map((station) => (
        <Step key={station.kind} station={station} evidence={evidence} onOpenPlace={onOpenPlace} />
      ))}
    </ol>
  );
}

function Step({
  station,
  evidence,
  onOpenPlace,
}: {
  station: Station;
  evidence?: ReadonlyMap<string, EvidenceItem>;
  onOpenPlace?: (place: HopPlace) => void;
}) {
  const [open, setOpen] = useState(false);
  const why = [station.reason, station.note].filter(Boolean).join(": ");
  return (
    <li className={classes.step} data-state={station.state} data-tone={station.tone} data-hop={station.kind}>
      <span className={classes.mark} aria-hidden />
      <div className={classes.stepBody}>
        <p className={classes.stepHead}>
          <span className={classes.stepLabel}>{station.label}</span>
          <span className={classes.stepState}>{station.state}</span>
          {station.expandable && (
            <button
              type="button"
              className={classes.stepToggle}
              aria-expanded={open}
              onClick={() => setOpen((o) => !o)}
            >
              {open ? <CaretDownIcon size={11} aria-hidden /> : <CaretRightIcon size={11} aria-hidden />}
              {station.places.length === 1 ? "1 place" : `${station.places.length} places`}
            </button>
          )}
        </p>
        {why && <p className={classes.stepWhy}>{why}</p>}
        {station.detail && <p className={classes.stepDetail}>{station.detail}</p>}
        {station.withheld && <p className={classes.stepWhy}>Another repository&apos;s part of this hop is not shown in this scope.</p>}
        {station.state === "ambiguous" && station.places.length > 0 && <Places places={station.places} onOpenPlace={onOpenPlace} />}
        {open && <Places places={station.places} evidence={evidence} onOpenPlace={onOpenPlace} />}
      </div>
    </li>
  );
}

function Places({
  places,
  evidence,
  onOpenPlace,
}: {
  places: HopPlace[];
  evidence?: ReadonlyMap<string, EvidenceItem>;
  onOpenPlace?: (place: HopPlace) => void;
}) {
  return (
    <ul className={classes.places}>
      {places.map((place, i) => {
        const item = place.evidence_id ? evidence?.get(place.evidence_id) : undefined;
        const label = placeLabel(place);
        return (
          <li key={`${label}#${i}`} className={classes.place}>
            {onOpenPlace ? (
              <button type="button" className={classes.placeLink} onClick={() => onOpenPlace(place)}>
                {label}
              </button>
            ) : (
              <span className={classes.placeText}>{label}</span>
            )}
            <span className={classes.placeRole}>{place.role}</span>
            {item && <Lines item={item} />}
          </li>
        );
      })}
    </ul>
  );
}

/** The supplied lines, numbered as they are in the file. */
function Lines({ item }: { item: EvidenceItem }) {
  const start = item.ref.line_start ?? 1;
  return (
    <pre className={classes.lines}>
      {(item.text ?? "")
        .replace(/\n$/, "")
        .split("\n")
        .map((line, i) => (
          <span key={i} className={classes.line}>
            <span className={classes.lineNumber}>{start + i}</span>
            <span>{line || " "}</span>
          </span>
        ))}
    </pre>
  );
}
