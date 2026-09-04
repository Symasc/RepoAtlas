"""Salt state provenance ranges (V1_SPEC §11, ARCHITECTURE.md §5).

The invariant these tests exist to pin:

    A deterministic entity or fact may cite only the smallest source span that
    actually establishes it.

For a Salt state that means the span ends on the **last source line structurally
belonging to that state**. Top-level Jinja controls, sibling states, trailing
comments and separator whitespace are not evidence for the preceding state; Jinja
written *inside* a state's value is.

Why an off-by-a-few-lines bug is worth a suite: a deterministic parser range is
the authoritative supplied span. Retrieval renders it, and citation validation
checks a model's claims against it. A range that reaches into the next state lets
an answer cite `cmd.run: rm -rf /` as support for a `test.nop`, with a
file-and-line reference that looks exactly like a real one.

Every assertion states the exact expected range and the exact covered text. A
test that only asserted "the range got smaller" would pass for a parser that
returned `(1, 1)` for everything.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.models.parsing import EntityType, ParsedFile, ParsedSymbol
from repoatlas.parsers.jinja import neutralize
from repoatlas.parsers.salt import SaltParser
from repoatlas.services.init_service import initialize
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import fact_snapshot, git, init_git_repository

pytestmark = pytest.mark.adversarial


# -- helpers -----------------------------------------------------------------


def parse(source: str, name: str = "init.sls") -> ParsedFile:
    return SaltParser().parse(Path(name), source)


def state(source: str, name: str) -> ParsedSymbol:
    parsed = parse(source)
    found = [s for s in parsed.symbols if s.name == name and s.kind is EntityType.SALT_STATE]
    assert found, f"{name!r} not found in {[s.name for s in parsed.symbols]}"
    return found[0]


def covered(source: str, symbol: ParsedSymbol) -> list[str]:
    """Exactly the lines the symbol claims as its evidence."""
    return source.splitlines()[symbol.line_start - 1 : symbol.line_end]


def assert_span(source: str, name: str, start: int, end: int, *, text: list[str]) -> None:
    symbol = state(source, name)
    assert (symbol.line_start, symbol.line_end) == (start, end), (
        name,
        (symbol.line_start, symbol.line_end),
        covered(source, symbol),
    )
    assert covered(source, symbol) == text


def forbid(source: str, name: str, *fragments: str) -> None:
    """No line the symbol cites may contain any of these."""
    body = "\n".join(covered(source, state(source, name)))
    for fragment in fragments:
        assert fragment not in body, (name, fragment, body)


# == 1. the review fixture ===================================================

REVIEW = """\
{% if grains['os'] == 'Ubuntu' %}

install_nginx:
  pkg.installed:
    - name: nginx

{% endif %}
"""


def test_the_review_fixture_stops_before_the_trailing_jinja() -> None:
    """The finding itself. The state ended on `{% endif %}`, four lines of which
    two were blank and one belonged to a conditional, not to the package."""
    assert_span(
        REVIEW,
        "install_nginx",
        3,
        5,
        text=["install_nginx:", "  pkg.installed:", "    - name: nginx"],
    )
    forbid(REVIEW, "install_nginx", "{% endif %}", "{% if")


def test_a_leading_jinja_condition_is_context_not_definition() -> None:
    """A symbol representing `install_nginx` is not a symbol representing the
    condition that guards it, so the span starts at the state ID."""
    assert state(REVIEW, "install_nginx").line_start == 3


# == 2. sibling boundaries ===================================================


@pytest.mark.parametrize(
    ("gap", "second_start"),
    [
        ("", 4),
        ("\n", 5),
        ("\n\n\n", 7),
        ("\n# a comment about state_b\n", 6),
        ("\n{% if pillar.get('x') %}\n", 6),
    ],
    ids=["none", "one-blank", "three-blank", "comment", "jinja"],
)
def test_a_state_never_absorbs_its_sibling(gap: str, second_start: int) -> None:
    source = (
        "state_a:\n  pkg.installed:\n    - name: a\n"
        + gap
        + "state_b:\n  pkg.installed:\n    - name: b\n"
    )
    if gap.strip().startswith("{%"):
        source += "{% endif %}\n"
    assert_span(
        source,
        "state_a",
        1,
        3,
        text=["state_a:", "  pkg.installed:", "    - name: a"],
    )
    first, second = state(source, "state_a"), state(source, "state_b")
    assert first.line_end < second.line_start
    assert second.line_start == second_start
    forbid(source, "state_a", "state_b:", "# a comment", "{% if")


def test_the_last_state_stops_before_its_closing_jinja() -> None:
    source = (
        "state_a:\n  test.nop: []\n\n{% if condition %}\nstate_b:\n  test.nop: []\n{% endif %}\n"
    )
    assert_span(source, "state_a", 1, 2, text=["state_a:", "  test.nop: []"])
    assert_span(source, "state_b", 5, 6, text=["state_b:", "  test.nop: []"])
    forbid(source, "state_a", "{% if")
    forbid(source, "state_b", "{% endif %}")


def test_several_states_inside_one_jinja_block_each_keep_their_own_span() -> None:
    source = (
        "{% if grains['os'] == 'Ubuntu' %}\n"
        "state_a:\n  test.nop: []\n"
        "\n"
        "state_b:\n  test.nop: []\n"
        "\n"
        "state_c:\n  test.nop: []\n"
        "{% endif %}\n"
    )
    assert_span(source, "state_a", 2, 3, text=["state_a:", "  test.nop: []"])
    assert_span(source, "state_b", 5, 6, text=["state_b:", "  test.nop: []"])
    assert_span(source, "state_c", 8, 9, text=["state_c:", "  test.nop: []"])


def test_nested_jinja_does_not_widen_the_states_it_wraps() -> None:
    source = (
        "{% if grains['os'] == 'Ubuntu' %}\n"
        "{% if pillar.get('enabled') %}\n"
        "state_a:\n  test.nop: []\n"
        "{% endif %}\n"
        "{% endif %}\n"
    )
    assert_span(source, "state_a", 3, 4, text=["state_a:", "  test.nop: []"])


# == 3. comments and whitespace ==============================================


def test_a_trailing_comment_is_not_evidence_for_the_state_above_it() -> None:
    source = "state_a:\n  test.nop: []\n# explanation about state_b\n"
    assert_span(source, "state_a", 1, 2, text=["state_a:", "  test.nop: []"])
    forbid(source, "state_a", "# explanation")


def test_a_comment_inside_the_state_stays_inside_the_span() -> None:
    """Structural placement, not "comments bad": a comment between a state's own
    entries is inside its block, and excluding it would break the span."""
    source = "state_a:\n  file.managed:\n    # why\n    - name: /etc/a\n"
    assert_span(
        source,
        "state_a",
        1,
        4,
        text=["state_a:", "  file.managed:", "    # why", "    - name: /etc/a"],
    )


@pytest.mark.parametrize("trailer", ["", "\n", "\n\n\n", "\n# a note\n", "\n   \n\t\n"])
def test_trailing_file_whitespace_never_enlarges_the_last_state(trailer: str) -> None:
    source = "state_a:\n  test.nop: []\n" + trailer
    assert_span(source, "state_a", 1, 2, text=["state_a:", "  test.nop: []"])


def test_a_state_ending_at_eof_without_a_newline_reports_the_same_line() -> None:
    with_newline = "state_a:\n  test.nop: []\n"
    without = "state_a:\n  test.nop: []"
    assert state(with_newline, "state_a").line_end == 2
    assert state(without, "state_a").line_end == 2


# == 4. real state content is never truncated ================================


def test_a_literal_block_scalar_is_kept_whole() -> None:
    source = (
        "config:\n"
        "  file.managed:\n"
        "    - contents: |\n"
        "        line one\n"
        "        line two\n"
        "        line three\n"
        "\n"
        "next_state:\n  test.nop: []\n"
    )
    assert_span(
        source,
        "config",
        1,
        6,
        text=[
            "config:",
            "  file.managed:",
            "    - contents: |",
            "        line one",
            "        line two",
            "        line three",
        ],
    )
    forbid(source, "config", "next_state:")


def test_a_folded_block_scalar_is_kept_whole() -> None:
    """A folded scalar joins its lines with spaces, so the parsed value's newline
    count says nothing about how many source lines it occupied."""
    source = (
        "config:\n"
        "  file.managed:\n"
        "    - contents: >\n"
        "        folded one\n"
        "        folded two\n"
        "\n"
        "next_state:\n  test.nop: []\n"
    )
    assert_span(
        source,
        "config",
        1,
        5,
        text=[
            "config:",
            "  file.managed:",
            "    - contents: >",
            "        folded one",
            "        folded two",
        ],
    )


@pytest.mark.parametrize("indicator", ["|", "|-", "|+", ">", ">-"])
def test_every_block_scalar_chomping_form_keeps_its_content(indicator: str) -> None:
    source = (
        f"config:\n  file.managed:\n    - contents: {indicator}\n"
        "        one\n        two\n\nnext_state:\n  test.nop: []\n"
    )
    symbol = state(source, "config")
    assert symbol.line_start == 1
    assert symbol.line_end == 5, covered(source, symbol)
    forbid(source, "config", "next_state:")


def test_nested_mappings_are_all_inside_the_span() -> None:
    source = (
        "app:\n"
        "  file.managed:\n"
        "    - name: /etc/app/config.yaml\n"
        "    - context:\n"
        "        database:\n"
        "          host: db.internal\n"
        "          port: 5432\n"
    )
    assert state(source, "app").line_end == 7


def test_requisites_are_inside_the_span_and_still_produce_edges() -> None:
    source = (
        "service:\n"
        "  service.running:\n"
        "    - require:\n"
        "      - pkg: package\n"
        "    - watch:\n"
        "      - file: config\n"
    )
    assert state(source, "service").line_end == 6
    targets = {r.target_key for r in parse(source).references}
    assert {"package", "config"} <= targets


def test_a_jinja_expression_inside_a_value_stays_within_the_span() -> None:
    """The rule is structural placement, not "Jinja bad"."""
    source = "config:\n  file.managed:\n    - source: salt://{{ pillar['app'] }}/config\n"
    assert_span(
        source,
        "config",
        1,
        3,
        text=[
            "config:",
            "  file.managed:",
            "    - source: salt://{{ pillar['app'] }}/config",
        ],
    )


def test_a_multiline_jinja_expression_inside_a_value_falls_back_conservatively() -> None:
    """A known limitation, pinned rather than papered over.

    Neutralization replaces an expression character-for-character, so a ``{{ }}``
    spanning several lines puts filler at column 0 on the continuation lines and
    destroys the block scalar's indentation. YAML then fails and the line-scan
    recovers the state at reduced confidence. The span it produces is still
    truthful — the state's own indented block, nothing beyond it — which is what
    this pass is about; making the construct parse is not.
    """
    source = (
        "state:\n"
        "  file.managed:\n"
        "    - context:\n"
        "        value: >-\n"
        "          {{ pillar.get(\n"
        "               'value',\n"
        "               'default'\n"
        "             ) }}\n"
    )
    symbol = state(source, "state")
    assert symbol.meta.get("recovery") == "line-scan"
    assert (symbol.line_start, symbol.line_end) == (1, 8), covered(source, symbol)
    assert parse(source).diagnostics


# == 5. Jinja neutralization preserves coordinates ===========================


@pytest.mark.parametrize(
    "source",
    [
        "a: 1\n{% if x %}\nb: 2\n{% endif %}\nc: 3\n",
        "a: {{ value }}\nb: 2\n",
        "{#\nmultiline\ncomment\n#}\na: 1\n",
        "{%-\n  if x\n-%}\na: 1\n{%- endif -%}\n",
        "a: {{ pillar.get(\n  'x',\n  'y') }}\nb: 2\n",
        "{% raw %}\n{{ not_jinja }}\n{% endraw %}\na: 1\n",
    ],
    ids=["statement", "expression", "block-comment", "whitespace-control", "multiline", "raw"],
)
def test_neutralization_preserves_every_line_position(source: str) -> None:
    """The whole range model rests on this: marks come from the neutralized text
    and are read against the raw file, so the two must agree line for line."""
    result = neutralize(source)
    assert len(result) == len(source)
    assert result.count("\n") == source.count("\n")
    assert len(result.splitlines()) == len(source.splitlines())
    for index, (original, replaced) in enumerate(zip(source, result, strict=True)):
        if original == "\n":
            assert replaced == "\n", index


def test_neutralization_keeps_a_states_line_number_after_multiline_jinja() -> None:
    source = (
        "{% if pillar.get(\n"
        "     'enabled',\n"
        "     False) %}\n"
        "state_a:\n  test.nop: []\n"
        "{% endif %}\n"
    )
    assert_span(source, "state_a", 4, 5, text=["state_a:", "  test.nop: []"])


# == 6. top-level constructs that are not states =============================


def test_include_targets_cite_their_own_line() -> None:
    source = "include:\n  - common\n  - nginx\n\nstate_a:\n  test.nop: []\n"
    parsed = parse(source)
    includes = {
        r.target_key: (r.line_start, r.line_end)
        for r in parsed.references
        if r.meta.get("via") == "include"
    }
    assert includes == {"common": (2, 2), "nginx": (3, 3)}
    assert_span(source, "state_a", 5, 6, text=["state_a:", "  test.nop: []"])


def test_extend_targets_cite_their_own_line() -> None:
    """Salt `extend` semantics are a known V1 limitation; only the range of what
    the parser already emits is under test here."""
    source = "extend:\n  apache:\n    service.running:\n      - watch: []\n\nx:\n  test.nop: []\n"
    parsed = parse(source)
    extends = [
        (r.target_key, r.line_start, r.line_end)
        for r in parsed.references
        if r.meta.get("via") == "extend"
    ]
    assert extends == [("apache", 2, 2)]


def test_pillar_data_keys_get_their_own_narrow_span() -> None:
    """The non-state half of an `.sls` tree goes through the same range model."""
    source = "service:\n  host: db.internal\n  port: 5432\n\nother:\n  value: 1\n"
    parsed = parse(source)
    spans = {s.qualified_name: (s.line_start, s.line_end) for s in parsed.symbols}
    assert spans["service"] == (1, 3)
    assert spans["service.host"] == (2, 2)
    assert spans["service.port"] == (3, 3)
    assert spans["other"] == (5, 6)


# == 7. dynamic state identity stays fail-closed =============================


def test_a_jinja_generated_state_name_is_not_a_deterministic_entity() -> None:
    source = (
        "{% for service in pillar.get('services', []) %}\n"
        "service_{{ service }}:\n"
        "  service.running:\n"
        "    - name: {{ service }}\n"
        "{% endfor %}\n"
    )
    names = {s.name for s in parse(source).symbols if s.kind is EntityType.SALT_STATE}
    assert not [n for n in names if "{{" in n or "{%" in n], names


def test_a_fully_templated_state_name_invents_no_identity() -> None:
    source = "{{ pillar['name'] }}:\n  service.running: []\n"
    names = {s.name for s in parse(source).symbols}
    assert not [n for n in names if "{{" in n], names


# == 8. malformed input fails conservatively =================================


@pytest.mark.parametrize(
    "source",
    [
        "state_a:\n  test.nop: []\n{% if\nmissing %}\nstate_b:\n  test.nop: []\n",
        "state_a:\n   test.nop:\n  - broken indentation\n     - more\n",
        "state_a:\n  test.nop:\n    - name: [unclosed\n",
        "state_a:\n  test.nop: []\nstate_a:\n  test.nop: []\n",
        "state_a:\n  test.nop: []\n{{ dangling\n",
        "\t- tab indented\n  state: x\n",
    ],
    ids=["unfinished-if", "bad-indent", "unclosed-list", "duplicate-key", "dangling", "tabs"],
)
def test_malformed_input_never_swallows_a_sibling_construct(source: str) -> None:
    """A parse that fails halfway must lose facts, not widen them.

    "Does it reach EOF?" is the wrong question — one state really can be the whole
    of a four-line file. The property that matters is that a span never reaches
    over a *sibling*: no line it cites, other than its own first, may begin a new
    top-level construct.
    """
    parsed = parse(source)
    lines = source.splitlines()
    for symbol in parsed.symbols:
        assert 1 <= symbol.line_start <= symbol.line_end <= len(lines), symbol
        for text in lines[symbol.line_start : symbol.line_end]:
            assert not text.strip() or text[0].isspace(), (symbol, text)


# == 9. line endings =========================================================


def test_crlf_line_endings_produce_the_same_ranges_as_lf() -> None:
    lf = "{% if x %}\nstate_a:\n  pkg.installed:\n    - name: a\n{% endif %}\n"
    crlf = lf.replace("\n", "\r\n")
    assert state(lf, "state_a").line_start == state(crlf, "state_a").line_start
    assert state(lf, "state_a").line_end == state(crlf, "state_a").line_end
    assert (state(crlf, "state_a").line_start, state(crlf, "state_a").line_end) == (2, 4)


# == 10. the false-support case ==============================================

DANGEROUS = """\
safe_state:
  test.nop:
    - name: safe

{% if false %}
{% endif %}

dangerous_unrelated_state:
  cmd.run:
    - name: rm -rf /example
"""


def test_a_states_evidence_can_never_contain_an_unrelated_command() -> None:
    """Why the bug is not cosmetic line numbering.

    Under the old range, `safe_state` cited lines 1-7 — and a range that walks
    past its own block is one blank line away from citing the `cmd.run` below it
    as support for a `test.nop`.
    """
    assert_span(
        DANGEROUS,
        "safe_state",
        1,
        3,
        text=["safe_state:", "  test.nop:", "    - name: safe"],
    )
    forbid(DANGEROUS, "safe_state", "rm -rf", "cmd.run", "dangerous_unrelated_state", "{%")


def test_the_dangerous_state_still_cites_only_itself() -> None:
    assert_span(
        DANGEROUS,
        "dangerous_unrelated_state",
        8,
        10,
        text=[
            "dangerous_unrelated_state:",
            "  cmd.run:",
            "    - name: rm -rf /example",
        ],
    )


# == 11. persisted ranges ====================================================


def _root(tmp_path: Path, tag: str, sls: str) -> Path:
    root = tmp_path / f"{tag}-root"
    repo = root / "deployment"
    (repo / "salt").mkdir(parents=True)
    (repo / "salt" / "app.sls").write_text(sls, encoding="utf-8")
    (repo / "README.md").write_text("# deployment\n", encoding="utf-8")
    init_git_repository(repo)
    return root


def _workspace(tmp_path: Path, tag: str, root: Path) -> Workspace:
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / f"{tag}-state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    space = open_workspace(settings=settings, require_root=False)
    initialize(space, root, config_path=tmp_path / f"{tag}.yaml")
    return space


def entity_span(space: Workspace, name: str) -> tuple[int, int]:
    row = space.database.execute(
        "SELECT line_start, line_end FROM entities WHERE name = ? AND type = 'salt_state'",
        (name,),
    ).fetchone()
    assert row is not None, name
    return int(row["line_start"]), int(row["line_end"])


def test_the_persisted_entity_range_is_the_corrected_one(tmp_path: Path) -> None:
    """The finding is user-visible provenance, so stopping at `ParsedFile` would
    prove only half of it."""
    root = _root(tmp_path, "persist", REVIEW)
    space = _workspace(tmp_path, "persist", root)
    try:
        assert entity_span(space, "install_nginx") == (3, 5)
    finally:
        space.close()


def test_every_persisted_salt_span_lies_inside_its_file(tmp_path: Path) -> None:
    root = _root(tmp_path, "bounds", DANGEROUS)
    space = _workspace(tmp_path, "bounds", root)
    try:
        assert entity_span(space, "safe_state") == (1, 3)
        assert entity_span(space, "dangerous_unrelated_state") == (8, 10)
        rows = space.database.execute(
            "SELECT line_start, line_end FROM edges WHERE line_start IS NOT NULL"
        ).fetchall()
        limit = len(DANGEROUS.splitlines())
        for row in rows:
            assert 1 <= int(row["line_start"]) <= int(row["line_end"]) <= limit, dict(row)
    finally:
        space.close()


def test_retrieval_renders_the_corrected_span(tmp_path: Path) -> None:
    from repoatlas.services.query_service import locate

    root = _root(tmp_path, "locate", DANGEROUS)
    space = _workspace(tmp_path, "locate", root)
    try:
        context = space.resolver().resolve(root, command="locate")
        evidence = locate(space, "safe_state", context)
        salt = [c for c in evidence.candidates if c.path.endswith("app.sls")]
        assert salt, [c.path for c in evidence.candidates]
        for candidate in salt:
            if candidate.line_start == 1:
                assert "rm -rf" not in candidate.text, candidate
    finally:
        space.close()


# == 12. grounding boundary ==================================================


def test_a_citation_past_the_state_span_is_not_supported(tmp_path: Path) -> None:
    """The span is what citation validation checks against.

    A model citing lines 1-10 of the dangerous fixture must not be validated by
    an entity span that used to reach that far. With the corrected span the
    supplied evidence stops at line 3, so the wider citation has nothing behind
    it.
    """
    root = _root(tmp_path, "ground", DANGEROUS)
    space = _workspace(tmp_path, "ground", root)
    try:
        start, end = entity_span(space, "safe_state")
        assert (start, end) == (1, 3)
        body = "\n".join(DANGEROUS.splitlines()[start - 1 : end])
        assert "rm -rf" not in body
        assert "{% endif %}" not in body
    finally:
        space.close()


# == 13. incremental behaviour ===============================================


def _commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", message)


def _rewrite(root: Path, sls: str) -> None:
    (root / "deployment" / "salt" / "app.sls").write_text(sls, encoding="utf-8")
    _commit(root / "deployment", "rewrite")


def _clean_snapshot(tmp_path: Path, tag: str, root: Path) -> dict[str, object]:
    space = _workspace(tmp_path, f"clean-{tag}", root)
    try:
        return fact_snapshot(space)
    finally:
        space.close()


BASE = "state_a:\n  test.nop: []\n"
APPENDED = BASE + "\n{% if condition %}\nstate_b:\n  test.nop: []\n{% endif %}\n"
WIDENED = "state_a:\n  test.nop:\n    - name: alpha\n" + APPENDED[len(BASE) :]


def test_appending_a_guarded_state_leaves_the_first_span_untouched(tmp_path: Path) -> None:
    root = _root(tmp_path, "append", BASE)
    space = _workspace(tmp_path, "append", root)
    try:
        assert entity_span(space, "state_a") == (1, 2)

        _rewrite(root, APPENDED)
        sync(space, space.resolver().resolve(root))
        assert entity_span(space, "state_a") == (1, 2)
        assert entity_span(space, "state_b") == (5, 6)

        _rewrite(root, WIDENED)
        sync(space, space.resolver().resolve(root))
        assert entity_span(space, "state_a") == (1, 3)
    finally:
        space.close()


def test_each_edit_leaves_state_equal_to_a_clean_reindex(tmp_path: Path) -> None:
    root = _root(tmp_path, "equiv", BASE)
    space = _workspace(tmp_path, "equiv", root)
    try:
        for tag, sls in (("appended", APPENDED), ("widened", WIDENED), ("base", BASE)):
            _rewrite(root, sls)
            sync(space, space.resolver().resolve(root))
            assert fact_snapshot(space) == _clean_snapshot(tmp_path, tag, root), tag
    finally:
        space.close()


def test_deleting_a_state_removes_its_span_and_its_edges(tmp_path: Path) -> None:
    source = (
        "state_a:\n  test.nop: []\n\n"
        "state_b:\n  service.running:\n    - require:\n      - pkg: state_a\n"
    )
    root = _root(tmp_path, "delete", source)
    space = _workspace(tmp_path, "delete", root)
    try:
        assert entity_span(space, "state_b") == (4, 7)

        _rewrite(root, "state_a:\n  test.nop: []\n")
        sync(space, space.resolver().resolve(root))
        rows = space.database.execute(
            "SELECT COUNT(*) n FROM entities WHERE name = 'state_b'"
        ).fetchone()
        assert int(rows["n"]) == 0
        assert entity_span(space, "state_a") == (1, 2)
        assert fact_snapshot(space) == _clean_snapshot(tmp_path, "deleted", root)
    finally:
        space.close()


def test_moving_a_state_moves_its_span(tmp_path: Path) -> None:
    first = "state_a:\n  test.nop: []\n\nstate_b:\n  test.nop: []\n"
    swapped = "state_b:\n  test.nop: []\n\nstate_a:\n  test.nop: []\n"
    root = _root(tmp_path, "move", first)
    space = _workspace(tmp_path, "move", root)
    try:
        assert entity_span(space, "state_a") == (1, 2)
        _rewrite(root, swapped)
        sync(space, space.resolver().resolve(root))
        assert entity_span(space, "state_a") == (4, 5)
        assert entity_span(space, "state_b") == (1, 2)
    finally:
        space.close()


def test_same_named_states_in_two_files_stay_distinct(tmp_path: Path) -> None:
    """The repository/file identity from an earlier pass must survive this one."""
    root = _root(tmp_path, "twin", "service:\n  test.nop: []\n")
    other = root / "deployment" / "salt" / "other.sls"
    other.write_text("\n\nservice:\n  test.nop: []\n", encoding="utf-8")
    _commit(root / "deployment", "second file")

    space = _workspace(tmp_path, "twin", root)
    try:
        rows = space.database.execute(
            "SELECT e.key AS key, e.line_start AS s, e.line_end AS e2, f.rel_path AS p "
            "FROM entities e JOIN files f ON f.id = e.file_id WHERE e.name = 'service'"
        ).fetchall()
        spans = {str(r["p"]): (int(r["s"]), int(r["e2"])) for r in rows}
        assert spans == {"salt/app.sls": (1, 2), "salt/other.sls": (3, 4)}
        assert len({str(r["key"]) for r in rows}) == 2
    finally:
        space.close()
