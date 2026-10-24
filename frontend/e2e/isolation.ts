import { existsSync, realpathSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import path from "node:path";

/**
 * Fail-closed guard for the e2e servers' RepoAtlas state. Every database an e2e server opens must
 * resolve (through symlinks) to a file inside the e2e state directory, and never to anything inside
 * this machine's RepoAtlas config or data directories, where the operator's own index lives.
 *
 * A path that cannot be resolved is refused too: the guard never guesses.
 */
export function operatorDirectories(home: string = homedir()): string[] {
  return [path.join(home, ".local/share/repoatlas"), path.join(home, ".config/repoatlas")];
}

/**
 * The real location of a path, following symlinks. A file the e2e build has not written yet has no
 * real location of its own, so its nearest existing ancestor is resolved and the rest appended.
 * Throws only when no ancestor exists at all.
 */
function resolved(target: string): string {
  const absolute = path.resolve(target);
  let existing = absolute;
  const rest: string[] = [];
  while (!existsSync(existing)) {
    const parent = path.dirname(existing);
    if (parent === existing) throw new Error(`no existing ancestor for ${target}`);
    rest.unshift(path.basename(existing));
    existing = parent;
  }
  return path.join(realpathSync(existing), ...rest);
}

function within(child: string, parent: string): boolean {
  const relative = path.relative(parent, child);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

/** Throws unless ``database`` is a scratch file inside ``stateDir`` and outside every operator directory. */
export function assertScratchDatabase(
  database: string,
  stateDir: string,
  home: string = homedir(),
): void {
  let real: string;
  let state: string;
  try {
    real = resolved(database);
    state = resolved(stateDir);
  } catch {
    throw new Error(`refusing e2e database ${database}: its location cannot be resolved`);
  }
  for (const operator of operatorDirectories(home)) {
    let op: string;
    try {
      op = resolved(operator);
    } catch {
      op = path.resolve(operator);
    }
    if (within(real, op) || within(op, real)) {
      throw new Error(`refusing e2e database ${database}: it is inside an operator directory`);
    }
  }
  if (!within(real, state)) {
    throw new Error(`refusing e2e database ${database}: it is outside the e2e state directory`);
  }
}

/** The state directory the e2e servers write into: fixed under the system temp directory. */
export const E2E_STATE = path.join(tmpdir(), "repoatlas-explorer-e2e");
