-- RepoAtlas schema v7: what operators state about external services, redirects and callbacks.
--
-- `service_declarations`: one row per declaration an operator makes (repoatlas.models.
-- service_declarations): an origin classified as an external service, a redirect from an origin or
-- a route to a destination, or an OAuth callback the provider permits. `fields_json` holds the
-- validated fields of the one kind, `identity_key` the words that cannot change on update (the
-- origin, the redirect source, the callback provider origin). Only an external service is unique
-- by its identity: two redirects may leave one source for different destinations. The ids are
-- never reused, so the audit rows below always name the declaration they were written about.
-- No parse, sync or link pass reads or writes this table, so PARSER_FACTS_VERSION does not change.
--
-- `service_declaration_audit`: what operators did to these declarations, append-only, one row per
-- create, update, revoke or import, written in the same transaction as the change it records,
-- holding the validated fields before and after (never a credential: the fields are validated
-- first). Triggers refuse any UPDATE or DELETE, so history cannot be rewritten by any code path.
-- It records actions, never join outcomes: a sync writes nothing here.
CREATE TABLE service_declarations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT    NOT NULL
                  CHECK (kind IN ('external_service', 'redirect', 'permitted_callback')),
    identity_key  TEXT    NOT NULL,
    fields_json   TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE UNIQUE INDEX idx_service_declarations_external
    ON service_declarations (identity_key) WHERE kind = 'external_service';

CREATE INDEX idx_service_declarations_kind ON service_declarations (kind, identity_key);

CREATE TABLE service_declaration_audit (
    id            INTEGER PRIMARY KEY,
    at            TEXT    NOT NULL,
    action        TEXT    NOT NULL CHECK (action IN ('create', 'update', 'revoke', 'import')),
    kind          TEXT    NOT NULL
                  CHECK (kind IN ('external_service', 'redirect', 'permitted_callback')),
    subject_id    INTEGER NOT NULL,
    subject       TEXT    NOT NULL,
    via           TEXT    NOT NULL CHECK (via IN ('ui', 'api', 'cli', 'import')),
    request_group TEXT,
    note          TEXT,
    before_json   TEXT,
    after_json    TEXT
);

CREATE INDEX idx_service_declaration_audit_subject
    ON service_declaration_audit (kind, subject_id, id);

CREATE TRIGGER service_declaration_audit_no_update BEFORE UPDATE ON service_declaration_audit
BEGIN
    SELECT RAISE(ABORT, 'service_declaration_audit is append-only');
END;

CREATE TRIGGER service_declaration_audit_no_delete BEFORE DELETE ON service_declaration_audit
BEGIN
    SELECT RAISE(ABORT, 'service_declaration_audit is append-only');
END;
