"""Provenance spans never cite a line that does not exist (V1_SPEC §11, ARCHITECTURE.md §11).

Five parsers computed a whole-file entity's last line as ``content.count("\\n") + 1``.
That is the number of line *slots* delimited by newlines, not the number of lines
holding anything, so every file that ends in a newline — nearly all of them — got
one phantom line past its end.

It was not cosmetic. The span is persisted, retrieved as DETERMINISTIC evidence,
and handed to the model; grounding then clamps a citation to *the supplied span*,
so for a seven-line file the model could cite ``config.py:8-8`` and be believed.
The enforcement layer was accepting a line that does not exist, because the fact
it was enforcing against was wrong.

The fix is one shared definition of "how many lines does this file have"
(`marks.line_count`), not five copies. The suite's centrepiece is accordingly
`test_every_parser_range_lies_inside_its_source`, which holds every parser to the
invariant rather than checking five known sites.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.grounding import ground_answer
from repoatlas.indexing.chunking import chunk_content
from repoatlas.models.evidence import Evidence, Hypothesis, RepoAtlasAnswer
from repoatlas.parsers.marks import line_count, whole_file_span
from repoatlas.parsers.registry import ParserRegistry
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import git

pytestmark = pytest.mark.adversarial

LF = chr(10)
CR = chr(13)
FF = chr(12)


def source_lines(content: str) -> list[str]:
    """The lines a user could open and cite, by the same rule the fix uses."""
    text = content.replace(CR + LF, LF).replace(CR, LF)
    lines = text.split(LF)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def widest(parsed) -> tuple[int, int]:
    """The whole-file entity's span: the one covering the most lines.

    Only ever asked of a file with content. A whole-file span is absent exactly
    when the file is empty, and that case has its own test.
    """
    spanned = [s for s in parsed.symbols if s.line_start is not None and s.line_end is not None]
    assert spanned, "every non-empty file has at least one spanned symbol"
    symbol = max(spanned, key=lambda s: s.line_end - s.line_start)
    return symbol.line_start, symbol.line_end


#: One minimal two-line body per parser, in the shape the review used.
BODIES = {
    "mod.py": "x = 1" + LF + "y = 2",
    "svc.ts": "const x = 1;" + LF + "const y = 2;",
    "svc.js": "const x = 1;" + LF + "const y = 2;",
    "run.sh": "echo one" + LF + "echo two",
    "Dockerfile": "FROM alpine" + LF + "RUN echo hello",
    # lib.rb is absent on purpose: Ruby is not parsed structurally (OFF-01), so it
    # yields no spans to be wrong about. That it claims none is asserted below.
}

#: Parsers the review found already correct. They must stay correct.
CONTROL_BODIES = {
    "state.sls": "svc:" + LF + "  service.running: []",
    "tmpl.jinja": "hello" + LF + "world",
    "conf.yaml": "a: 1" + LF + "b: 2",
    "conf.json": '{"a": 1,' + LF + '"b": 2}',
}


def parse(name: str, body: str):
    parser = ParserRegistry().for_path(Path(name))
    assert parser is not None, name
    return parser.parse(Path(name), body)


# == 1. the reproduced defect, per parser ====================================


@pytest.mark.parametrize("name", sorted(BODIES))
def test_a_terminal_newline_does_not_add_a_line(name: str) -> None:
    """The defect itself: two real lines must be ``1-2``, newline or not."""
    body = BODIES[name]
    assert widest(parse(name, body)) == (1, 2), f"{name} without a terminal newline"
    assert widest(parse(name, body + LF)) == (1, 2), f"{name} with a terminal newline"


@pytest.mark.parametrize("name", sorted(CONTROL_BODIES))
def test_the_already_correct_parsers_stay_correct(name: str) -> None:
    body = CONTROL_BODIES[name]
    before = widest(parse(name, body))
    after = widest(parse(name, body + LF))
    assert before == after, f"{name} changed when a terminal newline was added"
    assert after[1] <= len(source_lines(body + LF))


# == 2. the line-count model =================================================


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("", 0),
        ("a", 1),
        ("a" + LF, 1),
        ("a" + LF + "b", 2),
        ("a" + LF + "b" + LF, 2),
        (LF, 1),
        (LF + LF, 2),
        ("a" + LF + LF, 2),
        ("a" + LF + LF + LF, 3),
        ("   " + LF, 1),
        (CR + LF, 1),
        ("a" + CR + LF + "b" + CR + LF, 2),
        ("a" + CR + LF + "b", 2),
        ("a" + FF + "b", 1),
    ],
)
def test_the_source_line_model(content: str, expected: int) -> None:
    """Every case decided explicitly rather than inherited from an expression.

    The form-feed case is the one `str.splitlines()` gets wrong: it treats U+000C
    as a line break, while `ast` and the tree-sitter grammars do not. Adopting
    `splitlines()` would have replaced one phantom-line bug with a rarer one.
    """
    assert line_count(content) == expected
    assert line_count(content) == len(source_lines(content))


def test_trailing_blank_lines_are_real_lines() -> None:
    """The distinction the fix turns on: a terminal newline is not a line, but a
    blank line before it is."""
    assert line_count("x = 1" + LF) == 1
    assert line_count("x = 1" + LF + LF) == 2
    assert line_count("x = 1" + LF + LF + LF) == 3
    for name in sorted(BODIES):
        parsed = parse(name, BODIES[name] + LF + LF)
        assert widest(parsed)[1] == 3, name


def test_an_empty_file_claims_no_lines_at_all() -> None:
    """An empty file has zero lines, so its whole-file entity names none.

    The entity still exists — an empty ``__init__.py`` is a real, importable module
    whose removal would strand every import of that package — it simply makes no
    claim about source it does not have. Anchoring it at ``1-1`` instead, which is
    what it used to do, names a line the file does not contain: the same fabricated
    citation this suite exists to prevent, in the one file where it is easiest to
    excuse."""
    assert line_count("") == 0
    assert whole_file_span("") == (None, None)
    for name in sorted(BODIES):
        parsed = parse(name, "")
        assert parsed.symbols, f"{name}: an empty file still has a whole-file entity"
        for symbol in parsed.symbols:
            assert symbol.line_start is None, name
            assert symbol.line_end is None, name


@pytest.mark.parametrize(
    "body",
    [
        "x = 1" + LF + "y = 2",
        "def foo" + LF + "  1" + LF + "end" + LF + LF,
        "def alpha" + CR + "  1" + CR + "end" + CR,
        "",
    ],
)
def test_a_gated_ruby_file_claims_no_lines(body: str) -> None:
    """Ruby is not parsed structurally (OFF-01), so every provenance claim it could
    get wrong is a claim it does not make: no symbol, no reference, no span."""
    parsed = parse("lib.rb", body)
    assert parsed.symbols == ()
    assert parsed.references == ()


# == 3. the registry-wide invariant ==========================================


#: Deliberately awkward bodies: a multiline construct at EOF, a comment at EOF,
#: CRLF, blank-only files, and Unicode (Pass 3's subject, guarded here).
INVARIANT_BODIES = {
    "mod.py": ("import os" + LF + LF + "def foo():" + LF + "    x = 1" + LF + "    return x" + LF),
    "eof_comment.py": "x = 1" + LF + "# final comment" + LF,
    "unicode.py": "def créer_utilisateur():" + LF + "    return True" + LF,
    "crlf.py": "x = 1" + CR + LF + "y = 2" + CR + LF,
    "blank.py": LF + LF,
    "svc.ts": "export function foo() {" + LF + "  return 1;" + LF + "}" + LF,
    "run.sh": "foo() {" + LF + "  echo hello" + LF + "}" + LF,
    "lib.rb": "def foo" + LF + "  1" + LF + "end" + LF,
    "Dockerfile": (
        "FROM alpine" + LF + "RUN printf '%s" + chr(92) + "n' " + chr(92) + LF + "    hello" + LF
    ),
    "state.sls": "svc:" + LF + "  service.running: []" + LF,
    "tmpl.jinja": "{% if x %}" + LF + "hello" + LF + "{% endif %}" + LF,
    "conf.yaml": "a: 1" + LF + "b:" + LF + "  c: 2" + LF,
    "conf.json": '{"a": 1, "b": {"c": 2}}' + LF,
}


@pytest.mark.parametrize("name", sorted(INVARIANT_BODIES))
def test_every_parser_range_lies_inside_its_source(name: str) -> None:
    """The invariant that outlives the five known sites.

    Every symbol and every reference a parser emits is checked against the file
    it came from, and the cited lines are actually sliced out — a range that
    merely looks plausible is not the same as one that exists.
    """
    body = INVARIANT_BODIES[name]
    lines = source_lines(body)
    parsed = parse(name, body)

    for symbol in parsed.symbols:
        # An absent span is legal for exactly one thing: the whole of an empty
        # file. Anything else with no lines is a parser that forgot to record them.
        if symbol.line_start is None or symbol.line_end is None:
            assert not body, (name, symbol.name, "only an empty file may claim no lines")
            assert symbol.line_start is None and symbol.line_end is None, (name, symbol.name)
            continue
        assert symbol.line_start >= 1, (name, symbol.name, symbol.line_start)
        assert symbol.line_end >= symbol.line_start, (name, symbol.name)
        assert symbol.line_end <= max(1, len(lines)), (
            f"{name}: {symbol.kind.value} {symbol.name} claims "
            f"{symbol.line_start}-{symbol.line_end} but the file has {len(lines)} lines"
        )
        if lines:
            cited = lines[symbol.line_start - 1 : symbol.line_end]
            assert len(cited) == symbol.line_end - symbol.line_start + 1, (name, symbol.name)

    for reference in parsed.references:
        assert reference.line_start >= 1, (name, reference.target_key)
        assert reference.line_end >= reference.line_start, (name, reference.target_key)
        assert reference.line_end <= max(1, len(lines)), (
            f"{name}: reference {reference.target_key} claims "
            f"{reference.line_start}-{reference.line_end}, file has {len(lines)} lines"
        )


def test_a_multiline_construct_at_eof_keeps_its_own_range() -> None:
    """The whole-file span was wrong; the AST-derived spans were not. Fixing one
    must not move the other."""
    body = "def foo():" + LF + "    x = 1" + LF + "    return x" + LF
    parsed = parse("mod.py", body)
    functions = [s for s in parsed.symbols if s.kind.value == "function"]
    assert functions, "the fixture must actually define a function"
    assert (functions[0].line_start, functions[0].line_end) == (1, 3)
    assert widest(parsed) == (1, 3)


def test_a_comment_at_eof_is_inside_the_whole_file_span() -> None:
    """Whole-file means whole file. Salt's smallest-subtree rule does not apply
    here: the trailing comment is genuinely part of the file, so line 2 belongs to
    the span — while the phantom line 3 does not."""
    parsed = parse("mod.py", "x = 1" + LF + "# final comment" + LF)
    assert widest(parsed) == (1, 2)


# == 4. persisted state, retrieval and grounding =============================


SEVEN_LINES = (
    "import os"
    + LF
    + LF
    + 'SERVICE_HOST = os.environ["SERVICE_HOST"]'
    + LF
    + LF
    + LF
    + "def connect():"
    + LF
    + "    return SERVICE_HOST"
    + LF
)


@pytest.fixture
def indexed_seven(tmp_path: Path) -> Iterator[tuple[Workspace, Path]]:
    root = tmp_path / "repos"
    repo = root / "svc"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "config.py").write_text(SEVEN_LINES, encoding="utf-8")
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "initial")
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    initialize(workspace, root, config_path=tmp_path / "config.yaml")
    yield workspace, root
    workspace.close()


def test_the_fixture_really_has_seven_lines() -> None:
    assert len(source_lines(SEVEN_LINES)) == 7
    assert SEVEN_LINES.endswith(LF)


def test_no_persisted_entity_span_exceeds_its_file(
    indexed_seven: tuple[Workspace, Path],
) -> None:
    """§17: the authoritative store, not just the parser return value."""
    workspace, _root = indexed_seven
    rows = workspace.database.execute(
        "SELECT e.type, e.name, e.line_start, e.line_end, f.rel_path, r.path AS repo_path "
        "FROM entities e JOIN files f ON f.id = e.file_id "
        "JOIN repositories r ON r.id = e.repo_id "
        "WHERE e.line_end IS NOT NULL"
    ).fetchall()
    assert rows, "nothing was indexed"
    for row in rows:
        content = (Path(row["repo_path"]) / row["rel_path"]).read_text(encoding="utf-8")
        total = len(source_lines(content))
        assert row["line_end"] <= total, (
            f"{row['rel_path']}: {row['type']} {row['name']} claims "
            f"{row['line_start']}-{row['line_end']} of {total} lines"
        )


def test_the_module_entity_stops_at_the_last_real_line(
    indexed_seven: tuple[Workspace, Path],
) -> None:
    workspace, _ = indexed_seven
    row = workspace.database.execute(
        "SELECT line_start, line_end FROM entities WHERE type = 'module'"
    ).fetchone()
    assert (row["line_start"], row["line_end"]) == (1, 7)


def test_retrieval_reports_the_corrected_span(
    indexed_seven: tuple[Workspace, Path],
) -> None:
    """§18: the stored span is the fix, so retrieval is right without clamping."""
    workspace, root = indexed_seven
    context = workspace.resolver().resolve(root / "svc")
    found = workspace.retriever().retrieve("config", context)
    spans = {(c.line_start, c.line_end) for c in found.candidates}
    assert (1, 7) in spans, spans
    assert all(end is None or end <= 7 for _, end in spans), spans


def test_grounding_rejects_a_citation_to_the_phantom_line(
    indexed_seven: tuple[Workspace, Path],
) -> None:
    """§19: the reason this was a P2 and not a rendering nit.

    Grounding clamps a citation to the span it supplied. While the supplied span
    was 1-8, a model citing line 8 of a seven-line file was believed.
    """
    workspace, root = indexed_seven
    context = workspace.resolver().resolve(root / "svc")
    found = workspace.retriever().retrieve("config", context)

    def answer(start: int, end: int) -> RepoAtlasAnswer:
        return RepoAtlasAnswer(
            summary="s",
            hypotheses=[
                Hypothesis(
                    statement="SERVICE_HOST is read here",
                    confidence=0.9,
                    evidence=[
                        Evidence(
                            repository="svc",
                            path="src/config.py",
                            line_start=start,
                            line_end=end,
                            description="cited",
                        )
                    ],
                )
            ],
        )

    def cited(start: int, end: int) -> list[tuple[int | None, int | None]]:
        report = ground_answer(answer(start, end), found)
        return [(e.line_start, e.line_end) for h in report.answer.hypotheses for e in h.evidence]

    assert cited(7, 7) == [(7, 7)], "a real line must still be citable"
    assert cited(8, 8) == [], "line 8 of a seven-line file must not survive grounding"
    assert cited(99, 99) == []
    # A range that overlaps the end is clamped to the real last line, never past it.
    for _, end in cited(5, 20):
        assert end is not None and end <= 7


def test_no_chunk_claims_a_line_past_the_end(
    indexed_seven: tuple[Workspace, Path],
) -> None:
    """§21: chunk ranges are provenance too."""
    workspace, _root = indexed_seven
    rows = workspace.database.execute(
        "SELECT c.line_start, c.line_end, f.rel_path, r.path AS repo_path "
        "FROM chunks c JOIN files f ON f.id = c.file_id "
        "JOIN repositories r ON r.id = c.repo_id"
    ).fetchall()
    assert rows, "nothing was chunked"
    for row in rows:
        content = (Path(row["repo_path"]) / row["rel_path"]).read_text(encoding="utf-8")
        assert row["line_end"] <= len(source_lines(content)), dict(row)


def test_path_only_evidence_keeps_its_absent_line_range(
    indexed_seven: tuple[Workspace, Path],
) -> None:
    """§20: a file-identity candidate legitimately has no lines. The fix must not
    give it an invented whole-file span for the sake of uniformity."""
    workspace, root = indexed_seven
    context = workspace.resolver().resolve(root / "svc")
    found = workspace.retriever().retrieve("src/config.py", context)
    path_only = [c for c in found.candidates if c.line_start is None]
    assert path_only, "the file-identity candidate should still be produced"
    assert all(c.line_end is None for c in path_only)


# == 5. no downstream clamping ===============================================


def test_the_fix_is_not_a_renderer_workaround() -> None:
    """A `min(line_end, len(lines))` in rendering would hide the symptom while
    storage, the API and grounding stayed wrong. The authoritative value is the
    one the parser produces."""
    import ast

    for module in ("repoatlas/rendering/text.py", "repoatlas/retrieval/retriever.py"):
        tree = ast.parse(Path(module).read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "min":
                continue
            rendered = ast.unparse(node)
            assert "line_end" not in rendered, f"{module} clamps a span: {rendered}"


# == 6. one line model, shared by the parsers and the chunker ================


FF_SOURCE = "a = 1" + LF + FF + "b = 2" + LF
VT_SOURCE = "a = 1" + LF + "b = '" + chr(11) + "x'" + LF


def test_the_chunker_does_not_keep_its_own_line_model() -> None:
    """The defect's twin, found while auditing what enforces the invariant.

    `chunk_content` split with `str.splitlines()` while the parsers counted with
    the source-grammar splitter, so the two disagreed on every character
    `splitlines()` breaks on and no grammar does. A form feed made the chunker
    believe a two-line file had three, and it emitted a chunk holding `b = 2` at
    line 3 — a real string of source attributed to a line that does not exist.
    That is worse than the phantom end-of-file line, which at least cited nothing.
    """
    parsed = parse("mod.py", FF_SOURCE)
    total = line_count(FF_SOURCE)
    assert total == 2
    assert widest(parsed) == (1, 2)

    chunks = chunk_content(FF_SOURCE, parsed.symbols)
    assert chunks
    for chunk in chunks:
        assert chunk.line_end <= total, (
            f"chunk {chunk.line_start}-{chunk.line_end} of a {total}-line file: {chunk.text!r}"
        )
    assert "b = 2" in "".join(c.text for c in chunks), "the content must still be chunked"


def test_a_vertical_tab_inside_a_string_is_not_a_line_break() -> None:
    """`b = '\\x0bx'` is one line of Python. `splitlines()` broke the literal in
    half and gave each half a line number, so an exact search for the string found
    two fragments on two lines that were really one."""
    parsed = parse("mod.py", VT_SOURCE)
    assert line_count(VT_SOURCE) == 2
    assert widest(parsed) == (1, 2)
    chunks = chunk_content(VT_SOURCE, parsed.symbols)
    assert len(chunks) == 1, [(c.line_start, c.line_end, c.text) for c in chunks]
    assert chunks[0].text.count(chr(11)) == 1, "the literal must survive intact"


@pytest.mark.parametrize("source", [FF_SOURCE, VT_SOURCE])
def test_every_chunk_is_a_faithful_slice_of_its_own_lines(source: str) -> None:
    """A chunk's text must be exactly the lines its range names. Comparing the two
    independently catches a chunker whose ranges are in bounds but shifted."""
    lines = source_lines(source)
    for chunk in chunk_content(source, parse("mod.py", source).symbols):
        expected = LF.join(lines[chunk.line_start - 1 : chunk.line_end])
        assert chunk.text == expected, (chunk.line_start, chunk.line_end, chunk.text)


def test_the_pyyaml_backed_parsers_keep_pyyaml_line_semantics() -> None:
    """The trap on the other side of the fix.

    "One line model everywhere" is the wrong lesson. A line array indexed by a
    PyYAML mark must use *PyYAML's* line breaks, which are `splitlines()`'s, not
    the source-grammar splitter's. The two differ only on characters PyYAML either
    rejects outright or breaks on identically — this pins both halves of that
    claim, so a later "unify the splitters" refactor fails here rather than
    silently shifting every YAML line number in a file containing U+2028.
    """
    import yaml

    for rejected in (FF, chr(11)):
        with pytest.raises(yaml.YAMLError):
            yaml.compose("a: 1" + rejected + "b: 2" + LF)

    for accepted in (chr(0x85), chr(0x2028), chr(0x2029)):
        document = "a: 1" + accepted + "b: 2" + LF
        node = yaml.compose(document)
        marks = {key.value: key.start_mark.line for key, _ in node.value}
        assert marks == {"a": 0, "b": 1}, accepted
        assert len(document.splitlines()) == 2, accepted


def test_a_salt_file_that_pyyaml_refuses_is_scanned_with_source_line_numbers() -> None:
    """The one PyYAML-shaped path where `splitlines()` is wrong: the fallback scan
    runs *because* PyYAML refused the content, and a form feed is one of the two
    things it refuses. Counting it as a break would report every state after it one
    line too low."""
    body = (
        "first:" + LF + "  service.running: []" + FF + LF + "second:" + LF + "  test.nop: []" + LF
    )
    # The discriminating shape: `splitlines()` sees five lines here and would put
    # `second` on line 4; the file has four and `second` is on line 3.
    assert len(body.splitlines()) == 5
    parsed = parse("state.sls", body)
    total = line_count(body)
    assert total == 4
    states = {s.name: (s.line_start, s.line_end) for s in parsed.symbols}
    assert "second" in states, states
    assert states["second"][0] == 3, states
    for name, (start, end) in states.items():
        assert end is not None and end <= total, (name, start, end)


def test_a_dockerfile_instruction_keeps_its_true_line() -> None:
    """The Dockerfile parser enumerates lines itself to join continuations, and had
    its own model too. With a form feed, every instruction after it was reported one
    line too low — and the last one landed past the end of the file."""
    body = "FROM alpine" + LF + "RUN echo one" + FF + LF + "RUN echo two" + LF
    total = line_count(body)
    assert total == 3
    assert len(body.splitlines()) == 4, "the discriminating shape"

    parsed = parse("Dockerfile", body)
    for symbol in parsed.symbols:
        assert symbol.line_end is not None and symbol.line_end <= total, symbol
    lines = {r.target_key: (r.line_start, r.line_end) for r in parsed.references}
    assert lines["alpine"] == (1, 1), lines
    assert max(end for _, end in lines.values()) == total, lines


# == 7. classic-Mac line endings, end to end =================================


CR_BODIES = {
    "mod.py": "def alpha():{s}    return 1{s}{s}def beta():{s}    return 2{s}",
    "svc.ts": (
        "export function alpha() {{{s}  return 1;{s}}}{s}{s}"
        "export function beta() {{{s}  return 2;{s}}}{s}"
    ),
    "run.sh": "alpha() {{{s}  echo one{s}}}{s}{s}beta() {{{s}  echo two{s}}}{s}",
    # lib.rb: gated (OFF-01); see test_a_gated_ruby_file_claims_no_lines.
}


@pytest.mark.parametrize("name", sorted(CR_BODIES))
def test_a_lone_carriage_return_is_a_line_terminator(name: str) -> None:
    """Reported as untested after the last pass; testing it found a defect.

    The tree-sitter grammars for TypeScript, Ruby and Bash do not accept a lone
    `\\r`, so a classic-Mac file collapsed into a single line: `beta` on lines 5-7
    was reported at `1-1`, and grounding would have believed a citation to line 1
    for content that is not there. Bash lost both definitions entirely. A CR-only
    file must now produce exactly what the same file with newlines produces.
    """
    template = CR_BODIES[name]
    with_lf = template.format(s=LF)
    with_cr = template.format(s=CR)
    assert line_count(with_cr) == line_count(with_lf)

    def spans(body: str) -> dict[str, tuple[int | None, int | None]]:
        return {s.name: (s.line_start, s.line_end) for s in parse(name, body).symbols}

    lf_spans = spans(with_lf)
    assert lf_spans == spans(with_cr), name
    assert "beta" in lf_spans, f"{name}: the trailing definition must survive"
    # `beta` follows a blank line in every body, so its span must start past the
    # first definition — the exact thing the collapse destroyed.
    assert lf_spans["beta"][0] is not None and lf_spans["beta"][0] > 2, (name, lf_spans)


def test_a_mixed_file_is_left_exactly_as_it_is() -> None:
    """The rewrite is confined to files with no `\\n` at all. Where the two are
    mixed, nothing can tell a terminator from a carriage return inside a string, so
    guessing would be the fabrication the rewrite exists to prevent."""
    mixed = "def alpha():" + LF + "    return '" + CR + "'" + LF
    parsed = parse("mod.py", mixed)
    for symbol in parsed.symbols:
        assert symbol.line_end is not None
        assert symbol.line_end <= line_count(mixed), (symbol.name, symbol.line_end)


def test_a_carriage_return_file_survives_a_real_index(tmp_path: Path) -> None:
    """End to end, which is what the concern actually asked for: through the
    scanner, the store and retrieval, not just the parser."""
    root = tmp_path / "repos"
    repo = root / "legacy"
    repo.mkdir(parents=True)
    body = CR_BODIES["mod.py"].format(s=CR)
    (repo / "legacy.py").write_bytes(body.encode("utf-8"))
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "initial")
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        initialize(workspace, root, config_path=tmp_path / "config.yaml")
        rows = workspace.database.execute(
            "SELECT name, line_start, line_end FROM entities "
            "WHERE line_end IS NOT NULL AND name = 'beta'"
        ).fetchall()
        assert rows, "the trailing definition must reach the store"
        expected = {
            s.name: (s.line_start, s.line_end)
            for s in parse("legacy.py", CR_BODIES["mod.py"].format(s=LF)).symbols
        }
        assert (rows[0]["line_start"], rows[0]["line_end"]) == expected["beta"]
        total = line_count(body)
        for row in workspace.database.execute("SELECT line_start, line_end FROM chunks").fetchall():
            assert row["line_end"] <= total, dict(row)
    finally:
        workspace.close()


# == 8. an empty file, end to end ============================================


def test_an_empty_module_is_stored_with_no_line_numbers(tmp_path: Path) -> None:
    """The concern the last pass documented rather than fixed.

    An empty `__init__.py` is a real, importable module, so the entity has to stay
    — but it has no lines, so it must claim none. `1-1` named a line the file does
    not contain.
    """
    root = tmp_path / "repos"
    package = root / "svc" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "mod.py").write_text("x = 1" + LF, encoding="utf-8")
    repo = root / "svc"
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "initial")
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        initialize(workspace, root, config_path=tmp_path / "config.yaml")
        rows = workspace.database.execute(
            "SELECT e.type, e.name, e.line_start, e.line_end FROM entities e "
            "JOIN files f ON f.id = e.file_id WHERE f.rel_path = 'pkg/__init__.py'"
        ).fetchall()
        kinds = {row["type"] for row in rows}
        assert "module" in kinds, "the empty module entity must survive"
        for row in rows:
            assert row["line_start"] is None, dict(row)
            assert row["line_end"] is None, dict(row)

        empty_chunks = workspace.database.execute(
            "SELECT COUNT(*) AS n FROM chunks c JOIN files f ON f.id = c.file_id "
            "WHERE f.rel_path = 'pkg/__init__.py'"
        ).fetchone()
        assert empty_chunks["n"] == 0, "an empty file has nothing to chunk"

        # The neighbouring real file is untouched by the absent span.
        neighbour = workspace.database.execute(
            "SELECT e.line_start, e.line_end FROM entities e JOIN files f ON f.id = e.file_id "
            "WHERE f.rel_path = 'pkg/mod.py' AND e.type = 'module'"
        ).fetchone()
        assert (neighbour["line_start"], neighbour["line_end"]) == (1, 1)
    finally:
        workspace.close()


# == 9. an index built by an older build says so =============================


def test_status_will_not_call_an_index_built_by_an_older_build_current(
    indexed_seven: tuple[Workspace, Path],
) -> None:
    """The other concern the last pass left open: the fix reached new syncs, but an
    untouched repository reported itself `current` while holding facts the current
    build would compute differently.

    Nothing else notices — HEAD, mtime and content hash all still agree, because
    the file genuinely did not change. The facts version is the only signal, and
    `status` promises never to claim currency it cannot support.
    """
    from repoatlas.services.status_service import status

    workspace, root = indexed_seven
    context = workspace.resolver().resolve(root / "svc")

    assert status(workspace, context).index_state == "current"

    with workspace.database.transaction() as conn:
        conn.execute("UPDATE files SET parser_version = ?", ("schema:1||python-ast:1",))
    report = status(workspace, context)
    assert report.index_state == "stale", report.index_state
    assert [r.index_state for r in report.repositories] == ["stale (indexed by an older build)"]


def test_the_facts_version_covers_every_input_that_changes_a_fact() -> None:
    """A parser fix that does not move the facts version never reaches an existing
    index. So does a chunking fix — which is why chunking is in it: the spans in
    `chunks` are cited evidence exactly as an entity's are."""
    from repoatlas.dependencies.registry import DependencyRegistry
    from repoatlas.indexing.chunking import CHUNKING_VERSION
    from repoatlas.sync.planner import facts_version

    version = facts_version(ParserRegistry(), DependencyRegistry())
    assert f"chunks:{CHUNKING_VERSION}" in version
    for parser in ParserRegistry()._parsers:
        assert f"{parser.name}:{parser.version}" in version, parser.name


# == 10. the dependency layer uses the same line model =======================


DEPENDENCY_CASES = {
    "pyproject.toml": '[project]{s}name = "x"{s}dependencies = ["requests>=2"]{s}',
    "package.json": '{{"name":"x",{s}"dependencies":{{"left-pad":"^1.0.0"}}}}',
    "Gemfile": 'source "https://rubygems.org"{s}gem "rails", "~> 7.0"{s}',
}


def _dependency_lines(name: str, body: str) -> list[tuple[str, int]]:
    from repoatlas.dependencies.registry import DependencyRegistry

    found: list[tuple[str, int]] = []
    for parser in DependencyRegistry().for_path(Path(name)):
        report = parser.parse(Path(name), body)
        found.extend((d.name, d.line) for d in list(report.declared) + list(report.resolved))
    return found


@pytest.mark.parametrize("name", sorted(DEPENDENCY_CASES))
def test_a_dependency_never_names_a_line_its_manifest_lacks(name: str) -> None:
    """A manifest parser reports provenance too, and had its own line model.

    A Gemfile containing a form feed put `rails` on line 3 of a two-line file —
    out of range, from `str.splitlines()`. A CR-only `package.json` put `left-pad`
    on line 1 when it is on line 2 — in range, and wrong, which is the worse of
    the two because nothing downstream can tell.
    """
    template = DEPENDENCY_CASES[name]
    for body in (
        template.format(s=LF),
        template.format(s=CR),
        template.format(s=LF).replace(LF, FF + LF, 1),
    ):
        total = line_count(body)
        for package, line in _dependency_lines(name, body):
            assert 1 <= line <= max(1, total), (name, package, line, total)


def test_a_dependency_line_survives_a_change_of_line_ending() -> None:
    """The same manifest, written with newlines and with carriage returns, must
    report the same lines. Anything else means the line model moved."""
    for name, template in sorted(DEPENDENCY_CASES.items()):
        with_lf = _dependency_lines(name, template.format(s=LF))
        with_cr = _dependency_lines(name, template.format(s=CR))
        if not with_cr:
            continue  # tomllib and json refuse a lone CR; a refusal cites nothing.
        assert with_lf == with_cr, name
