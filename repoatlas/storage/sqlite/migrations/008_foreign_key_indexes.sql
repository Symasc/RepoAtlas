-- RepoAtlas schema v8: an index on every foreign-key child column a delete looks up.
--
-- With foreign keys enforced, deleting a parent row makes SQLite look up each child table
-- by the child column. Without an index that lookup is a full scan per deleted row: deleting
-- one file's entities scanned every unresolved reference once per entity, so a later sync
-- that reparsed N entities cost N full scans (measured 0.26 s per entity at 1.57M references).
-- Index-only: no per-file fact changes, so PARSER_FACTS_VERSION is unchanged and nothing is
-- reparsed. The statement text and order match the documented manual repair exactly
-- (recognition compares each table's indexes in creation order), so a database already
-- carrying these five indexes is recognized and this migration applies nothing to it.
-- edges.repo_id and unresolved_refs.repo_id stay unindexed on purpose: their parent is a
-- repository, deleted only when a repository is removed, one scan per repository.
CREATE INDEX idx_unresolved_src ON unresolved_refs (src_id);
CREATE INDEX idx_link_decisions_src ON link_decisions (src_id);
CREATE INDEX idx_link_decisions_file_id ON link_decisions (file_id);
CREATE INDEX idx_http_call_decisions_src ON http_call_decisions (src_id);
CREATE INDEX idx_http_call_decisions_file_id ON http_call_decisions (file_id);
