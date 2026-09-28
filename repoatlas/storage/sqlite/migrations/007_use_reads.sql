-- RepoAtlas schema v7 — which export tables each file's uses read.
--
-- USES edges are kept between syncs and replaced one consumer file at a time
-- (repoatlas/graph/uses_link.py). A use is traced through the export tables of every
-- file it passes -- the module it names, each re-exporting barrel, every `export *`
-- branch it searched, found or not -- so a change to one of those tables can change a
-- use in another file. Each row says: this consumer file's uses read that file's export
-- table. A sync that changes what a file exports resolves again exactly the files that
-- read it, rather than every use in the workspace.
--
-- Written only by the USES stage, for the files it resolves, in the same transaction
-- as their edges. Both ends are files, and a row goes with either.
CREATE TABLE use_reads (
    file_id      INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    read_file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE
);

CREATE INDEX idx_use_reads_read ON use_reads (read_file_id);
CREATE INDEX idx_use_reads_file ON use_reads (file_id);

-- No backfill: the table starts empty, so nothing may be concluded from it until every
-- use has been resolved once. Forgetting whether the kept USES edges are complete makes
-- the next sync resolve every use, which writes every row.
DELETE FROM meta WHERE key = 'uses_links';
