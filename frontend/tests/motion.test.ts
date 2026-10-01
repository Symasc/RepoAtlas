import { describe, expect, it } from "vitest";

import { animationsOff, MOTION_KEY, MOTION_SCRIPT, REDUCE_QUERY } from "@/lib/motion";

/** Runs the head script against a stored choice and a system preference; returns `data-motion`. */
function boot(choice: string | null, systemReduces: boolean, storage = true): string | undefined {
  const attributes: Record<string, string> = {};
  const localStorage = {
    getItem: (key: string) => {
      if (!storage) throw new Error("storage refused");
      return key === MOTION_KEY ? choice : null;
    },
  };
  const matchMedia = (query: string) => ({ matches: query === REDUCE_QUERY && systemReduces });
  const document = { documentElement: { setAttribute: (name: string, value: string) => (attributes[name] = value) } };
  new Function("localStorage", "matchMedia", "document", MOTION_SCRIPT)(localStorage, matchMedia, document);
  return attributes["data-motion"];
}

describe("animations", () => {
  it("follow the reader's choice, else the system's", () => {
    expect(animationsOff("off", false)).toBe(true);
    expect(animationsOff("on", true)).toBe(false);
    expect(animationsOff(null, true)).toBe(true);
    expect(animationsOff(null, false)).toBe(false);
    expect(animationsOff("something else", true)).toBe(true);
  });

  it("are decided before first paint exactly as the app decides them", () => {
    for (const choice of ["on", "off", null, "junk"]) {
      for (const system of [true, false]) {
        expect(boot(choice, system)).toBe(animationsOff(choice, system) ? "reduced" : "full");
      }
    }
  });

  it("leave the page as it was when storage is refused", () => {
    expect(boot("off", false, false)).toBeUndefined();
  });
});
