import { describe, expect, it } from "vitest";

import { refitAction, type RefitCause } from "@/lib/graph/refit";

/**
 * When the canvas glides to a new fit. With nothing held the rule is the one the
 * canvas always had; a hold only ever removes a cause.
 */

const bools = [false, true] as const;
const causes: RefitCause[] = bools.flatMap((recentred) =>
  bools.flatMap((resized) => bools.flatMap((grown) => bools.map((moved) => ({ recentred, resized, grown, moved })))),
);

/** The rule as it stood before holds existed: glide unless nothing asked, or only growth asked of a view the reader moved. */
function before({ recentred, resized, grown, moved }: RefitCause): "none" | "glide" {
  if (!recentred && !resized && !grown) return "none";
  if (!recentred && !resized && moved) return "none";
  return "glide";
}

const name = (c: object) => JSON.stringify(c);

describe("refitAction", () => {
  it("is the old rule on all 16 combinations when nothing is held", () => {
    expect(causes).toHaveLength(16);
    for (const cause of causes) {
      expect(refitAction(cause, {}), name(cause)).toBe(before(cause));
      expect(refitAction(cause), name(cause)).toBe(before(cause));
    }
  });

  it("does not glide for a resize alone when resizes are held", () => {
    for (const moved of bools) {
      expect(refitAction({ recentred: false, resized: true, grown: false, moved }, { resize: true })).toBe("none");
    }
  });

  it("does not glide for growth alone when growth is held", () => {
    for (const moved of bools) {
      expect(refitAction({ recentred: false, resized: false, grown: true, moved }, { growth: true })).toBe("none");
    }
  });

  it("still glides for growth when only resizes are held, and for a resize when only growth is held", () => {
    expect(refitAction({ recentred: false, resized: false, grown: true, moved: false }, { resize: true })).toBe("glide");
    expect(refitAction({ recentred: false, resized: true, grown: false, moved: false }, { growth: true })).toBe("glide");
  });

  it("always glides for a re-centring, whatever is held", () => {
    for (const cause of causes.filter((c) => c.recentred)) {
      for (const hold of [{}, { growth: true }, { resize: true }, { growth: true, resize: true }]) {
        expect(refitAction(cause, hold), `${name(cause)} ${name(hold)}`).toBe("glide");
      }
    }
  });

  it("glides only for a re-centring when both are held", () => {
    for (const cause of causes) {
      expect(refitAction(cause, { growth: true, resize: true }), name(cause)).toBe(cause.recentred ? "glide" : "none");
    }
  });
});
