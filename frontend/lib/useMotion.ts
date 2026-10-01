"use client";

import { useSyncExternalStore } from "react";

import { animationsOff, MOTION_KEY, REDUCE_QUERY, type MotionChoice } from "./motion";

const CHANGED = "ra-motion-change";
/** The choice made on this page, for a browser that refuses to store it. */
let chosen: MotionChoice | null = null;

function stored(): string | null {
  try {
    return window.localStorage.getItem(MOTION_KEY) ?? chosen;
  } catch {
    return chosen;
  }
}

function read(): boolean {
  return animationsOff(stored(), window.matchMedia(REDUCE_QUERY).matches);
}

function apply(): void {
  document.documentElement.setAttribute("data-motion", read() ? "reduced" : "full");
}

function subscribe(onChange: () => void): () => void {
  // another tab's choice, or the system's, reaches this page's CSS too
  const changed = () => {
    apply();
    onChange();
  };
  const media = window.matchMedia(REDUCE_QUERY);
  media.addEventListener("change", changed);
  window.addEventListener("storage", changed);
  window.addEventListener(CHANGED, changed);
  return () => {
    media.removeEventListener("change", changed);
    window.removeEventListener("storage", changed);
    window.removeEventListener(CHANGED, changed);
  };
}

/** True when the explorer should not animate. The server renders as if it may. */
export function useAnimationsOff(): boolean {
  return useSyncExternalStore(subscribe, read, () => false);
}

/** Remembers the reader's choice in this browser and applies it at once. */
export function chooseAnimations(choice: MotionChoice): void {
  chosen = choice;
  try {
    window.localStorage.setItem(MOTION_KEY, choice);
  } catch {
    // not remembered past this page, but applied
  }
  apply();
  window.dispatchEvent(new Event(CHANGED));
}
