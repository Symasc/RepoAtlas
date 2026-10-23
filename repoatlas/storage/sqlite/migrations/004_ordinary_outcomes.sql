-- RepoAtlas schema v4: the outcome of every ordinary reference claim.
--
-- Counts only, replaced with the graph in the global commit's transaction
-- (repoatlas.graph.linker, commit_global). `ordinary_outcomes` holds how many ordinary
-- claims share one (repository, language, relationship, target kind, outcome, subreason);
-- every column but the count is a closed code, so the table is bounded by the codes
-- (repoatlas.models.ordinary_outcomes) and never by claim text, and carries no path, name
-- or host of any source. `ordinary_accounting` holds the closed-key figures that reconcile
-- the account: the claims considered, those excluded because another planner or the USES
-- stage owns them, the partition and legacy residuals. It is its own account and is not a
-- decomposition of the legacy `unresolved` scalar. The per-claim rows of what is still open
-- are `link_decisions` rows of family `ordinary`.
CREATE TABLE ordinary_outcomes (
    repo_id     INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    language    TEXT    NOT NULL,
    edge_type   TEXT    NOT NULL,
    target_kind TEXT    NOT NULL,
    outcome     TEXT    NOT NULL,
    subreason   TEXT    NOT NULL,
    claims      INTEGER NOT NULL,
    PRIMARY KEY (repo_id, language, edge_type, target_kind, outcome, subreason)
) WITHOUT ROWID;

CREATE TABLE ordinary_accounting (
    key   TEXT    NOT NULL PRIMARY KEY,
    value INTEGER NOT NULL
) WITHOUT ROWID;
