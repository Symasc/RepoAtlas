-- RepoAtlas schema v2 — the dependency layer (V1_SPEC §13, ARCHITECTURE.md §3).
--
-- Declared and resolved versions are separate rows rather than columns of one
-- row, because a package can be declared without being locked (no lockfile yet)
-- and locked without being declared (a transitive dependency). Storing them as
-- one row would force inventing one half of every such pair.
--
-- `provided` is the third role: the package this repository *is*. It is the other
-- half of cross-repository matching (V1_SPEC §9) — repo A depends on repo B only
-- when A declares a name B provides, in the same ecosystem, exactly.
--
-- Provenance lives on the row like every other fact, so a dependency cannot
-- outlive the manifest that stated it.
CREATE TABLE dependencies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id         INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    file_id         INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ecosystem       TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    normalized_name TEXT    NOT NULL,
    role            TEXT    NOT NULL,   -- declared | resolved | provided
    constraint_text TEXT,
    version         TEXT,
    group_name      TEXT,
    line_start      INTEGER NOT NULL DEFAULT 1,
    line_end        INTEGER NOT NULL DEFAULT 1,
    parser          TEXT
);

CREATE INDEX idx_dependencies_repo   ON dependencies (repo_id, ecosystem, normalized_name);
CREATE INDEX idx_dependencies_file   ON dependencies (file_id);
CREATE INDEX idx_dependencies_lookup ON dependencies (ecosystem, normalized_name, role);
