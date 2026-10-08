-- RepoAtlas schema v2: link decisions.
--
-- Why each claim of a link family did, or did not, become an edge: one row per subject
-- the global pass decided, replaced with the edges it accounts for, in the same
-- transaction (repoatlas.graph.linker, commit_global). `family` names the pass that
-- decided it (`graphql_codegen` first); `subject` is that family's own spelling of what
-- was decided, and `evidence_json` the source facts it rests on. A refusal is as much a
-- fact as a link: it says what was read and why no edge follows from it.
CREATE TABLE link_decisions (
    decision_ref  TEXT    NOT NULL PRIMARY KEY,
    family        TEXT    NOT NULL,
    repo_id       INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    src_id        INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    line_start    INTEGER,
    line_end      INTEGER,
    subject       TEXT    NOT NULL,
    decision      TEXT    NOT NULL,
    evidence_json TEXT    NOT NULL
);

CREATE INDEX idx_link_decisions_family ON link_decisions (family, decision);
CREATE INDEX idx_link_decisions_file ON link_decisions (repo_id, file_id, line_start);
