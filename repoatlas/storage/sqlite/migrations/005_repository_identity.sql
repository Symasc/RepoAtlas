-- RepoAtlas schema v5 — repository identity (V1_SPEC §5, ARCHITECTURE.md §3).
--
-- A repository's display name was also its identity. Two managed repositories
-- called `common` — `teamA/common` and `teamB/common`, both legitimate — were
-- therefore one thing to the graph (`repo:common`), to entity keys
-- (`file:common:index.ts`, `sym:common:index.ts:SHARED_NAME:1`), to rank fusion,
-- to deduplication, to grounding and to the terminal.
--
-- `slug` is the identity: the repository's canonical path relative to the managed
-- root. Unique by construction, deterministic, reproducible by a fresh index of
-- the same tree, and independent of discovery order — see `repository_slug` for
-- why a row id, a UUID and a remote URL were each rejected.
ALTER TABLE repositories ADD COLUMN slug TEXT NOT NULL DEFAULT '';

-- Partial, because an upgraded row has no identity until the next sync assigns
-- one: the root is a *configuration* value that SQL cannot see, so backfilling it
-- here would mean writing host-absolute paths into every key. Empty slugs are
-- allowed to coexist; assigned ones must be unique.
CREATE UNIQUE INDEX idx_repositories_slug ON repositories (slug) WHERE slug != '';

-- Repository graph nodes are keyed by name, so on an upgraded database they are
-- wrong and, for duplicated names, shared between repositories. They are dropped
-- rather than rewritten: `ensure_repository_entity` recreates each one on the next
-- sync, and every edge touching them is produced by the global link pass, which
-- deletes and rebuilds its own output on every run (ARCHITECTURE.md §5). Nothing
-- durable is lost — unlike file-scoped facts, a repository node carries no
-- provenance of its own.
--
-- File and symbol entity keys are equally wrong on an upgraded database, and are
-- rebuilt by a different mechanism: the schema version participates in each file's
-- stored `parser_version`, so bumping it reparses every file and rewrites every
-- file-scoped key with the new identity (ARCHITECTURE.md §9).
DELETE FROM entities WHERE type = 'repository' AND file_id IS NULL;
