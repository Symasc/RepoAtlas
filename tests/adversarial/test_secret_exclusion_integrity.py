"""Built-in secret exclusion, at every stage and in every case (V1_SPEC §17).

Three invariants, and the third is the one this suite was written for:

    Built-in obvious-secret filename checks are security policy, not user
    preference, and cannot be negated by ``.repoatlasignore``.

    An excluded secret file is rejected before its contents are parsed, chunked,
    embedded, retrieved, serialized into prompts, or logged.

    Filename-case variation cannot bypass a built-in secret exclusion.

The guarantee being defended is narrow and stated exactly: RepoAtlas excludes
files whose *names* match its built-in obvious-secret policy, before their
contents enter the pipeline. A credential pasted into ``app.py`` is not covered,
and no test here pretends otherwise.

Every fixture value is a harmless marker. No real key material appears anywhere
in this repository.
"""

from __future__ import annotations

import fnmatch
import json
import os
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import pytest
from repoatlas.cli.main import app
from repoatlas.config.settings import SECRET_PATTERNS, Settings
from repoatlas.discovery.ignore import is_secret_path
from repoatlas.llm.scripted import ScriptedBackend
from repoatlas.services.init_service import initialize
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import fact_snapshot, git, init_git_repository
from typer.testing import CliRunner

pytestmark = pytest.mark.adversarial

runner = CliRunner()


# -- the fixture -------------------------------------------------------------

#: Files the built-in policy must exclude, each with a unique harmless marker so
#: a leak names the pattern that failed rather than merely that something failed.
SECRET_FILES: dict[str, str] = {
    # dotenv family
    ".env": "REPOATLAS_SECRET_CANARY_ENV_001",
    ".ENV": "REPOATLAS_SECRET_CANARY_ENV_UPPER_001",
    ".Env": "REPOATLAS_SECRET_CANARY_ENV_MIXED_001",
    ".eNv": "REPOATLAS_SECRET_CANARY_ENV_ODD_001",
    ".env.production": "REPOATLAS_SECRET_CANARY_ENVPROD_001",
    ".ENV.PRODUCTION": "REPOATLAS_SECRET_CANARY_ENVPROD_UPPER_001",
    ".Env.Production": "REPOATLAS_SECRET_CANARY_ENVPROD_MIXED_001",
    ".env.local": "REPOATLAS_SECRET_CANARY_ENVLOCAL_001",
    ".ENV.LOCAL": "REPOATLAS_SECRET_CANARY_ENVLOCAL_UPPER_001",
    # ssh private keys
    "id_rsa": "REPOATLAS_SECRET_CANARY_RSA_002",
    "ID_RSA": "REPOATLAS_SECRET_CANARY_RSA_UPPER_002",
    "Id_Rsa": "REPOATLAS_SECRET_CANARY_RSA_MIXED_002",
    "id_ed25519": "REPOATLAS_SECRET_CANARY_ED_002",
    "ID_ED25519": "REPOATLAS_SECRET_CANARY_ED_UPPER_002",
    "Id_Ed25519": "REPOATLAS_SECRET_CANARY_ED_MIXED_002",
    # pem
    "server.pem": "REPOATLAS_SECRET_CANARY_PEM_003",
    "server.PEM": "REPOATLAS_SECRET_CANARY_PEM_UPPER_003",
    "server.PeM": "REPOATLAS_SECRET_CANARY_PEM_MIXED_003",
    "tls.private.PEM": "REPOATLAS_SECRET_CANARY_PEM_MULTI_003",
    # key
    "private.key": "REPOATLAS_SECRET_CANARY_KEY_004",
    "private.KEY": "REPOATLAS_SECRET_CANARY_KEY_UPPER_004",
    "private.Key": "REPOATLAS_SECRET_CANARY_KEY_MIXED_004",
    "service.production.KEY": "REPOATLAS_SECRET_CANARY_KEY_MULTI_004",
    # the same policy, at depth
    "config/.ENV": "REPOATLAS_SECRET_CANARY_NESTED_ENV_005",
    "services/api/.Env.production": "REPOATLAS_SECRET_CANARY_NESTED_ENVPROD_005",
    "deploy/certs/server.PEM": "REPOATLAS_SECRET_CANARY_NESTED_PEM_005",
    "users/alice/ID_RSA": "REPOATLAS_SECRET_CANARY_NESTED_RSA_005",
    "nested/deeper/still/private.KEY": "REPOATLAS_SECRET_CANARY_NESTED_KEY_005",
}

#: Near-misses. Every one of them must stay indexable: over-exclusion silently
#: loses real evidence, which is its own kind of wrong answer.
SAFE_FILES: dict[str, str] = {
    "environment.txt": "REPOATLAS_SAFE_MARKER_ENVIRONMENT",
    "env.py": "REPOATLAS_SAFE_MARKER_ENVPY = 1",
    "monkey.py": "REPOATLAS_SAFE_MARKER_MONKEY = 1",
    "keyboard.txt": "REPOATLAS_SAFE_MARKER_KEYBOARD",
    "key_rotation.md": "REPOATLAS_SAFE_MARKER_KEYROTATION",
    "pem_notes.md": "REPOATLAS_SAFE_MARKER_PEMNOTES",
    "id_rsa_notes.txt": "REPOATLAS_SAFE_MARKER_RSANOTES",
    "id_ed25519_notes.txt": "REPOATLAS_SAFE_MARKER_EDNOTES",
    "dotenv.md": "REPOATLAS_SAFE_MARKER_DOTENV",
    "my.env.backup": "REPOATLAS_SAFE_MARKER_ENVBACKUP",
    "notes.pem.txt": "REPOATLAS_SAFE_MARKER_PEMTXT",
    "server.pem.txt": "REPOATLAS_SAFE_MARKER_PEMDOTTXT",
    ".envrc": "REPOATLAS_SAFE_MARKER_ENVRC",
    "id_rsa.pub": "REPOATLAS_SAFE_MARKER_RSAPUB",
    "id_ed25519.pub": "REPOATLAS_SAFE_MARKER_EDPUB",
    "app.py": "REPOATLAS_SAFE_MARKER_APP = 1",
}

CANARIES = tuple(SECRET_FILES.values())


def write_fixture(repo: Path) -> Path:
    for relative, marker in {**SECRET_FILES, **SAFE_FILES}.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{marker}\n", encoding="utf-8")
    return repo


def _settings(tmp_path: Path, tag: str) -> Settings:
    return Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / f"{tag}-state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )


def _indexed(tmp_path: Path, tag: str, root: Path) -> Workspace:
    space = open_workspace(settings=_settings(tmp_path, tag), require_root=False)
    initialize(space, root, config_path=tmp_path / f"{tag}.yaml")
    return space


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    """One managed repository holding every secret and near-miss name."""
    root = tmp_path / "vault-root"
    repo = root / "vault"
    repo.mkdir(parents=True)
    write_fixture(repo)
    init_git_repository(repo)
    return root


@pytest.fixture
def vault(tmp_path: Path, vault_root: Path) -> Iterator[Workspace]:
    space = _indexed(tmp_path, "vault", vault_root)
    yield space
    space.close()


def indexed_paths(space: Workspace) -> set[str]:
    return {str(r["rel_path"]) for r in space.database.execute("SELECT rel_path FROM files")}


def chunk_text(space: Workspace) -> str:
    return "\n".join(str(r["text"]) for r in space.database.execute("SELECT text FROM chunks"))


def leaked_canaries(haystack: str) -> list[str]:
    return [canary for canary in CANARIES if canary in haystack]


# == 1. the policy function ==================================================


@pytest.mark.parametrize("name", sorted(SECRET_FILES))
def test_every_secret_name_is_recognized_whatever_its_case(name: str) -> None:
    """The finding itself: `.ENV` is `.env`, and a filesystem that says otherwise
    is not a security boundary."""
    assert is_secret_path(name), name
    assert is_secret_path(PurePosixPath(name).name), name
    assert is_secret_path(f"a/b/c/{PurePosixPath(name).name}"), name


@pytest.mark.parametrize("name", sorted(SAFE_FILES))
def test_a_near_miss_name_is_not_excluded(name: str) -> None:
    """Case-insensitivity must not become an over-broad substring filter.

    `monkey.py` ends in neither `.key` nor `key`; `id_rsa_notes.txt` is notes
    *about* a key, not one; `.envrc` is direnv configuration and is deliberately
    outside the V1 policy.
    """
    assert not is_secret_path(name), name


@pytest.mark.parametrize(
    "name",
    [
        "server.pem.txt",
        "notes.pem.txt",
        "backup.KEY.zip",
        "my.env.backup",
        "MY.ENV.BACKUP",
        "envfile",
        "ENV",
        "keyring.rb",
        "monkey.KEY.md",
    ],
)
def test_suffix_matching_is_a_real_suffix_not_a_substring(name: str) -> None:
    """`*.pem` means the basename *ends* with `.pem`, in any case — not that the
    letters appear somewhere in it."""
    assert not is_secret_path(name), name


@pytest.mark.parametrize(
    "name",
    ["tls.private.PEM", "service.production.KEY", "a.b.c.d.pem", "A.B.C.D.KEY"],
)
def test_multiple_dots_still_match_the_final_suffix(name: str) -> None:
    assert is_secret_path(name), name


def test_the_whole_built_in_pattern_list_is_case_insensitive() -> None:
    """Audits the policy as a list, so a pattern added later is covered too.

    Fixing the six names in the review report would leave the next pattern
    someone adds matching case-sensitively again.
    """
    for pattern in SECRET_PATTERNS:
        sample = pattern.replace("*", "sample").replace(".*", ".sample")
        for variant in (sample, sample.upper(), sample.lower(), sample.title()):
            assert is_secret_path(variant), (pattern, variant)


def test_the_stored_path_is_never_rewritten_for_comparison(vault_root: Path) -> None:
    """Case folding is a comparison, not a normalization: a skipped file keeps
    its real spelling, because identity and security matching are different
    concerns."""
    from repoatlas.sync.scanner import scan_repository

    scanned = {f.rel_path for f in scan_repository(vault_root / "vault")}
    assert "server.pem.txt" in scanned  # spelled exactly as on disk
    assert "id_rsa.pub" in scanned


# == 2. exclusion happens before the file is read ============================


def test_an_excluded_file_is_never_opened(tmp_path: Path, vault_root: Path) -> None:
    """The boundary is the point of this pass.

    A 5 KB private key must not be read and then discarded — a file that is never
    opened cannot leak through an exception message, a log line, or a partially
    built chunk.
    """
    opened: list[str] = []
    real_read_bytes = Path.read_bytes
    real_open = Path.open

    def record_read(self: Path, *args: object, **kwargs: object) -> bytes:
        opened.append(str(self))
        return real_read_bytes(self)

    def record_open(self: Path, *args: object, **kwargs: object):
        opened.append(str(self))
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    Path.read_bytes = record_read  # type: ignore[method-assign]
    Path.open = record_open  # type: ignore[method-assign]
    try:
        space = _indexed(tmp_path, "unopened", vault_root)
        space.close()
    finally:
        Path.read_bytes = real_read_bytes  # type: ignore[method-assign]
        Path.open = real_open  # type: ignore[method-assign]

    # Exact path comparison, not substring: `vault/.env` is a prefix of
    # `vault/.envrc`, which is a safe file and *is* legitimately read.
    for secret in SECRET_FILES:
        suffix = f"/vault/{secret}"
        assert not [path for path in opened if path.endswith(suffix)], secret
    # The control: safe files really were read, so the assertion above is not
    # passing because nothing was read at all.
    assert [path for path in opened if path.endswith("/vault/app.py")]
    assert [path for path in opened if path.endswith("/vault/.envrc")]


# == 3. the database, stage by stage =========================================


def test_no_secret_name_reaches_the_files_table(vault: Workspace) -> None:
    """If a secret file reached `files`, the boundary was already too late."""
    assert indexed_paths(vault).isdisjoint(SECRET_FILES)


def test_every_safe_near_miss_is_still_indexed(vault: Workspace) -> None:
    indexed = indexed_paths(vault)
    assert set(SAFE_FILES).issubset(indexed), set(SAFE_FILES) - indexed


def test_no_canary_reaches_a_chunk(vault: Workspace) -> None:
    assert leaked_canaries(chunk_text(vault)) == []


def test_no_canary_is_searchable(vault: Workspace) -> None:
    for canary in CANARIES:
        assert vault.search.search_literal(canary) == [], canary


def test_no_secret_path_is_searchable_by_filename(vault: Workspace) -> None:
    for secret in SECRET_FILES:
        name = PurePosixPath(secret).name
        assert not [hit for hit in vault.search.search_literal(name) if hit.path in SECRET_FILES]


def test_no_secret_file_becomes_an_entity(vault: Workspace) -> None:
    """Exact comparison, not `LIKE`: SQLite's `LIKE` is case-insensitive for
    ASCII, so a substring probe would report `.env` as matching `.ENV` and
    manufacture its own failure."""
    keys = {str(r["key"]) for r in vault.database.execute("SELECT key FROM entities")}
    for secret in SECRET_FILES:
        assert f"file:vault:{secret}" not in keys, secret


def test_no_canary_is_embedded(vault: Workspace) -> None:
    """Vectors are keyed by chunk content hash, so a chunk that never existed has
    none. Asserted at the source text, because an embedding is not reversible."""
    rows = vault.database.execute(
        "SELECT c.text FROM chunks c JOIN embeddings e ON e.content_hash = c.content_hash"
    ).fetchall()
    assert leaked_canaries("\n".join(str(r["text"]) for r in rows)) == []


def test_the_embedding_backend_never_receives_a_canary(tmp_path: Path, vault_root: Path) -> None:
    """The strongest form: record every string handed to the model.

    Absence of a chunk implies absence of an embedding, but only if nothing else
    embeds text by another route. This checks the backend's own input.
    """
    from repoatlas.embeddings.hashing import HashingEmbedding

    seen: list[str] = []
    real_embed = HashingEmbedding.embed

    def recording(self: HashingEmbedding, texts: list[str]):
        seen.extend(texts)
        return real_embed(self, texts)

    HashingEmbedding.embed = recording  # type: ignore[method-assign]
    try:
        space = _indexed(tmp_path, "embcap", vault_root)
        space.close()
    finally:
        HashingEmbedding.embed = real_embed  # type: ignore[method-assign]

    assert seen, "no text was embedded at all; the assertion below would be vacuous"
    assert leaked_canaries("\n".join(seen)) == []


def test_no_secret_reaches_an_evidence_set(vault: Workspace, vault_root: Path) -> None:
    context = vault.resolver().resolve(vault_root, command="locate")
    questions = [
        "Where is REPOATLAS_SECRET_CANARY_ENV_UPPER_001?",
        "Where is REPOATLAS_SECRET_CANARY_RSA_UPPER_002?",
        "server.PEM",
        "private.KEY",
        "Where are the credentials?",
    ]
    for question in questions:
        evidence = vault.retriever().retrieve(question, context)
        assert leaked_canaries("\n".join(c.text for c in evidence.candidates)) == [], question
        assert not [c for c in evidence.candidates if c.path in SECRET_FILES], question


# == 4. mandatory policy beats user preference ===============================


def test_a_repoatlasignore_negation_cannot_re_admit_a_secret(tmp_path: Path) -> None:
    """§17 is policy, not preference.

    A user who writes `!.ENV` is asking for something RepoAtlas does not offer;
    the alternative is a configuration file that can silently disable a security
    control.
    """
    root = tmp_path / "neg-root"
    repo = root / "vault"
    repo.mkdir(parents=True)
    write_fixture(repo)
    (repo / ".repoatlasignore").write_text(
        "\n".join(["*", "!.ENV", "!ID_RSA", "!server.PEM", "!private.KEY", "!app.py"]) + "\n",
        encoding="utf-8",
    )
    init_git_repository(repo)

    space = _indexed(tmp_path, "neg", root)
    try:
        indexed = indexed_paths(space)
        assert indexed.isdisjoint(SECRET_FILES)
        assert leaked_canaries(chunk_text(space)) == []
        # The negation still works for a file the security policy does not claim,
        # so this proves precedence rather than a broken ignore implementation.
        assert "app.py" in indexed
    finally:
        space.close()


def test_a_user_ignore_rule_keeps_its_case_sensitive_semantics(tmp_path: Path) -> None:
    """Only the built-in policy became case-insensitive.

    `.repoatlasignore` documents gitignore-like semantics, and gitignore is
    case-sensitive on a case-sensitive filesystem. Quietly changing that for
    every user pattern would drop files nobody asked to drop.
    """
    from repoatlas.discovery.ignore import IgnoreRules

    rules = IgnoreRules.from_text("notes.txt\n*.LOG\n")
    assert rules.matches("notes.txt")
    assert not rules.matches("NOTES.TXT")
    assert rules.matches("server.LOG")
    assert not rules.matches("server.log")


def test_a_gitignored_secret_stays_ignored(tmp_path: Path) -> None:
    root = tmp_path / "gi-root"
    repo = root / "vault"
    repo.mkdir(parents=True)
    write_fixture(repo)
    (repo / ".gitignore").write_text(".ENV\nserver.PEM\n", encoding="utf-8")
    init_git_repository(repo)
    space = _indexed(tmp_path, "gi", root)
    try:
        assert indexed_paths(space).isdisjoint(SECRET_FILES)
    finally:
        space.close()


# == 5. an already-contaminated database =====================================


def _case_sensitive_is_secret_path(rel_path: str) -> bool:
    """The pre-fix implementation, verbatim, so the contaminated state is real."""
    name = PurePosixPath(rel_path).name
    return any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_PATTERNS)


@pytest.fixture
def contaminated(
    tmp_path: Path, vault_root: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Workspace]:
    """A database indexed by the *vulnerable* build, then handed to the fixed one.

    Fixing enumeration for new files is not enough: an upgraded RepoAtlas whose
    `files`, `chunks`, FTS and embedding tables still hold `.ENV` has not fixed
    anything for the user who was already running it.
    """
    monkeypatch.setattr("repoatlas.sync.scanner.is_secret_path", _case_sensitive_is_secret_path)
    space = _indexed(tmp_path, "contaminated", vault_root)
    monkeypatch.undo()
    yield space
    space.close()


def test_the_contaminated_fixture_really_is_contaminated(contaminated: Workspace) -> None:
    """Guards every test below: if this stops failing the way it should, the
    upgrade tests would be proving nothing."""
    leaked = indexed_paths(contaminated) & set(SECRET_FILES)
    assert len(leaked) >= 15, leaked
    assert leaked_canaries(chunk_text(contaminated))


def test_a_normal_sync_removes_an_already_indexed_secret(
    contaminated: Workspace, vault_root: Path
) -> None:
    """No manual database deletion, no `--full`, no flag to discover."""
    sync(contaminated, contaminated.resolver().resolve(vault_root))
    assert indexed_paths(contaminated).isdisjoint(SECRET_FILES)


@pytest.mark.parametrize(
    ("table", "query"),
    [
        (
            "chunks",
            "SELECT COUNT(*) n FROM chunks c JOIN files f ON f.id = c.file_id WHERE f.rel_path = ?",
        ),
        (
            "entities",
            "SELECT COUNT(*) n FROM entities e JOIN files f ON f.id = e.file_id "
            "WHERE f.rel_path = ?",
        ),
        (
            "edges",
            "SELECT COUNT(*) n FROM edges g JOIN files f ON f.id = g.file_id WHERE f.rel_path = ?",
        ),
        (
            "unresolved_refs",
            "SELECT COUNT(*) n FROM unresolved_refs u "
            "JOIN files f ON f.id = u.file_id WHERE f.rel_path = ?",
        ),
    ],
)
def test_every_dependent_table_is_cleared_by_the_upgrade_sync(
    contaminated: Workspace, vault_root: Path, table: str, query: str
) -> None:
    sync(contaminated, contaminated.resolver().resolve(vault_root))
    for secret in SECRET_FILES:
        n = int(contaminated.database.execute(query, (secret,)).fetchone()["n"])
        assert n == 0, (table, secret)


def test_the_upgrade_sync_clears_chunks_fts_and_vectors(
    contaminated: Workspace, vault_root: Path
) -> None:
    sync(contaminated, contaminated.resolver().resolve(vault_root))
    assert leaked_canaries(chunk_text(contaminated)) == []
    for canary in CANARIES:
        assert contaminated.search.search_literal(canary) == [], canary
    rows = contaminated.database.execute(
        "SELECT c.text FROM chunks c JOIN embeddings e ON e.content_hash = c.content_hash"
    ).fetchall()
    assert leaked_canaries("\n".join(str(r["text"]) for r in rows)) == []


def test_the_upgrade_sync_leaves_no_orphan_vector_for_a_secret(
    contaminated: Workspace, vault_root: Path
) -> None:
    """Vectors are content-addressed, so they outlive the chunk that produced
    them until they are collected. A vector is not the text, but a vector nobody
    can account for is state the user did not consent to keep."""
    before = int(contaminated.database.execute("SELECT COUNT(*) n FROM embeddings").fetchone()["n"])
    sync(contaminated, contaminated.resolver().resolve(vault_root))
    after = int(contaminated.database.execute("SELECT COUNT(*) n FROM embeddings").fetchone()["n"])
    orphans = int(
        contaminated.database.execute(
            "SELECT COUNT(*) n FROM embeddings v WHERE NOT EXISTS "
            "(SELECT 1 FROM chunks c WHERE c.content_hash = v.content_hash)"
        ).fetchone()["n"]
    )
    assert orphans == 0
    assert after < before


def test_no_secret_survives_the_upgrade_into_retrieval(
    contaminated: Workspace, vault_root: Path
) -> None:
    sync(contaminated, contaminated.resolver().resolve(vault_root))
    context = contaminated.resolver().resolve(vault_root, command="locate")
    for canary in CANARIES:
        evidence = contaminated.retriever().retrieve(f"Where is {canary}?", context)
        assert not [c for c in evidence.candidates if c.path in SECRET_FILES], canary


# == 6. renames, branches and case-only changes ==============================


def _commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", message)


def _sync_all(space: Workspace, root: Path) -> None:
    sync(space, space.resolver().resolve(root))


def test_a_safe_file_renamed_to_a_secret_name_loses_its_index_entry(
    tmp_path: Path, vault_root: Path, vault: Workspace
) -> None:
    repo = vault_root / "vault"
    assert "environment.txt" in indexed_paths(vault)
    (repo / "environment.txt").rename(repo / ".ENV.RENAMED")
    _commit(repo, "rename to a secret name")
    _sync_all(vault, vault_root)

    indexed = indexed_paths(vault)
    assert "environment.txt" not in indexed
    assert ".ENV.RENAMED" not in indexed
    assert "REPOATLAS_SAFE_MARKER_ENVIRONMENT" not in chunk_text(vault)


def test_a_secret_file_renamed_to_a_safe_name_becomes_indexable(
    tmp_path: Path, vault_root: Path, vault: Workspace
) -> None:
    """The policy is about names, so it follows the name."""
    repo = vault_root / "vault"
    assert "server.PEM" not in indexed_paths(vault)
    (repo / "server.PEM").rename(repo / "server_pem_notes.md")
    _commit(repo, "rename to a safe name")
    _sync_all(vault, vault_root)

    assert "server_pem_notes.md" in indexed_paths(vault)
    assert "REPOATLAS_SECRET_CANARY_PEM_UPPER_003" in chunk_text(vault)


def test_a_case_only_rename_stays_excluded(
    tmp_path: Path, vault_root: Path, vault: Workspace
) -> None:
    """On Linux `server.pem` and `server.PEM` are two files; both are excluded,
    and neither is excluded merely because the filesystem folded the name."""
    repo = vault_root / "vault"
    (repo / "server.pem").rename(repo / "SERVER.Pem")
    _commit(repo, "case-only rename")
    _sync_all(vault, vault_root)

    indexed = indexed_paths(vault)
    assert "SERVER.Pem" not in indexed
    assert "server.pem" not in indexed
    assert leaked_canaries(chunk_text(vault)) == []


def test_a_branch_that_introduces_a_secret_leaves_nothing_behind(tmp_path: Path) -> None:
    root = tmp_path / "branch-root"
    repo = root / "svc"
    repo.mkdir(parents=True)
    (repo / "normal.py").write_text("SAFE_ON_BRANCH_A = 1\n", encoding="utf-8")
    init_git_repository(repo)

    space = _indexed(tmp_path, "branch", root)
    try:
        assert "normal.py" in indexed_paths(space)

        git(repo, "checkout", "--quiet", "-b", "secrets")
        (repo / "PRIVATE.KEY").write_text("REPOATLAS_SECRET_CANARY_BRANCH_006\n", encoding="utf-8")
        _commit(repo, "add a key on branch B")
        _sync_all(space, root)

        indexed = indexed_paths(space)
        assert "PRIVATE.KEY" not in indexed
        assert "REPOATLAS_SECRET_CANARY_BRANCH_006" not in chunk_text(space)

        git(repo, "checkout", "--quiet", "main")
        _sync_all(space, root)
        assert indexed_paths(space) == {"normal.py"}
    finally:
        space.close()


# == 7. incremental equals a clean index =====================================


def _clean_snapshot(tmp_path: Path, tag: str, root: Path) -> dict[str, object]:
    space = _indexed(tmp_path, f"clean-{tag}", root)
    try:
        return fact_snapshot(space)
    finally:
        space.close()


def test_an_upgraded_database_equals_a_clean_index(
    tmp_path: Path, vault_root: Path, contaminated: Workspace
) -> None:
    """The upgrade must not leave a differently-shaped index behind."""
    sync(contaminated, contaminated.resolver().resolve(vault_root))
    assert fact_snapshot(contaminated) == _clean_snapshot(tmp_path, "upgraded", vault_root)


@pytest.mark.parametrize(
    ("tag", "action"),
    [
        ("safe-to-secret", "rename-safe"),
        ("secret-to-safe", "rename-secret"),
        ("case-only", "rename-case"),
        ("ignore-change", "ignore"),
    ],
)
def test_each_transition_equals_a_clean_index(
    tmp_path: Path, vault_root: Path, vault: Workspace, tag: str, action: str
) -> None:
    repo = vault_root / "vault"
    if action == "rename-safe":
        (repo / "keyboard.txt").rename(repo / "keyboard.KEY")
    elif action == "rename-secret":
        (repo / "private.KEY").rename(repo / "private_key_notes.md")
    elif action == "rename-case":
        (repo / "id_rsa").rename(repo / "ID_Rsa_2")
    else:
        (repo / ".repoatlasignore").write_text("*.md\n", encoding="utf-8")
    _commit(repo, f"transition {tag}")
    _sync_all(vault, vault_root)
    assert fact_snapshot(vault) == _clean_snapshot(tmp_path, tag, vault_root)


# == 8. symlinks and error paths =============================================


def test_a_safe_named_symlink_to_a_secret_does_not_leak(tmp_path: Path) -> None:
    """Symlinks are skipped outright, and this pass does not change that.

    Following one to inspect its target would be exactly the read the boundary
    exists to prevent — and the target may be outside the repository entirely.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "real.pem").write_text("REPOATLAS_SECRET_CANARY_SYMLINK_007\n", encoding="utf-8")

    root = tmp_path / "sym-root"
    repo = root / "svc"
    repo.mkdir(parents=True)
    (repo / "app.py").write_text("SAFE = 1\n", encoding="utf-8")
    (repo / "config.txt").symlink_to(outside / "real.pem")
    (repo / "cert.PEM").symlink_to(outside / "real.pem")
    init_git_repository(repo)

    space = _indexed(tmp_path, "sym", root)
    try:
        indexed = indexed_paths(space)
        assert "config.txt" not in indexed
        assert "cert.PEM" not in indexed
        assert "REPOATLAS_SECRET_CANARY_SYMLINK_007" not in chunk_text(space)
    finally:
        space.close()


def test_an_unreadable_secret_is_skipped_by_name_not_by_error(tmp_path: Path) -> None:
    """Filtering must not depend on a read succeeding or failing.

    A read that fails still produces a diagnostic naming the path; a file that is
    never read produces nothing at all.
    """
    root = tmp_path / "unreadable-root"
    repo = root / "svc"
    repo.mkdir(parents=True)
    (repo / "app.py").write_text("SAFE = 1\n", encoding="utf-8")
    secret = repo / "server.PEM"
    secret.write_text("REPOATLAS_SECRET_CANARY_UNREADABLE_008\n", encoding="utf-8")
    init_git_repository(repo)
    secret.chmod(0o000)

    try:
        space = _indexed(tmp_path, "unreadable", root)
    finally:
        secret.chmod(0o644)
    try:
        assert "server.PEM" not in indexed_paths(space)
        rows = space.database.execute("SELECT rel_path FROM files").fetchall()
        assert {str(r["rel_path"]) for r in rows} == {"app.py"}
    finally:
        space.close()


def test_an_oversized_secret_is_never_read(tmp_path: Path) -> None:
    root = tmp_path / "big-root"
    repo = root / "svc"
    repo.mkdir(parents=True)
    (repo / "app.py").write_text("SAFE = 1\n", encoding="utf-8")
    (repo / "huge.KEY").write_text(
        "REPOATLAS_SECRET_CANARY_OVERSIZED_009\n" + ("x" * 2_000_000), encoding="utf-8"
    )
    init_git_repository(repo)

    space = _indexed(tmp_path, "big", root)
    try:
        assert "huge.KEY" not in indexed_paths(space)
        assert "REPOATLAS_SECRET_CANARY_OVERSIZED_009" not in chunk_text(space)
    finally:
        space.close()


# == 9. a repository of nothing but secrets ==================================


def test_a_repository_containing_only_secrets_stays_managed(tmp_path: Path) -> None:
    """Discovery is about repositories; exclusion is about files. A repository
    that disappears from `repos` would be a confusing way to report that all of
    its files were excluded."""
    root = tmp_path / "only-root"
    repo = root / "keystore"
    repo.mkdir(parents=True)
    for name in (".env", "ID_RSA", "server.PEM"):
        (repo / name).write_text("REPOATLAS_SECRET_CANARY_ONLY_010\n", encoding="utf-8")
    init_git_repository(repo)

    space = _indexed(tmp_path, "only", root)
    try:
        names = {r.name for r in space.repositories.list_all()}
        assert "keystore" in names
        assert indexed_paths(space) == set()
        assert "REPOATLAS_SECRET_CANARY_ONLY_010" not in chunk_text(space)
    finally:
        space.close()


# == 10. the prompt and operator-visible output ==============================


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("REPOATLAS_CONFIG", str(tmp_path / "cli-config.yaml"))
    monkeypatch.setenv("REPOATLAS_DB", str(tmp_path / "cli-state" / "atlas.db"))
    yield tmp_path / "cli-config.yaml"


def _run_in(directory: Path, *args: str):
    previous = Path.cwd()
    os.chdir(directory)
    try:
        return runner.invoke(app, list(args))
    finally:
        os.chdir(previous)


def test_no_canary_reaches_the_model_prompt(
    cli_env: Path, vault_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inspect every message actually handed to the backend, not the rendering.

    The questions name the excluded files directly, which is as hard as a user
    can push retrieval toward them. A canary appearing in the prompt would mean
    the file's *contents* were retrieved; the question text itself is the user's
    own words and is excluded from the check, since echoing the question back is
    what a prompt is for.
    """
    backend = ScriptedBackend(
        [
            json.dumps(
                {"summary": "No such value is indexed.", "hypotheses": [], "unknowns": ["value"]}
            )
        ]
        * 6
    )
    monkeypatch.setattr("repoatlas.services.query_service.build_model", lambda settings: backend)

    assert _run_in(vault_root, "init", str(vault_root)).exit_code == 0
    questions = [
        "What is in server.PEM?",
        "What credentials does ID_RSA hold?",
        "What is the value in the .ENV file?",
    ]
    for question in questions:
        result = _run_in(vault_root / "vault", "ask", question)
        assert result.exit_code == 0, result.output

    prompt = "\n".join(m.content for call in backend.calls for m in call)
    assert leaked_canaries(prompt) == []
    for secret in SECRET_FILES:
        assert f"path={secret}" not in prompt, secret


def test_naming_a_canary_in_the_question_still_retrieves_nothing_from_it(
    cli_env: Path, vault_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one case where the canary legitimately appears — and must appear only
    there. Quoting the user's question back is not a leak; carrying the file that
    contains it into the evidence block is."""
    backend = ScriptedBackend(
        [json.dumps({"summary": "Not indexed.", "hypotheses": [], "unknowns": ["value"]})]
    )
    monkeypatch.setattr("repoatlas.services.query_service.build_model", lambda settings: backend)

    assert _run_in(vault_root, "init", str(vault_root)).exit_code == 0
    canary = "REPOATLAS_SECRET_CANARY_ENV_UPPER_001"
    assert _run_in(vault_root / "vault", "ask", f"Where is {canary}?").exit_code == 0

    prompt = "\n".join(m.content for call in backend.calls for m in call)
    # Once, as the question. Never as retrieved evidence.
    assert prompt.count(canary) == 1
    assert f"Where is {canary}?" in prompt
    assert ".ENV" not in prompt


def test_no_canary_appears_in_operator_visible_output(cli_env: Path, vault_root: Path) -> None:
    """Skipping is silent by design: a log line naming every excluded file would
    put a list of the repository's secret paths into terminal scrollback and CI
    output for no operational benefit. Nothing is printed, so nothing leaks.

    Queries here name the *files*, not the canaries, so any canary in the output
    came from the index rather than from the command line.
    """
    assert _run_in(vault_root, "init", str(vault_root)).exit_code == 0
    output = "".join(
        _run_in(vault_root / "vault", *args).output
        for args in (
            ("sync", "--verbose"),
            ("sync", "--full", "--verbose"),
            ("status", "--verbose"),
            ("repos",),
            ("locate", "server.PEM", "--verbose"),
            ("locate", "ID_RSA", "--verbose"),
            ("locate", "credentials", "--verbose", "--json"),
            ("ask", "--no-llm", "What is in the .ENV file?"),
        )
    )
    assert leaked_canaries(output) == []
