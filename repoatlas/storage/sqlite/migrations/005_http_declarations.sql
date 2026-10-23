-- RepoAtlas schema v5: operator declarations for HTTP requests the source leaves unlinked.
--
-- `deployment_links.callers_json`: the repositories whose requests a link may join (empty: any).
-- A link at a loopback origin must name them, because that origin is a different service in each
-- repository's dev setup (repoatlas.models.deployment_targets).
--
-- `declaration_audit`: what operators did to their declarations, append-only. One row per create,
-- update, revoke or import, written in the same transaction as the change it records, holding the
-- validated link fields before and after (never a credential: the fields are validated first).
-- Triggers refuse any UPDATE or DELETE, so history cannot be rewritten by any code path. It is
-- local history, not exported state, and it records actions, never join outcomes: a sync writes
-- nothing here.
ALTER TABLE deployment_links ADD COLUMN callers_json TEXT NOT NULL DEFAULT '[]';

CREATE TABLE declaration_audit (
    id            INTEGER PRIMARY KEY,
    at            TEXT    NOT NULL,
    action        TEXT    NOT NULL CHECK (action IN ('create', 'update', 'revoke', 'import')),
    kind          TEXT    NOT NULL CHECK (kind IN ('deployment_link')),
    subject_id    INTEGER NOT NULL,
    repository    TEXT    NOT NULL,
    via           TEXT    NOT NULL CHECK (via IN ('ui', 'api', 'cli', 'import')),
    request_group TEXT,
    note          TEXT,
    before_json   TEXT,
    after_json    TEXT
);

CREATE INDEX idx_declaration_audit_subject ON declaration_audit (kind, subject_id, id);
CREATE INDEX idx_declaration_audit_repository ON declaration_audit (repository, id);

CREATE TRIGGER declaration_audit_no_update BEFORE UPDATE ON declaration_audit
BEGIN
    SELECT RAISE(ABORT, 'declaration_audit is append-only');
END;

CREATE TRIGGER declaration_audit_no_delete BEFORE DELETE ON declaration_audit
BEGIN
    SELECT RAISE(ABORT, 'declaration_audit is append-only');
END;
