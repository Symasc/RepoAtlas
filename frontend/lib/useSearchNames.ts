"use client";

import { useSyncExternalStore } from "react";

/**
 * Whether search also finds variables and the other names code declares, not only
 * repositories and files. The reader's choice, remembered in this browser; off
 * until it is made.
 */
const NAMES_KEY = "ra-search-names";

const CHANGED = "ra-search-names-change";
/** The choice made on this page, for a browser that refuses to store it. */
let chosen: boolean | null = null;

function read(): boolean {
  try {
    const stored = window.localStorage.getItem(NAMES_KEY);
    if (stored !== null) return stored === "on";
  } catch {
    // unreadable storage: what was chosen on this page, if anything
  }
  return chosen ?? false;
}

function subscribe(onChange: () => void): () => void {
  // a choice made in another tab reaches this one too
  window.addEventListener("storage", onChange);
  window.addEventListener(CHANGED, onChange);
  return () => {
    window.removeEventListener("storage", onChange);
    window.removeEventListener(CHANGED, onChange);
  };
}

/** True when search includes names. The server renders as if it does not. */
export function useSearchNames(): boolean {
  return useSyncExternalStore(subscribe, read, () => false);
}

/** Remembers the reader's choice in this browser and applies it at once. */
export function chooseSearchNames(on: boolean): void {
  chosen = on;
  try {
    window.localStorage.setItem(NAMES_KEY, on ? "on" : "off");
  } catch {
    // not remembered past this page, but applied
  }
  window.dispatchEvent(new Event(CHANGED));
}
