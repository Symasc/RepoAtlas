"use client";

import { createContext, useContext } from "react";

/**
 * Moving between levels. Going deeper zooms into what was opened; climbing out
 * zooms back out onto where the reader came from. The canvas that is showing
 * registers how it leaves; the explorer awaits that before it changes the URL.
 */

export type Direction = "in" | "out" | "across" | "none";

export interface Arrival {
  direction: Direction;
  /** Node, in the level arrived at, that the reader came out of. */
  from: string | null;
}

export type Exit = (direction: Direction, via: string | null) => Promise<void>;

/** Registers how the showing canvas leaves; returns how to take that back. */
export type RegisterExit = (exit: Exit) => () => void;

export const TransitionContext = createContext<RegisterExit | null>(null);

export function useRegisterExit(): RegisterExit | null {
  return useContext(TransitionContext);
}

export const easeOut = (t: number) => 1 - Math.pow(1 - t, 3);
export const easeIn = (t: number) => t * t * t;
export const easeInOut = (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
