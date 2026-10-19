-- RepoAtlas schema v3: deployment links.
--
-- The one table a user writes rather than a sync: where a repository is deployed, under
-- which conditions, and the values of the variables a discovered deployment target
-- leaves open (repoatlas.models.deployment_targets). It is keyed by the repository's
-- slug, not its row, so removing a repository from the index never removes what a user
-- stated about it; a link whose target is no longer discovered is shown as detached.
-- No sync, reindex or link pass reads or writes it.
CREATE TABLE deployment_links (
    id              INTEGER PRIMARY KEY,
    repository      TEXT    NOT NULL,
    label           TEXT    NOT NULL,
    environment     TEXT,
    url             TEXT,
    target_id       TEXT,
    variables_json  TEXT    NOT NULL DEFAULT '{}',
    conditions_json TEXT    NOT NULL DEFAULT '[]',
    verification    TEXT    NOT NULL DEFAULT 'unverified'
                    CHECK (verification IN ('unverified', 'user_confirmed')),
    verified_at     TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE INDEX idx_deployment_links_repository ON deployment_links (repository, id);
