"""Unicode exact retrieval (V1_SPEC §14, ARCHITECTURE.md §10).

An indexed fact must not be reported absent because of the characters in it.

The defect had two independent causes, and either alone was enough to make a
Unicode query return nothing:

* **query analysis** extracted no term at all. Every identifier pattern in
  `retrieval/query.py` was written with explicit `A-Za-z` ranges, so
  `créer_utilisateur` matched none of them — `\\b` finds no boundary between `é`
  and `e`, so not even a suffix was recovered.
* **the FTS query builder** used `[A-Za-z0-9_.@$/-]+` while the index was built
  with `unicode61 remove_diacritics 2`. It did not merely drop non-ASCII text, it
  **shredded** it: `créer_utilisateur` became the two terms `cr` and
  `er_utilisateur`, neither of which is in the source.

The index itself was never at fault. A direct `MATCH '"créer_utilisateur"'` on the
same database returned the row, which is why this pass changes no schema.

The second subject of this suite is the boundary the fix could have destroyed.
`unicode61 remove_diacritics 2` folds diacritics *and* case, so FTS genuinely
matches `creer_utilisateur` against `créer_utilisateur`. That is legitimate
lexical recall and must never be promoted into byte-exact deterministic evidence.
Every positive case here is therefore paired with a near miss.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.models.evidence import EvidenceTier
from repoatlas.retrieval.query import analyze
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import Workspace, open_workspace
from repoatlas.storage.sqlite.search import tokenize_query
from tests.conftest import git

pytestmark = pytest.mark.adversarial


SOURCE = """\
def créer_utilisateur():
    return True


class Ünïcode:
    pass


def Δοκιμή():
    pass


def обработать():
    pass


JP_ERROR = "接続できませんでした"
ZH_ERROR = "无法连接到服务器"
KO_ERROR = "서버에 연결할 수 없습니다"
AR_ERROR = "تعذر الاتصال بالخادم"
HE_ERROR = "לא ניתן להתחבר לשרת"
RU_ERROR = "Ошибка подключения к серверу"
FR_ERROR = "[ÉCHEC] connexion refusée: hôte=éxemple"
JP_PUNCT = "エラー: 接続できませんでした。"
PHRASE = "naïve café"
MIXED_naïve_name = 1
CAFÉ_HÔTE = "config"


def create_user():
    pass


ASCII_ERROR = "could not connect to server"
SERVICE_HOST = "localhost"
"""

TS_SOURCE = """\
export function tráiter(): boolean {
  return true;
}

export const MENSAJE = "no se pudo conectar";
"""


@pytest.fixture
def unicode_root(tmp_path: Path) -> Path:
    root = tmp_path / "repos"
    repo = root / "uni"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text(SOURCE, encoding="utf-8")
    (repo / "src" / "svc.ts").write_text(TS_SOURCE, encoding="utf-8")
    # Unicode filenames, which the review found already worked. Pinned so the
    # tokenizer change cannot regress them.
    (repo / "src" / "配置.py").write_text("SETTING = 1\n", encoding="utf-8")
    (repo / "src" / "résumé.py").write_text("def résumé_de(): pass\n", encoding="utf-8")
    (repo / "データ.yaml").write_text("clé: valeur\n", encoding="utf-8")
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "initial")
    return root


@pytest.fixture
def indexed(tmp_path: Path, unicode_root: Path) -> Iterator[Workspace]:
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    initialize(workspace, unicode_root, config_path=tmp_path / "config.yaml")
    yield workspace
    workspace.close()


def retrieve(workspace: Workspace, question: str, root: Path):
    context = workspace.resolver().resolve(root / "uni")
    return workspace.retriever().retrieve(question, context)


def tiers(workspace: Workspace, question: str, root: Path) -> set[EvidenceTier]:
    return {c.tier for c in retrieve(workspace, question, root).candidates}


def deterministic(workspace: Workspace, question: str, root: Path) -> list[str]:
    """Paths of candidates the system is willing to call byte-exact facts."""
    return [
        f"{c.path}:{c.line_start}"
        for c in retrieve(workspace, question, root).candidates
        if c.tier is EvidenceTier.DETERMINISTIC
    ]


# == 1. the three fresh-review failures ======================================


@pytest.mark.parametrize(
    "question",
    ["créer_utilisateur", "Ünïcode", "接続できませんでした"],
)
def test_the_three_reproduced_queries_return_deterministic_evidence(
    indexed: Workspace, unicode_root: Path, question: str
) -> None:
    assert deterministic(indexed, question, unicode_root), question


@pytest.mark.parametrize("question", ["create_user", "could not connect to server"])
def test_the_ascii_controls_still_work(
    indexed: Workspace, unicode_root: Path, question: str
) -> None:
    assert retrieve(indexed, question, unicode_root).candidates, question


# == 2. scripts and identifier shapes ========================================


@pytest.mark.parametrize(
    "question",
    [
        "créer_utilisateur",  # accented Latin
        "Ünïcode",  # German umlaut
        "Δοκιμή",  # Greek
        "обработать",  # Cyrillic
        "tráiter",  # accented Latin, TypeScript
        "MIXED_naïve_name",  # mixed ASCII + Unicode identifier
        "CAFÉ_HÔTE",  # accented SCREAMING_CASE
    ],
)
def test_unicode_identifiers_are_extracted_and_found(
    indexed: Workspace, unicode_root: Path, question: str
) -> None:
    assert analyze(question).terms, f"query analysis extracted nothing from {question!r}"
    assert tokenize_query(question) == [question], "the FTS builder must not shred the term"
    assert retrieve(indexed, question, unicode_root).candidates, question


@pytest.mark.parametrize(
    "question",
    [
        "接続できませんでした",  # Japanese
        "无法连接到服务器",  # Chinese
        "서버에 연결할 수 없습니다",  # Korean
        "تعذر الاتصال بالخادم",  # Arabic
        "לא ניתן להתחבר לשרת",  # Hebrew
        "Ошибка подключения к серверу",  # Russian
    ],
)
def test_pasted_error_strings_are_retrievable(
    indexed: Workspace, unicode_root: Path, question: str
) -> None:
    """§12: an error message is not an identifier and must not have to look like one."""
    assert analyze(question).terms, question
    assert retrieve(indexed, question, unicode_root).candidates, question


# == 3. the exact / lexical boundary =========================================


def test_a_diacritic_folded_match_is_never_deterministic(
    indexed: Workspace, unicode_root: Path
) -> None:
    """The trust property.

    `unicode61 remove_diacritics 2` really does match `Unicode` against `Ünïcode`
    — verified directly against SQLite. That is honest lexical recall. Promoting
    it to the deterministic tier would tell the user that the source contains a
    string it does not contain.
    """
    found = retrieve(indexed, "Unicode", unicode_root)
    assert found.candidates, "the lexical match should still be offered"
    assert all(c.tier is not EvidenceTier.DETERMINISTIC for c in found.candidates), (
        "a diacritic-folded hit was promoted to byte-exact evidence"
    )
    # ...while the correctly spelled query is deterministic.
    assert deterministic(indexed, "Ünïcode", unicode_root)


def test_a_case_folded_match_is_never_deterministic(indexed: Workspace, unicode_root: Path) -> None:
    found = retrieve(indexed, "ÜNÏCODE", unicode_root)
    assert all(c.tier is not EvidenceTier.DETERMINISTIC for c in found.candidates)


@pytest.mark.parametrize(
    "question",
    [
        "créer_utilisateurs",  # one letter longer
        "autre_utilisateur",  # different word
        "接続できました",  # different Japanese string
        "无法连接到客户端",  # different Chinese string
        "Δοκιμάζω",  # different Greek word
        "необработать",  # different Cyrillic word
    ],
)
def test_near_misses_produce_no_deterministic_evidence(
    indexed: Workspace, unicode_root: Path, question: str
) -> None:
    assert deterministic(indexed, question, unicode_root) == [], question


@pytest.mark.parametrize(
    "question",
    ["ασύνδετος", "存在しない文字列", "غير_موجود", "неизвестный_символ"],
)
def test_absent_unicode_literals_are_reported_absent(
    indexed: Workspace, unicode_root: Path, question: str
) -> None:
    assert deterministic(indexed, question, unicode_root) == [], question


# == 4. normalization policy =================================================


def test_the_decomposed_form_is_lexical_not_deterministic(
    indexed: Workspace, unicode_root: Path
) -> None:
    """§6: the policy, stated as a test.

    Source text is stored exactly as written and never normalized — provenance
    and symbol identity depend on that. SQLite's tokenizer *does* treat NFC and
    NFD as the same token, so a decomposed query still finds the row lexically.
    It is not byte-identical to the stored text, so it is not deterministic
    evidence, and the two tiers say so separately.
    """
    composed = "Ünïcode"
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed

    assert deterministic(indexed, composed, unicode_root)
    found = retrieve(indexed, decomposed, unicode_root)
    assert all(c.tier is not EvidenceTier.DETERMINISTIC for c in found.candidates), (
        "a canonically-equivalent but differently-spelled query is not byte-exact"
    )


def test_stored_text_is_never_normalized(indexed: Workspace, unicode_root: Path) -> None:
    """Provenance means the stored bytes are the file's bytes."""
    row = indexed.database.execute(
        "SELECT name FROM entities WHERE name LIKE '%nicode%' OR name LIKE '%Ünïcode%'"
    ).fetchone()
    assert row is not None
    assert row["name"] == "Ünïcode"
    assert unicodedata.is_normalized("NFC", row["name"]) or row["name"] == "Ünïcode"


# == 5. punctuation, quoting and phrases =====================================


@pytest.mark.parametrize(
    "question",
    [
        "エラー: 接続できませんでした。",
        "[ÉCHEC] connexion refusée: hôte=éxemple",
        "(接続できませんでした)",
        "接続できませんでした、",
        "«接続できませんでした»",
    ],
)
def test_punctuation_around_a_unicode_literal_does_not_destroy_it(
    indexed: Workspace, unicode_root: Path, question: str
) -> None:
    """§15: real pasted errors arrive wrapped in punctuation, ASCII and not."""
    assert retrieve(indexed, question, unicode_root).candidates, question


@pytest.mark.parametrize("quote", ['"', "'", "`"])
def test_quoting_a_unicode_phrase_behaves_like_quoting_an_ascii_one(
    indexed: Workspace, unicode_root: Path, quote: str
) -> None:
    """§14: quoting must not be an undocumented workaround for Unicode."""
    quoted = f"{quote}naïve café{quote}"
    assert deterministic(indexed, quoted, unicode_root), quoted
    # and the unquoted form is not worse
    assert retrieve(indexed, "naïve café", unicode_root).candidates


# == 6. filenames and paths ==================================================


@pytest.mark.parametrize(
    "question",
    ["配置.py", "résumé.py", "データ.yaml", "src/配置.py", "src/résumé.py"],
)
def test_unicode_filenames_and_paths_stay_findable(
    indexed: Workspace, unicode_root: Path, question: str
) -> None:
    """§23: these already worked. The tokenizer change must not regress them."""
    assert deterministic(indexed, question, unicode_root), question


def test_a_unicode_path_and_a_unicode_symbol_together(
    indexed: Workspace, unicode_root: Path
) -> None:
    found = retrieve(indexed, "résumé_de in src/résumé.py", unicode_root)
    paths = {c.path for c in found.candidates}
    assert any("résumé" in p for p in paths), paths


# == 7. ASCII behaviour is unchanged =========================================


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("SERVICE_HOST", "env_vars"),
        ("create_user", "code_names"),
        ("src/config.py", "paths"),
        ("@company/common", "packages"),
        ("module.attr", "dotted"),
        ("left-pad", "ambiguous_packages"),
        ("camelCaseName", "ambiguous_packages"),
    ],
)
def test_established_ascii_classification_is_untouched(question: str, expected: str) -> None:
    """§16: this is an extension, not a rewrite. Each ASCII shape keeps its
    category, because the categories carry the precision of exact search."""
    analysis = analyze(question)
    assert question in getattr(analysis, expected), (question, expected)


@pytest.mark.parametrize(
    "text",
    [
        "SERVICE_HOST",
        "create_user",
        "src/config.py",
        "@company/common",
        "module.attr",
        "left-pad",
        "a-b_c.d@e$f/g",
        "100%",
        "under_score",
    ],
)
def test_the_fts_tokenizer_is_a_strict_superset_for_ascii(text: str) -> None:
    """The old class was `[A-Za-z0-9_.@$/-]+`; the new one is `[\\w.@$/-]+`.
    For ASCII input the two must agree exactly."""
    import re

    old = [t for t in re.findall(r"[A-Za-z0-9_.@$/-]+", text) if len(t) > 1]
    assert tokenize_query(text) == sorted(dict.fromkeys(old), key=len, reverse=True)


# == 8. FTS syntax safety ====================================================


@pytest.mark.parametrize(
    "question",
    [
        'café "quoted" text',
        "café AND naïve",
        "接続 NEAR できません",
        "café* naïve",
        "café:naïve",
        "(café) OR (naïve)",
        "café'naïve",
        'Ünïcode"',
        "café -naïve",
        "*",
        '"',
    ],
)
def test_unicode_with_fts_syntax_characters_never_raises(
    indexed: Workspace, unicode_root: Path, question: str
) -> None:
    """§19: user text must be serialized, never concatenated into MATCH."""
    retrieve(indexed, question, unicode_root)  # must not raise


def test_a_quote_inside_a_term_is_escaped_not_injected() -> None:
    from repoatlas.storage.sqlite.search import _fts_query

    expression = _fts_query(['café"OR"naïve'])
    assert expression.startswith('"') and expression.endswith('"')
    assert expression.count('"') % 2 == 0


# == 9. long literals and budget =============================================


def test_a_long_pasted_unicode_error_is_handled(indexed: Workspace, unicode_root: Path) -> None:
    """§22: a pasted stack trace must not explode into unusable fragments."""
    long_question = (
        "Traceback (most recent call last):\n"
        '  File "src/app.py", line 7, in <module>\n'
        "    接続できませんでした\n"
        "RuntimeError: 接続できませんでした — Ошибка подключения к серверу\n"
    ) * 3
    found = retrieve(indexed, long_question, unicode_root)
    assert found.candidates
    assert any(c.tier is EvidenceTier.DETERMINISTIC for c in found.candidates)


# == 10. storage round-trip ==================================================


@pytest.mark.parametrize(
    "text",
    [
        "café",  # BMP + combining-capable
        unicodedata.normalize("NFD", "café"),  # decomposed
        "接続できませんでした",  # CJK
        "𝔘𝔫𝔦𝔠𝔬𝔡𝔢",  # supplementary plane
        "naïve 🚀 café",  # emoji in a string
    ],
)
def test_sqlite_round_trips_every_form_without_corruption(tmp_path: Path, text: str) -> None:
    """§31: prove the persistence layer is not a second source of corruption."""
    connection = sqlite3.connect(tmp_path / "rt.db")
    connection.execute("CREATE TABLE t (x TEXT)")
    connection.execute("INSERT INTO t VALUES (?)", (text,))
    connection.commit()
    back = connection.execute("SELECT x FROM t").fetchone()[0]
    connection.close()
    assert back == text
    assert len(back) == len(text)


def test_a_surrogate_bearing_query_does_not_crash(indexed: Workspace, unicode_root: Path) -> None:
    """§32: undecodable filesystem bytes reach queries as surrogates. Outside the
    supported model, so the requirement is only that it fails conservatively."""
    retrieve(indexed, "caf\udcff", unicode_root)  # must not raise


# == 11. chunk seams =========================================================


@pytest.mark.parametrize(
    ("marker", "label"),
    [
        ("SÉPARATEUR_ACCENTUÉ_ÜBER", "accented Latin"),
        ("接続できませんでしたSEAM", "CJK"),
        (unicodedata.normalize("NFD", "SÉPARATEUR_DÉCOMPOSÉ"), "combining marks"),
    ],
)
def test_a_unicode_literal_straddling_a_chunk_seam_is_found(
    tmp_path: Path, marker: str, label: str
) -> None:
    """The previous remediation made a literal survive the character cap. That
    logic indexes by code point on the Python side and by character in SQLite's
    `substr`/`length`; a byte-oriented assumption anywhere would break here, where
    one character is two to four bytes.
    """
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    cap = settings.indexing.max_chunk_chars
    root = tmp_path / "repos"
    repo = root / "seam"
    repo.mkdir(parents=True)
    # One physical line longer than the cap, with the marker astride the cut.
    filler = "é" * (cap - len(marker) // 2)
    (repo / "big.py").write_text(f'BLOB = "{filler}{marker}{filler}"\n', encoding="utf-8")
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "initial")

    workspace = open_workspace(settings=settings, require_root=False)
    try:
        initialize(workspace, root, config_path=tmp_path / "config.yaml")
        pieces = workspace.database.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE split_total > 1"
        ).fetchone()["n"]
        assert pieces > 1, f"{label}: the fixture did not actually straddle a seam"

        context = workspace.resolver().resolve(root / "seam")
        found = workspace.retriever().retrieve(marker, context)
        assert any(c.tier is EvidenceTier.DETERMINISTIC for c in found.candidates), label
    finally:
        workspace.close()
