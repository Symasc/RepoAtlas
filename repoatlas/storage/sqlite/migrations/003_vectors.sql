-- RepoAtlas schema v3 — the vector layer (V1_SPEC §14, ARCHITECTURE.md §3).
--
-- `chunks.content_hash` completes the chunk row ARCHITECTURE.md §3 always
-- described. It is what makes embedding incremental at *chunk* granularity: a
-- file is reparsed as a whole, so its chunk rows are replaced wholesale and get
-- new ids, but a chunk whose text did not change keeps its hash.
ALTER TABLE chunks ADD COLUMN content_hash TEXT NOT NULL DEFAULT '';
CREATE INDEX idx_chunks_hash ON chunks (content_hash);

-- Vectors are keyed by (content_hash, model), not by chunk_id.
--
-- Keying by chunk_id would re-embed every chunk of any file that was reindexed,
-- including the chunks whose text is byte-identical — which V1_SPEC §20 forbids
-- outright ("never re-embed unchanged data"). Editing one line of a large file
-- would otherwise re-embed the whole file.
--
-- The cost is that cleanup is a sweep rather than a cascade: a vector is removed
-- when no chunk anywhere still has its hash. That is safe because a vector is not
-- a fact and carries no provenance — it is a ranking aid derived from chunk text,
-- so an orphan is wasted space, never a stale claim (contrast §3's rule for
-- entities and edges, which do cascade).
DROP TABLE embeddings;

CREATE TABLE embeddings (
    content_hash TEXT    NOT NULL,
    model        TEXT    NOT NULL,
    dim          INTEGER NOT NULL,
    vector       BLOB    NOT NULL,
    PRIMARY KEY (content_hash, model)
) WITHOUT ROWID;

CREATE INDEX idx_embeddings_model ON embeddings (model);
