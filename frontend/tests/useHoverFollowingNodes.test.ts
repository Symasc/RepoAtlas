import { describe, expect, it } from "vitest";

import { nextHover } from "@/components/graph/useHoverFollowingNodes";

// The hook is this rule applied when its nodes change; the unit environment has
// no DOM to render it in, so the rule itself is what is pinned here.
describe("the hover that follows the nodes", () => {
  const nodes = [{ id: "repo:a" }, { id: "repo:b" }];

  it("keeps a hover whose node is still there", () => {
    expect(nextHover("repo:b", nodes)).toBe("repo:b");
  });

  it("clears a hover whose node has left, as a summary does when opened by its own button", () => {
    expect(nextHover("repo:c", nodes)).toBeNull();
  });

  it("leaves no hover as none", () => {
    expect(nextHover(null, nodes)).toBeNull();
  });

  it("clears every hover when there are no nodes", () => {
    expect(nextHover("repo:a", [])).toBeNull();
    expect(nextHover(null, [])).toBeNull();
  });
});
