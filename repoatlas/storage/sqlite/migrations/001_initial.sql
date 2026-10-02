-- RepoAtlas schema v1 (ARCHITECTURE.md §3).
--
-- Provenance lives on the fact row, which is what makes stale-fact removal exact:
-- everything a file asserted is DELETE ... WHERE file_id = ? plus the cascade.

-- `slug` is a repository's identity: its canonical path relative to the managed
-- root. Unique by construction, deterministic, reproducible by a fresh index of
-- the same tree, and independent of discovery order (see `repository_slug` for
-- why a row id, a UUID and a remote URL were each rejected). `name` is only what
-- it is called: two managed repositories may both be called `common`.
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
    last_sync_status    TEXT    NOT NULL DEFAULT 'never',
    slug                TEXT    NOT NULL DEFAULT ''
);

-- Partial: a row has no identity until a sync assigns one, because the root is a
-- configuration value SQL cannot see. Assigned slugs must be unique.
CREATE UNIQUE INDEX idx_repositories_slug ON repositories (slug) WHERE slug != '';

-- `basename` makes a file's name a retrievable fact. It is generated rather than
-- written by the indexer, so it cannot drift from `rel_path`, and indexed, so a
-- filename lookup is a seek rather than a scan of every indexed file.
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
    basename        TEXT
        GENERATED ALWAYS AS (
            substr(rel_path, length(rtrim(rel_path, replace(rel_path, '/', ''))) + 1)
        ) VIRTUAL,
    UNIQUE (repo_id, rel_path)
);

CREATE INDEX idx_files_repo_path ON files (repo_id, rel_path);
CREATE INDEX idx_files_hash      ON files (content_hash);
CREATE INDEX idx_files_basename  ON files (basename);

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

-- `resolution` records how the global link pass resolved an edge whose use reaches
-- its declaration through configuration and re-exports rather than its literal
-- text: a workspace package and its entry, a tsconfig path alias, a barrel that
-- re-exports the name, or a package another repository provides. A small JSON
-- object (its basis, the files passed through, the names spelled on the way and,
-- across repositories, the manifest evidence), so such an edge can be read back to
-- what proved it. NULL on every other edge.
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
    parser      TEXT,
    resolution  TEXT
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

-- A chunk knows where it sits in the span it was cut from. Content with no line
-- structure (a minified bundle, a base64 blob) is cut at a fixed character count,
-- and a literal straddling a cut would exist in no single chunk. `span_index`
-- identifies the line span within its file, `split_index` the piece within that
-- span, and `split_total` how many pieces the span became, so search can stitch
-- adjacent pieces back across a seam. The defaults describe an unsplit chunk.
CREATE TABLE chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id         INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    repo_id         INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    line_start      INTEGER NOT NULL,
    line_end        INTEGER NOT NULL,
    text            TEXT    NOT NULL,
    token_estimate  INTEGER NOT NULL DEFAULT 0,
    span_index      INTEGER NOT NULL DEFAULT 0,
    split_index     INTEGER NOT NULL DEFAULT 0,
    split_total     INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX idx_chunks_file ON chunks (file_id);
CREATE INDEX idx_chunks_repo ON chunks (repo_id);

-- Seam recovery reads only split spans, in span order. `split_total` leads
-- deliberately. A partial index (`WHERE split_total > 1`) would be far smaller, but
-- SQLite will not choose one without a constraint on its leading column, and
-- without table statistics it cannot know the partial index is nearly empty:
-- measured, it fell back to scanning every chunk (7.2 ms per literal search at 50k
-- chunks, against 0.25 ms here, growing with the index). Leading on the
-- predicate's own column makes `split_total > 1` a range seek the planner takes on
-- its own, with no `INDEXED BY` hint tying the query to an index name.
CREATE INDEX idx_chunks_split
    ON chunks (split_total, repo_id, file_id, span_index, split_index);

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

-- The dependency layer (V1_SPEC §13). Declared and resolved versions are separate
-- rows rather than columns of one row, because a package can be declared without
-- being locked (no lockfile yet) and locked without being declared (a transitive
-- dependency). `provided` is the third role: the package this repository *is*, the
-- other half of cross-repository matching (V1_SPEC §9): repository A depends on
-- repository B only when A declares a name B provides, in the same ecosystem,
-- exactly. Provenance lives on the row like every other fact, so a dependency
-- cannot outlive the manifest that stated it.
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

-- Which export tables each file's uses read. USES edges are kept between syncs and
-- replaced one consumer file at a time (repoatlas/graph/uses_link.py). A use is
-- traced through the export tables of every file it passes (the module it names,
-- each re-exporting barrel, every `export *` branch it searched, found or not), so
-- a change to one of those tables can change a use in another file. Each row says:
-- this consumer file's uses read that file's export table, so a sync that changes
-- what a file exports resolves again exactly the files that read it. Written only
-- by the USES stage, in the same transaction as the edges.
CREATE TABLE use_reads (
    file_id      INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    read_file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE
);

CREATE INDEX idx_use_reads_read ON use_reads (read_file_id);
CREATE INDEX idx_use_reads_file ON use_reads (file_id);

-- The decision the HTTP join made for every request. Derived, never authoritative:
-- the global pass decides every request claim (repoatlas/graph/http_crossings.py)
-- and the global commit replaces every row here in the same transaction as the
-- HTTP edges, so a reader sees one pass's decisions or the previous one's, never a
-- mix. One row per request claim and injected root: literal alternatives are claims
-- of their own, and the requests an injected transport makes per root are told
-- apart by `variant`, so no decision overwrites another.
--
-- `decision_ref` is the row's identity: opaque, and the same for the same request
-- written the same way after any sync. A linked row's `CALLS_HTTP_HANDLER` edge
-- carries it in its resolution; a refused row has no edge. `chain_json` holds the
-- ordered hops (repoatlas/graph/http_chain.py) with each hop's provenance by
-- repository, path and lines.
CREATE TABLE http_call_decisions (
    decision_ref  TEXT    NOT NULL PRIMARY KEY,
    repo_id       INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    src_id        INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    line_start    INTEGER,
    line_end      INTEGER,
    method        TEXT    NOT NULL,
    url           TEXT    NOT NULL,
    variant       TEXT    NOT NULL,
    decision      TEXT    NOT NULL,
    evidence_json TEXT    NOT NULL,
    chain_json    TEXT    NOT NULL
);

CREATE INDEX idx_http_call_decisions_file ON http_call_decisions (repo_id, file_id, line_start);

CREATE TABLE meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
