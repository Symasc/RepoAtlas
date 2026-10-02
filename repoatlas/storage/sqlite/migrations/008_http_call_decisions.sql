-- RepoAtlas schema v8 — the decision the HTTP join made for every request.
--
-- Derived, never authoritative: the global pass decides every request claim
-- (repoatlas/graph/http_crossings.py) and the global commit replaces every row here in
-- the same transaction as the HTTP edges, so a reader sees one pass's decisions or the
-- previous one's, never a mix. One row per request claim and injected root: literal
-- alternatives are claims of their own, and the requests an injected transport makes
-- per root are told apart by `variant`, so no decision overwrites another.
--
-- `decision_ref` is the row's identity: opaque, and the same for the same request
-- written the same way after any sync. A linked row's `CALLS_HTTP_HANDLER` edge carries
-- it in its resolution; a refused row has no edge. `chain_json` holds the ordered hops
-- (repoatlas/graph/http_chain.py) with each hop's provenance by repository, path and lines.
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

-- No backfill: the table starts empty until the next global pass fills it, and the
-- `http_call_decisions` meta key records that one has.
