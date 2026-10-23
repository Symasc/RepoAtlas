-- RepoAtlas schema v6: declarations scoped to one file (and symbol) of their one caller.
--
-- `deployment_links.scope_json`: the words of a scoped declaration (repoatlas.models.
-- deployment_targets.DeclarationScope: path, symbol, base_key, base_shown, base_path), or NULL for
-- an ordinary link. It adds one nullable column that no parse writes, so what a parse stores per
-- file is unchanged and PARSER_FACTS_VERSION does not change: no file is reparsed.
ALTER TABLE deployment_links ADD COLUMN scope_json TEXT;
