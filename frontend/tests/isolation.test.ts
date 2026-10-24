import { mkdirSync, mkdtempSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { describe, expect, it } from "vitest";

import { assertScratchDatabase, operatorDirectories } from "../e2e/isolation";

/**
 * The e2e servers must never open this machine's RepoAtlas index. These tests run the guard against a
 * fake home directory and a fake state directory, so no operator path is ever touched.
 */
function fixture() {
  const root = mkdtempSync(path.join(tmpdir(), "isolation-"));
  const home = path.join(root, "home");
  const state = path.join(root, "state");
  mkdirSync(path.join(home, ".local/share/repoatlas"), { recursive: true });
  mkdirSync(path.join(home, ".config/repoatlas"), { recursive: true });
  mkdirSync(path.join(state, "live/state"), { recursive: true });
  writeFileSync(path.join(home, ".local/share/repoatlas/atlas.db"), "operator");
  return { root, home, state, operator: path.join(home, ".local/share/repoatlas/atlas.db") };
}

describe("e2e state isolation", () => {
  it("accepts a scratch database inside the e2e state directory", () => {
    const { home, state } = fixture();
    expect(() =>
      assertScratchDatabase(path.join(state, "live/state/atlas.db"), state, home),
    ).not.toThrow();
  });

  it("refuses the operator's index itself", () => {
    const { home, state, operator } = fixture();
    expect(() => assertScratchDatabase(operator, state, home)).toThrow(/operator directory/);
  });

  it("refuses a path inside the operator's config or data directories, even before it exists", () => {
    const { home, state } = fixture();
    expect(() =>
      assertScratchDatabase(path.join(home, ".config/repoatlas/new.db"), state, home),
    ).toThrow(/operator directory/);
  });

  it("refuses a symlink that leads into the operator's directory", () => {
    const { home, state } = fixture();
    const link = path.join(state, "live/state/atlas.db");
    symlinkSync(path.join(home, ".local/share/repoatlas/atlas.db"), link);
    expect(() => assertScratchDatabase(link, state, home)).toThrow(/operator directory/);
  });

  it("refuses a relative path that escapes the state directory into the operator's", () => {
    const { home, state } = fixture();
    const escape = path.join(state, "..", "home", ".local/share/repoatlas/atlas.db");
    expect(() => assertScratchDatabase(escape, state, home)).toThrow();
  });

  it("refuses a database outside the state directory, and the state directory itself", () => {
    const { home, state, root } = fixture();
    expect(() => assertScratchDatabase(path.join(root, "elsewhere.db"), state, home)).toThrow(
      /outside the e2e state directory/,
    );
  });

  it("lists both operator directories it guards", () => {
    expect(operatorDirectories("/fake/home")).toEqual([
      "/fake/home/.local/share/repoatlas",
      "/fake/home/.config/repoatlas",
    ]);
  });
});
