"""Conservative failure on malformed input (V1_SPEC §2, §7).

A parser that guesses when it cannot parse is worse than one that gives up: the
guess becomes a provenanced "fact" and gets cited. So every test here asserts two
things about broken input — *nothing was fabricated*, and *the failure was
recorded* — and, where recovery is possible, that what was recovered is marked
lower-confidence and carries how it was obtained.

Repository content is untrusted input (§17), so this doubles as the robustness
suite: no input in this file may raise out of a parser or abort a sync.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.models.parsing import ParsedFile, Severity
from repoatlas.parsers.jinja import JinjaParser, neutralize
from repoatlas.parsers.registry import ParserRegistry, default_parsers
from repoatlas.parsers.salt import SaltParser
from repoatlas.parsers.structured import StructuredParser
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace

# Inputs that are broken in structurally different ways. Each is fed to every
# parser that claims the extension, so a new parser inherits the whole matrix.
BROKEN_YAML: dict[str, str] = {
    "unclosed-flow-sequence": "a: [1, 2\nb: 3\n",
    "unclosed-flow-mapping": "a: {x: 1\n",
    "tab-indentation": "root:\n\tchild: 1\n",
    "bad-anchor": "a: *missing\n",
    "duplicate-keys": "a: 1\na: 2\n",
    "stray-close-brace": "a: }\n",
    "unterminated-quote": 'a: "unterminated\n',
    "tag-of-unknown-type": "a: !!python/object:os.system 'ls'\n",
    "control-characters": "a: \x01\x02\x03\n",
    "not-a-mapping": "- just\n- a\n- list\n",
    "scalar-document": "just a bare string\n",
    "empty": "",
    "only-a-comment": "# nothing here\n",
    "null-document": "---\n",
    "very-deep-nesting": "".join(f"{' ' * i}k{i}:\n" for i in range(200)),
}

BROKEN_SALT: dict[str, str] = {
    **BROKEN_YAML,
    "unterminated-jinja-expression": "state_id:\n  file.managed:\n    - name: {{ pillar['x'\n",
    "unterminated-jinja-block": "{% for h in hosts %}\nstate_id:\n  file.managed: []\n",
    "jinja-in-key-position": "{{ prefix }}_state:\n  file.managed: []\n",
    "state-without-a-function": "state_id:\n  - not: a function\n",
    "function-without-a-dot": "state_id:\n  managed:\n    - name: /etc/x\n",
    "requisite-naming-nothing": "state_id:\n  file.managed:\n    - require:\n      - pkg:\n",
    "binary-ish": "\x00\x01\x02 not text at all\n",
}

BROKEN_JINJA: dict[str, str] = {
    "unterminated-expression": "host = {{ pillar['service']['host']\n",
    "unterminated-block": "{% if x %}\nbody\n",
    "unterminated-comment": "{# never closed\n",
    "nested-delimiters": "{{ {{ x }} }}\n",
    "include-of-an-expression": "{% include some_variable %}\n",
    "empty-expression": "{{ }}\n",
    "only-delimiters": "{%%}{{}}{##}\n",
}


def _every_broken_input() -> list[tuple[str, str, str]]:
    return [
        *[("broken.yaml", name, text) for name, text in BROKEN_YAML.items()],
        *[("broken.sls", name, text) for name, text in BROKEN_SALT.items()],
        *[("broken.jinja", name, text) for name, text in BROKEN_JINJA.items()],
    ]


REGISTRY = ParserRegistry(default_parsers())


def _parse(filename: str, text: str) -> ParsedFile:
    parser = REGISTRY.for_path(Path(filename))
    assert parser is not None, filename
    return parser.parse(Path(filename), text)


# -- nothing raises, nothing is invented ------------------------------------


@pytest.mark.parametrize(("filename", "name", "text"), _every_broken_input(), ids=lambda v: v)
def test_broken_input_never_raises(filename: str, name: str, text: str) -> None:
    del name
    _parse(filename, text)


@pytest.mark.parametrize(("filename", "name", "text"), _every_broken_input(), ids=lambda v: v)
def test_broken_input_never_fabricates_provenance(filename: str, name: str, text: str) -> None:
    """Every line number a parser reports must exist in the file it read."""
    del name
    parsed = _parse(filename, text)
    line_count = max(1, len(text.splitlines()))
    for symbol in parsed.symbols:
        # A whole-file symbol over an empty file legitimately claims no lines.
        if symbol.line_start is None or symbol.line_end is None:
            assert not text, symbol
            assert symbol.line_start is None and symbol.line_end is None, symbol
            continue
        assert 1 <= symbol.line_start <= line_count
        assert symbol.line_start <= symbol.line_end <= line_count
    for reference in parsed.references:
        assert 1 <= reference.line_start <= line_count
    for diagnostic in parsed.diagnostics:
        assert diagnostic.line is None or 1 <= diagnostic.line <= line_count


@pytest.mark.parametrize(("filename", "name", "text"), _every_broken_input(), ids=lambda v: v)
def test_recovered_facts_are_marked_uncertain(filename: str, name: str, text: str) -> None:
    """Anything salvaged from a file that failed to parse is a claim, not a fact."""
    del name
    parsed = _parse(filename, text)
    if not any(d.severity is Severity.WARNING for d in parsed.diagnostics):
        return
    for reference in parsed.references:
        if reference.meta.get("recovery"):
            assert reference.confidence < 1.0


# -- the failure is recorded rather than swallowed --------------------------


#: The subset of broken YAML that is structurally invalid rather than merely
#: unusual, so a diagnostic is required rather than optional.
INVALID_YAML = sorted(
    name
    for name in BROKEN_YAML
    if name.startswith(("unclosed", "bad", "stray", "unterminated", "tab"))
)


@pytest.mark.parametrize("name", INVALID_YAML)
def test_structurally_invalid_yaml_produces_a_diagnostic(name: str) -> None:
    parsed = StructuredParser().parse(Path("broken.yaml"), BROKEN_YAML[name])
    assert parsed.diagnostics, name
    assert parsed.symbols == ()


def test_a_yaml_document_that_is_not_a_mapping_yields_no_configuration_keys() -> None:
    """A list has no configuration keys. Inventing indices as keys would be a lie."""
    parsed = StructuredParser().parse(Path("list.yaml"), BROKEN_YAML["not-a-mapping"])
    assert parsed.symbols == ()


def test_an_unsafe_yaml_tag_is_never_constructed(tmp_path: Path) -> None:
    """``!!python/object/apply:`` is arbitrary code execution in a naive loader.

    RepoAtlas composes nodes rather than constructing objects, so the tag is
    carried as text and never resolved. Asserted with a canary the payload would
    create if it ever ran (§17).
    """
    canary = tmp_path / "executed"
    source = f"a: !!python/object/apply:os.system ['touch {canary}']\n"
    parsed = StructuredParser().parse(Path("evil.yaml"), source)
    assert not canary.exists()
    # The key itself is still ordinary configuration data and is recorded.
    assert [s.qualified_name for s in parsed.symbols] == ["a"]


def test_a_yaml_alias_bomb_is_bounded() -> None:
    """The classic billion-laughs expansion. Composing nodes shares them rather
    than expanding them, so this is linear — asserted, not assumed."""
    source = "a: &a [x, x, x, x, x, x, x, x, x]\n"
    for letter, previous in zip("bcdefghij", "abcdefghi", strict=True):
        source += f"{letter}: &{letter} [*{previous}, *{previous}, *{previous}]\n"
    parsed = StructuredParser().parse(Path("bomb.yaml"), source)
    assert len(parsed.symbols) < 1000


# -- Salt --------------------------------------------------------------------


def test_unparseable_salt_recovers_only_what_a_line_scan_can_prove() -> None:
    source = BROKEN_SALT["unterminated-jinja-expression"]
    parsed = SaltParser().parse(Path("broken.sls"), source)
    assert parsed.diagnostics
    assert all(r.confidence < 1.0 for r in parsed.references if r.meta.get("recovery"))


def test_a_salt_state_without_a_function_declares_nothing() -> None:
    parsed = SaltParser().parse(Path("s.sls"), BROKEN_SALT["state-without-a-function"])
    assert not [r for r in parsed.references if r.meta.get("salt_function")]


def test_a_requisite_naming_nothing_produces_no_reference() -> None:
    """``- pkg:`` with no target names no state. An empty target must not become
    an edge to the empty string."""
    parsed = SaltParser().parse(Path("s.sls"), BROKEN_SALT["requisite-naming-nothing"])
    assert all(r.target_key.strip() for r in parsed.references)


def test_binary_content_yields_no_salt_facts() -> None:
    parsed = SaltParser().parse(Path("s.sls"), BROKEN_SALT["binary-ish"])
    assert parsed.symbols == ()


# -- Jinja -------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(BROKEN_JINJA))
def test_neutralization_never_changes_the_length_of_the_file(name: str) -> None:
    """Line and column marks are only trustworthy if the text keeps its shape,
    including when a delimiter is never closed."""
    source = BROKEN_JINJA[name]
    result = neutralize(source)
    assert len(result) == len(source)
    assert result.count("\n") == source.count("\n")


def test_an_unterminated_expression_is_left_alone_rather_than_half_removed() -> None:
    """Neutralization removes complete constructs. A partial one is not a
    construct, and rewriting it would move every column after it."""
    source = BROKEN_JINJA["unterminated-expression"]
    assert neutralize(source) == source


def test_a_non_literal_include_names_nothing() -> None:
    parsed = JinjaParser().parse(Path("t.jinja"), BROKEN_JINJA["include-of-an-expression"])
    assert parsed.references == ()


def test_empty_delimiters_produce_no_references() -> None:
    parsed = JinjaParser().parse(Path("t.jinja"), BROKEN_JINJA["only-delimiters"])
    assert parsed.references == ()


# -- end to end: a broken file must not break a sync -------------------------


def test_a_repository_full_of_broken_files_still_syncs(
    initialized: Workspace, workspace_root: Path
) -> None:
    """One unparseable file must not cost the other facts in the repository."""
    repo = workspace_root / "deployment"
    for index, text in enumerate(BROKEN_SALT.values()):
        (repo / f"broken_{index}.sls").write_text(text)
    for index, text in enumerate(BROKEN_JINJA.values()):
        (repo / f"broken_{index}.jinja").write_text(text)

    report = sync(initialized, initialized.resolver().resolve(repo))
    entry = report.repositories[0]
    assert entry.files_indexed > 0
    assert all(d.severity is not Severity.ERROR for d in entry.diagnostics)

    # The facts that were already there are untouched.
    rows = initialized.database.execute(
        "SELECT COUNT(*) AS n FROM entities WHERE key = 'env:SERVICE_HOST'"
    ).fetchone()
    assert rows["n"] == 1


def test_a_broken_file_is_still_searchable_as_text(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Failing to parse costs the structure, not the content: the file is still
    chunked and findable, which is what keeps a partial answer honest."""
    repo = workspace_root / "deployment"
    (repo / "broken.sls").write_text("unparseable_marker_state:\n  file.managed:\n\t- bad: [\n")
    sync(initialized, initialized.resolver().resolve(repo))
    assert initialized.search.find_literal("unparseable_marker_state").hits
