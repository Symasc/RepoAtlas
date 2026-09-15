"""OFF-01: Ruby is recognized and text-indexed, and never parsed structurally.

The only Ruby grammar RepoAtlas could ship without downloading anything at index
time is ``tree-sitter-ruby`` 0.23.1, whose external scanner predates upstream fix
``ad907a69`` ("scanner: fix heredoc serialization buffer overflows") and can write
one attacker-chosen byte past tree-sitter's serialization buffer. A guard on the
raw text is not sound --- in error recovery that scanner's literal stack grows on
every quote, slash or percent byte, so short heredoc words reach the overflow as
readily as long ones --- so Ruby structural parsing is gated entirely.

What these tests hold RepoAtlas to:

* the gate is **content-independent**: ordinary Ruby, every heredoc opener form
  the vulnerable scanner accepts at every size around its limits, multibyte
  words, CRLF and LF all get the same fixed answer;
* the gate is **first**: a tripwire on the grammar loader and on the imports of
  any Ruby grammar or grammar downloader fires before native code could, so a
  bypassed gate fails here rather than feeding a payload to a scanner;
* the answer repeats **nothing** from the file, and a sync derives **no
  structural facts** from it while text search and the text-level dependency
  parsers carry on exactly as for any language without a structural parser.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.graph.linker import GLOBAL_PARSER
from repoatlas.models.parsing import (
    GRAMMAR_UNAVAILABLE,
    STRUCTURAL_PARSING_DISABLED,
    ParsedFile,
    ParseDiagnostic,
    Severity,
)
from repoatlas.parsers import ruby as ruby_module
from repoatlas.parsers import treesitter
from repoatlas.parsers.ruby import RubyParser
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import open_workspace

ROOT = Path(__file__).resolve().parent.parent

#: The upstream commit a Ruby grammar must contain before the gate may go.
UPSTREAM_FIX = "ad907a69da0c8a4f7a943a7fe012712208da6dee"

#: Modules whose import would mean Ruby bytes were on their way to native code.
FORBIDDEN_MODULES = ("tree_sitter_ruby", "tree_sitter_language_pack")

GATED = ParsedFile(
    language="Ruby",
    parser="tree-sitter-ruby",
    parser_version="3",
    diagnostics=(ParseDiagnostic(severity=Severity.WARNING, message=STRUCTURAL_PARSING_DISABLED),),
)


class _Tripwire(importlib.abc.MetaPathFinder):
    """Refuses, and records, any import of a Ruby grammar or grammar downloader."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def find_spec(
        self, fullname: str, path: object, target: object = None
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname.split(".")[0] in FORBIDDEN_MODULES:
            self.attempts.append(fullname)
            raise ImportError("OFF-01 tripwire: a Ruby grammar import was attempted")
        return None


@pytest.fixture
def tripwire(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every route from Ruby source to a grammar, armed to fail loudly and first.

    The loader, the tree builder (under both names it is reachable by) and the
    imports are all replaced, so a parser that reaches for any of them raises in
    Python before a byte is handed to native code. The returned list records what
    was attempted, which the tests require to stay empty.
    """
    reached: list[str] = []

    def refuse(*args: object, **_kwargs: object) -> object:
        reached.append(f"grammar reached: {args[:1]!r}")
        raise AssertionError("OFF-01 tripwire: Ruby source reached the grammar loader")

    monkeypatch.setattr(treesitter, "load_parser", refuse)
    monkeypatch.setattr(treesitter, "parse_tree", refuse)
    monkeypatch.setattr(ruby_module, "parse_tree", refuse)
    for name in FORBIDDEN_MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    finder = _Tripwire()
    sys.meta_path.insert(0, finder)
    try:
        yield reached
    finally:
        sys.meta_path.remove(finder)
        reached.extend(f"import attempted: {name}" for name in finder.attempts)


# ---------------------------------------------------------------------------
# The inputs: every shape the vulnerable scanner treats specially, and plain Ruby
# ---------------------------------------------------------------------------

#: Heredoc word sizes in encoded bytes: the uint8 truncation boundary and the
#: 1024-byte serialization-buffer neighbourhood.
WORD_BYTES = (254, 255, 256, 1019, 1020, 1021, 1024, 1025)

#: Every opener ``scan_heredoc_word`` accepts: bare, ``-`` and ``~`` indentation
#: forms, and the three quoted forms (which may hold any byte up to the quote).
OPENERS = ("<<{w}", "<<-{w}", "<<~{w}", "<<'{w}'", '<<"{w}"', "<<`{w}`")

#: Word bodies whose byte length differs from their character count.
ALPHABETS = {"ascii": "A", "latin1-2byte": "é", "cjk-3byte": "日", "emoji-4byte": "\U0001f600"}


def _word(alphabet: str, size: int) -> str:
    unit = ALPHABETS[alphabet]
    width = len(unit.encode("utf-8"))
    return unit * (size // width) + "B" * (size % width)


def _heredoc(opener: str, word: str, newline: str) -> str:
    lines = ["def payload", f"  x = {opener.format(w=word)}", "    body", f"{word}", "end", ""]
    return newline.join(lines)


def _cases() -> Iterator[object]:
    yield pytest.param(
        "runner.rb", "module M\n  def run\n    ENV['HOST']\n  end\nend\n", id="ordinary"
    )
    yield pytest.param("empty.rb", "", id="empty")
    for opener in OPENERS:
        for size in WORD_BYTES:
            for alphabet in ALPHABETS:
                for newline, label in (("\n", "lf"), ("\r\n", "crlf")):
                    word = _word(alphabet, size)
                    assert len(word.encode("utf-8")) == size
                    yield pytest.param(
                        "payload.rb",
                        _heredoc(opener, word, newline),
                        id=f"{opener[:3].strip('{')}-{size}b-{alphabet}-{label}",
                    )
    # The literal-stack route: error recovery, 203 literal openers, a short word.
    yield pytest.param(
        "stack.rb", "}}} (( " + '"' * 203 + " <<WXYZ\nbody\nWXYZ\n", id="literal-stack-short-word"
    )
    yield pytest.param("classic.rb", "def a\r  1\rend\r", id="cr-only")


@pytest.mark.parametrize(("name", "content"), list(_cases()))
def test_every_ruby_shape_is_declined_before_any_grammar(
    tripwire: list[str], name: str, content: str
) -> None:
    assert RubyParser().parse(Path(name), content) == GATED
    assert tripwire == []


@pytest.mark.parametrize(
    "name",
    ["Gemfile", "Rakefile", "Guardfile", "Capfile", "tasks.rake", "gem.gemspec", "config.ru"],
)
def test_every_file_the_ruby_parser_claims_is_gated(tripwire: list[str], name: str) -> None:
    parser = RubyParser()
    assert parser.supports(Path(name))
    assert parser.parse(Path(name), 'gem "rails", "~> 7.0"\n') == GATED
    assert tripwire == []


def test_the_gated_answer_repeats_nothing_from_the_file(tripwire: list[str]) -> None:
    sentinel = "OFF01_RUBY_SOURCE_SENTINEL_7731"
    parsed = RubyParser().parse(Path("secret.rb"), f"x = <<~{sentinel}\n{sentinel}\n{sentinel}\n")
    assert sentinel not in parsed.model_dump_json()
    assert parsed.symbols == () and parsed.references == ()
    assert tripwire == []


def test_the_tripwire_fires_before_native_code_when_the_gate_is_bypassed(
    tripwire: list[str],
) -> None:
    """The guarantee the other tests lean on: a bypass is caught, not executed.

    ``_structure`` is exactly what ``parse`` would run without the gate. Under the
    tripwire it must stop in Python --- no grammar is constructed and nothing is
    imported --- which is what makes "the tripwire stayed quiet" meaningful above.
    """
    payload = _heredoc("<<'{w}'", _word("ascii", 1019), "\n")
    with pytest.raises(AssertionError, match="OFF-01 tripwire"):
        RubyParser()._structure(Path("payload.rb"), payload)
    assert tripwire and tripwire[0].startswith("grammar reached")


def test_the_gate_diagnostic_is_its_own_category() -> None:
    """Policy is not a broken installation, and a caller must be able to tell."""
    assert STRUCTURAL_PARSING_DISABLED != GRAMMAR_UNAVAILABLE
    assert GATED.diagnostics[0].severity is Severity.WARNING


# ---------------------------------------------------------------------------
# Through a real sync: no structural facts, text search and dependencies intact
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_a_sync_derives_no_structural_facts_from_ruby(tripwire: list[str], tmp_path: Path) -> None:
    repo = tmp_path / "repos" / "tooling"
    (repo / "lib").mkdir(parents=True)
    (repo / "lib" / "runner.rb").write_text(
        "require_relative 'support'\nmodule Tooling\n  class Runner\n"
        "    def host\n      ENV['SERVICE_HOST']\n    end\n  end\nend\n",
        encoding="utf-8",
    )
    (repo / "lib" / "payload.rb").write_text(
        "}}} (( " + '"' * 203 + " <<WXYZ\nbody\nWXYZ\n" + _heredoc("<<~{w}", "A" * 1019, "\r\n"),
        encoding="utf-8",
    )
    (repo / "Gemfile").write_text(
        'source "https://rubygems.org"\ngem "rack", "~> 2.2"\n', encoding="utf-8"
    )
    (repo / "tool.py").write_text(
        "import os\n\n\ndef host():\n    return os.environ['SERVICE_HOST']\n"
    )
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "x")

    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        report, _ = initialize(workspace, tmp_path / "repos", config_path=tmp_path / "config.yaml")
        db = workspace.database
        ruby_paths = ("lib/runner.rb", "lib/payload.rb", "Gemfile")

        entity_types = {
            (row["path"], row["type"])
            for row in db.execute(
                "SELECT f.rel_path AS path, e.type AS type "
                "FROM entities e JOIN files f ON f.id = e.file_id"
            )
        }
        for path in ruby_paths:
            assert {t for p, t in entity_types if p == path} == {"file"}, path
        assert ("tool.py", "function") in entity_types, "Python still parses"

        ruby_edges = {
            (row["path"], row["type"], row["parser"])
            for row in db.execute(
                "SELECT f.rel_path AS path, e.type AS type, e.parser AS parser "
                "FROM edges e JOIN files f ON f.id = e.file_id"
            )
            if row["path"] in ruby_paths
        }
        # The only edges a Ruby-claimed file may carry are the dependency layer's,
        # derived from the Gemfile's text by the global pass; nothing from Ruby
        # structure, and nothing at all from a .rb file.
        assert ruby_edges <= {("Gemfile", "DEPENDS_ON", GLOBAL_PARSER)}, ruby_edges
        assert not db.execute(
            "SELECT 1 FROM edges WHERE parser = ? LIMIT 1", (RubyParser.name,)
        ).fetchone()

        chunked = {
            row["path"]
            for row in db.execute(
                "SELECT DISTINCT f.rel_path AS path FROM chunks c JOIN files f ON f.id = c.file_id"
            )
        }
        assert set(ruby_paths) <= chunked, "Ruby stays searchable as text"

        declared = {
            (row["ecosystem"], row["name"])
            for row in db.execute(
                "SELECT ecosystem, name FROM dependencies WHERE role = 'declared'"
            )
        }
        assert ("gem", "rack") in declared, "the text-level Gemfile parser is untouched"

        located = {
            (d.path, d.message)
            for repository in report.repositories
            for d in repository.diagnostics
        }
        for path in ruby_paths:
            assert (path, STRUCTURAL_PARSING_DISABLED) in located, path
        assert not any(message == GRAMMAR_UNAVAILABLE for _, message in located)
    finally:
        workspace.close()
    assert tripwire == []


# ---------------------------------------------------------------------------
# The removal condition
# ---------------------------------------------------------------------------


def test_no_ruby_grammar_is_a_dependency_while_the_gate_stands() -> None:
    """The gate and a Ruby grammar dependency must not coexist by accident.

    If this fails because a ``tree-sitter-ruby`` release was added: first verify
    that release contains upstream commit ``ad907a69`` and installs offline as a
    wheel, pin it exactly, re-characterize the Ruby parser's output against the
    tests kept under the OFF-01 skip in ``tests/test_parsers_code.py``, and only
    then remove the gate in ``RubyParser.parse``, this test, and the skip.
    """
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert not [
        spec for spec in declared if spec.lower().replace("_", "-").startswith("tree-sitter-ruby")
    ]
    assert 'name = "tree-sitter-ruby"' not in lock
    assert RubyParser.version == "3"
    assert UPSTREAM_FIX in (ROOT / "repoatlas" / "parsers" / "ruby.py").read_text(encoding="utf-8")
