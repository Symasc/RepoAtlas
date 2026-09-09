-- RepoAtlas schema v1 (ARCHITECTURE.md §3).
--
-- Provenance lives on the fact row, which is what makes stale-fact removal exact:
-- everything a file asserted is DELETE ... WHERE file_id = ? plus the cascade.

CREATE TABLE repositories (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    NOT NULL,
    path                TEXT    NOT NULL UNIQUE,
    branch              TEXT,
    head_sha            TEXT,
    is_dirty            INTEGER NOT NULL DEFAULT 0,
    languages_json      TEXT    NOT NULL DEFAULT '[]',
    manifests_json      TEXT    NOT NULL DEFAULT '[]',
    lockfiles_json      TEXT    NOT NULL DEFAULT '[]',
    last_indexed_at     TEXT,
    last_indexed_head   TEXT,
    last_indexed_dirty  INTEGER,
    last_sync_status    TEXT    NOT NULL DEFAULT 'never'
);

CREATE TABLE files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id         INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    rel_path        TEXT    NOT NULL,
    size            INTEGER NOT NULL,
    mtime_ns        INTEGER NOT NULL,
    content_hash    TEXT    NOT NULL,
    language        TEXT,
    parser          TEXT,
    parser_version  TEXT    NOT NULL DEFAULT '',
    is_binary       INTEGER NOT NULL DEFAULT 0,
    indexed_at      TEXT,
    UNIQUE (repo_id, rel_path)
);

CREATE INDEX idx_files_repo_path ON files (repo_id, rel_path);
CREATE INDEX idx_files_hash      ON files (content_hash);

-- Two tiers, distinguished by file_id: file-scoped facts cascade away with their
-- file; interned global entities (env vars, packages, repositories) are shared and
-- garbage-collected when no edge references them.
CREATE TABLE entities (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    type            TEXT    NOT NULL,
    key             TEXT    NOT NULL,
    repo_id         INTEGER REFERENCES repositories(id) ON DELETE CASCADE,
    file_id         INTEGER REFERENCES files(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL,
    qualified_name  TEXT,
    line_start      INTEGER,
    line_end        INTEGER,
    signature       TEXT,
    meta_json       TEXT
);

-- Interned entities are unique by key; file-scoped ones are not (the same symbol
-- name may legitimately recur), so the uniqueness constraint is partial.
CREATE UNIQUE INDEX idx_entities_global_key ON entities (key) WHERE file_id IS NULL;
CREATE INDEX idx_entities_key      ON entities (key);
CREATE INDEX idx_entities_file     ON entities (file_id);
CREATE INDEX idx_entities_repo     ON entities (repo_id, type, name);

CREATE TABLE edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    src_id      INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    dst_id      INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    type        TEXT    NOT NULL,
    confidence  REAL    NOT NULL DEFAULT 1.0,
    repo_id     INTEGER REFERENCES repositories(id) ON DELETE CASCADE,
    file_id     INTEGER REFERENCES files(id) ON DELETE CASCADE,
    line_start  INTEGER,
    line_end    INTEGER,
    parser      TEXT
);

CREATE INDEX idx_edges_src  ON edges (src_id, type);
CREATE INDEX idx_edges_dst  ON edges (dst_id, type);
CREATE INDEX idx_edges_file ON edges (file_id);

-- References whose target is not indexed yet. Retried on every link pass, so a
-- repository added later retroactively gains its cross-repository edges.
CREATE TABLE unresolved_refs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id     INTEGER REFERENCES repositories(id) ON DELETE CASCADE,
    file_id     INTEGER REFERENCES files(id) ON DELETE CASCADE,
    src_id      INTEGER REFERENCES entities(id) ON DELETE CASCADE,
    edge_type   TEXT    NOT NULL,
    target_kind TEXT    NOT NULL,
    target_key  TEXT    NOT NULL,
    line_start  INTEGER,
    line_end    INTEGER,
    confidence  REAL    NOT NULL DEFAULT 1.0,
    meta_json   TEXT
);

CREATE INDEX idx_unresolved_target ON unresolved_refs (target_kind, target_key);
CREATE INDEX idx_unresolved_file   ON unresolved_refs (file_id);

CREATE TABLE chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id         INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    repo_id         INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    line_start      INTEGER NOT NULL,
    line_end        INTEGER NOT NULL,
    text            TEXT    NOT NULL,
    token_estimate  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_chunks_file ON chunks (file_id);
CREATE INDEX idx_chunks_repo ON chunks (repo_id);

-- Identifier-aware tokenization: SERVICE_HOST, snake_case, @company/common and
-- module.attr must survive tokenization (ARCHITECTURE.md §3).
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    text,
    content = 'chunks',
    content_rowid = 'id',
    tokenize = "unicode61 remove_diacritics 2 tokenchars '_.-@$'"
);

CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts (rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts (chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts (chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO chunks_fts (rowid, text) VALUES (new.id, new.text);
END;

CREATE VIRTUAL TABLE entities_fts USING fts5(
    name,
    qualified_name,
    content = 'entities',
    content_rowid = 'id',
    tokenize = "unicode61 remove_diacritics 2 tokenchars '_.-@$'"
);

CREATE TRIGGER entities_ai AFTER INSERT ON entities BEGIN
    INSERT INTO entities_fts (rowid, name, qualified_name)
    VALUES (new.id, new.name, new.qualified_name);
END;

CREATE TRIGGER entities_ad AFTER DELETE ON entities BEGIN
    INSERT INTO entities_fts (entities_fts, rowid, name, qualified_name)
    VALUES ('delete', old.id, old.name, old.qualified_name);
END;

CREATE TRIGGER entities_au AFTER UPDATE ON entities BEGIN
    INSERT INTO entities_fts (entities_fts, rowid, name, qualified_name)
    VALUES ('delete', old.id, old.name, old.qualified_name);
    INSERT INTO entities_fts (rowid, name, qualified_name)
    VALUES (new.id, new.name, new.qualified_name);
END;

CREATE TABLE embeddings (
    chunk_id    INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB NOT NULL
);

CREATE TABLE meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
