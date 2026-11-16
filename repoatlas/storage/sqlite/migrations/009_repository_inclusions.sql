-- RepoAtlas schema v9: repository inclusions (submodules and the manifests that state them).
--
-- `repositories.remotes_json`: the identities of a repository's remotes (repoatlas.gitstate.
-- config.normalize_git_url), `origin` first, read from its Git configuration on each sync. Only
-- identities are stored, never a URL as written, so no credential a remote embeds is kept.
-- `repositories.gitlinks_json`: the commit each submodule path is pinned at in its index, as
-- `{"path": "sha"}`. Both are what the global pass reads to resolve an inclusion to the indexed
-- repository it names (repoatlas.graph.inclusions); both are refreshed by every sync.
--
-- `inclusion_declarations`: what users state about an inclusion the index could not settle on its
-- own -- that the entry a parent repository's file declares at a path is one indexed repository.
-- Written only by a person (the API, an import), never by a sync; the global pass reads it as a
-- fallback and never lets it override a path or remote the index proves. Keyed by slugs and
-- paths, so a repository removed and indexed again finds its declarations where it left them.
--
-- `inclusion_declaration_audit`: what users did to those declarations, append-only, written in the
-- same transaction as the change it records. Triggers refuse any UPDATE or DELETE.
ALTER TABLE repositories ADD COLUMN remotes_json TEXT NOT NULL DEFAULT '[]';

ALTER TABLE repositories ADD COLUMN gitlinks_json TEXT NOT NULL DEFAULT '{}';

CREATE TABLE inclusion_declarations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    repository    TEXT    NOT NULL,
    file          TEXT    NOT NULL,
    path          TEXT    NOT NULL,
    target        TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE UNIQUE INDEX idx_inclusion_declarations_entry
    ON inclusion_declarations (repository, file, path);

CREATE TABLE inclusion_declaration_audit (
    id            INTEGER PRIMARY KEY,
    at            TEXT    NOT NULL,
    action        TEXT    NOT NULL CHECK (action IN ('create', 'update', 'revoke', 'import')),
    subject_id    INTEGER NOT NULL,
    repository    TEXT    NOT NULL,
    via           TEXT    NOT NULL CHECK (via IN ('ui', 'api', 'cli', 'import')),
    note          TEXT,
    before_json   TEXT,
    after_json    TEXT
);

CREATE INDEX idx_inclusion_declaration_audit_subject
    ON inclusion_declaration_audit (subject_id, id);

CREATE TRIGGER inclusion_declaration_audit_no_update BEFORE UPDATE ON inclusion_declaration_audit
BEGIN
    SELECT RAISE(ABORT, 'inclusion_declaration_audit is append-only');
END;

CREATE TRIGGER inclusion_declaration_audit_no_delete BEFORE DELETE ON inclusion_declaration_audit
BEGIN
    SELECT RAISE(ABORT, 'inclusion_declaration_audit is append-only');
END;
