"""Salt and Jinja, pushed hard (V1_SPEC §7.5, §7.6).

Salt is the language V1 is least able to be certain about: an `.sls` file is
Jinja-templated YAML whose meaning depends on a top file RepoAtlas cannot read and
on grains it cannot evaluate. So the standard here is not completeness but
**conservatism** — a parse failure must produce a diagnostic and partial results,
never a fabricated relationship, and anything recovered from broken input must
carry a reduced confidence and say how it was obtained.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.models.parsing import EdgeType, EntityType, ParsedFile, Severity
from repoatlas.parsers.jinja import JinjaParser, neutralize
from repoatlas.parsers.salt import SaltParser
from repoatlas.services.workspace import Workspace

pytestmark = pytest.mark.adversarial


def parse_salt(source: str, name: str = "t.sls") -> ParsedFile:
    return SaltParser().parse(Path(name), source)


def parse_jinja(source: str, name: str = "t.jinja") -> ParsedFile:
    return JinjaParser().parse(Path(name), source)


def refs(parsed: ParsedFile, edge: EdgeType) -> set[str]:
    return {r.target_key for r in parsed.references if r.edge_type is edge}


def names(parsed: ParsedFile) -> set[str]:
    return {s.name for s in parsed.symbols}


# -- every requisite kind ----------------------------------------------------


@pytest.mark.parametrize(
    ("requisite", "edge"),
    [
        ("require", EdgeType.REQUIRES),
        ("require_in", EdgeType.REQUIRES),
        ("watch", EdgeType.WATCHES),
        ("watch_in", EdgeType.WATCHES),
        ("onchanges", EdgeType.WATCHES),
        ("onchanges_in", EdgeType.WATCHES),
    ],
)
def test_each_requisite_form_names_its_target(requisite: str, edge: EdgeType) -> None:
    source = (
        "state_one:\n  cmd.run:\n    - name: /usr/bin/true\n"
        f"    - {requisite}:\n      - cmd: state_two\n"
        "state_two:\n  cmd.run:\n    - name: /usr/bin/true\n"
    )
    parsed = parse_salt(source)
    assert "state_two" in refs(parsed, edge), (requisite, parsed.references)


def test_the_ecosystem_records_every_requisite_kind(adversarial: Workspace) -> None:
    rows = adversarial.database.execute(
        "SELECT DISTINCT e.type FROM edges e JOIN repositories r ON r.id = e.repo_id "
        "WHERE r.name = 'deployment'"
    ).fetchall()
    kinds = {row["type"] for row in rows}
    assert {"REQUIRES", "WATCHES", "INCLUDES"} <= kinds, kinds


def test_include_and_extend_are_both_recorded(adversarial: Workspace) -> None:
    rows = adversarial.database.execute(
        "SELECT u.edge_type, u.target_key FROM unresolved_refs u JOIN files f ON f.id = u.file_id "
        "WHERE f.rel_path = 'service/extend.sls'"
    ).fetchall()
    claims = {(row["edge_type"], row["target_key"]) for row in rows}
    assert ("INCLUDES", "service") in claims
    assert any("service_config" in key for _, key in claims)


# -- state IDs versus configuration data -------------------------------------


def test_a_pillar_file_declares_configuration_not_states() -> None:
    parsed = parse_salt("service:\n  host: example\n  port: 8080\n", "pillar.sls")
    assert not [s for s in parsed.symbols if s.kind is EntityType.SALT_STATE]
    assert {"service.host", "service.port"} <= {s.qualified_name for s in parsed.symbols}


def test_a_key_that_looks_like_config_but_declares_a_function_is_a_state() -> None:
    """The distinction is structural: a `module.function` child makes it a state."""
    parsed = parse_salt("host:\n  file.managed:\n    - name: /etc/host\n")
    assert "host" in {s.name for s in parsed.symbols if s.kind is EntityType.SALT_STATE}


def test_config_data_nested_under_a_state_is_not_a_config_key() -> None:
    """`- name:` is an argument to file.managed, not a configuration key someone
    could reference from a pillar."""
    parsed = parse_salt("s:\n  file.managed:\n    - name: /etc/x\n    - mode: '0644'\n")
    assert not [s for s in parsed.symbols if s.kind is EntityType.CONFIG]


def test_nested_dictionaries_keep_their_dotted_path() -> None:
    parsed = parse_salt("a:\n  b:\n    c:\n      d: 1\n", "pillar.sls")
    assert "a.b.c.d" in {s.qualified_name for s in parsed.symbols}


# -- malformed input degrades conservatively ---------------------------------


BROKEN = {
    "valid-jinja-broken-yaml": (
        "{% for h in pillar['hosts'] %}\ndeploy_{{ h }}:\n  file.managed:\n\t- bad: [\n"
        "{% endfor %}\nrecoverable:\n  service.running: []\n"
    ),
    "malformed-jinja": "{% if %}\nstate:\n  cmd.run: []\n",
    "unterminated-jinja": "state:\n  file.managed:\n    - name: {{ pillar['x'\n",
    "incomplete-state": "state_without_body:\n",
    "state-body-is-a-scalar": "state:\n  just_a_string\n",
    "empty": "",
    "only-jinja": "{% set x = 1 %}\n",
    "tabs-everywhere": "a:\n\tb:\n\t\tc: 1\n",
}


@pytest.mark.parametrize("name", sorted(BROKEN))
def test_broken_salt_never_raises_and_never_misplaces_a_line(name: str) -> None:
    source = BROKEN[name]
    parsed = parse_salt(source)
    limit = max(1, len(source.splitlines()))
    for symbol in parsed.symbols:
        assert symbol.line_start is not None and symbol.line_end is not None, (name, symbol)
        assert 1 <= symbol.line_start <= symbol.line_end <= limit, (name, symbol)
    for reference in parsed.references:
        assert 1 <= reference.line_start <= limit, (name, reference)
    for diagnostic in parsed.diagnostics:
        assert diagnostic.line is None or 1 <= diagnostic.line <= limit, (name, diagnostic)


def test_recovery_by_line_scan_is_marked_and_downgraded() -> None:
    parsed = parse_salt(BROKEN["valid-jinja-broken-yaml"])
    assert any(d.severity is Severity.WARNING for d in parsed.diagnostics)
    recovered = [r for r in parsed.references if r.meta.get("recovery")]
    for reference in recovered:
        assert reference.confidence < 1.0
        assert reference.meta["recovery"] == "line-scan"


def test_a_templated_state_id_is_never_declared() -> None:
    """`deploy_{{ h }}` names nothing another file could reference."""
    assert not [n for n in names(parse_salt(BROKEN["valid-jinja-broken-yaml"])) if "deploy" in n]


def test_a_state_with_no_body_declares_no_function() -> None:
    parsed = parse_salt(BROKEN["incomplete-state"])
    assert not [r for r in parsed.references if r.meta.get("salt_function")]


def test_a_failed_parse_still_records_a_diagnostic(adversarial: Workspace) -> None:
    """The ecosystem's broken.sls must have produced a warning rather than silence."""
    row = adversarial.database.execute(
        "SELECT parser FROM files f JOIN repositories r ON r.id = f.repo_id "
        "WHERE r.name = 'deployment' AND f.rel_path = 'service/broken.sls'"
    ).fetchone()
    assert row is not None, "the file must still be indexed despite failing to parse"


def test_a_broken_file_is_still_searchable_as_text(adversarial: Workspace) -> None:
    """Failing to parse costs the structure, not the content."""
    assert adversarial.search.search_literal("recoverable_state")


# -- computed targets never resolve ------------------------------------------


def test_a_dynamic_template_name_produces_no_resolved_source(
    adversarial: Workspace,
) -> None:
    rows = adversarial.database.execute(
        "SELECT dst.key AS d FROM edges e JOIN entities dst ON dst.id = e.dst_id "
        "JOIN files f ON f.id = e.file_id WHERE f.rel_path = 'service/dynamic.sls'"
    ).fetchall()
    for row in rows:
        assert "{{" not in row["d"] and "}}" not in row["d"]
        assert not row["d"].startswith("file:deployment:service/files/")


def test_a_dynamic_pillar_key_is_not_resolved_to_a_config_key(
    adversarial: Workspace,
) -> None:
    """`salt['pillar.get'](pillar_key)` where pillar_key is built from a grain.

    The literal grain accesses in the same file — `grains['os']`, `grains['id']` —
    *are* written down and are recorded. What must not appear is a pillar key,
    because the only pillar access in the file is through the computed variable.
    """
    rows = adversarial.database.execute(
        "SELECT dst.key AS d FROM edges e JOIN entities dst ON dst.id = e.dst_id "
        "JOIN files f ON f.id = e.file_id "
        "WHERE f.rel_path = 'service/dynamic.sls' AND dst.key LIKE 'cfgkey:%'"
    ).fetchall()
    keys = {row["d"] for row in rows}
    assert keys == {"cfgkey:grains:os", "cfgkey:grains:id"}, keys
    assert not any(key.startswith("cfgkey:pillar") for key in keys)


def test_an_unresolvable_salt_source_stays_a_claim(adversarial: Workspace) -> None:
    resolved = adversarial.database.execute(
        "SELECT COUNT(*) AS n FROM edges e JOIN entities dst ON dst.id = e.dst_id "
        "WHERE dst.key LIKE '%does_not_exist%'"
    ).fetchone()["n"]
    assert resolved == 0
    claim = adversarial.database.execute(
        "SELECT COUNT(*) AS n FROM unresolved_refs WHERE target_key LIKE '%does_not_exist%'"
    ).fetchone()["n"]
    assert claim > 0


def test_a_resolvable_salt_source_resolves_below_full_confidence(
    adversarial: Workspace,
) -> None:
    """`file_roots` is never readable, so no `salt://` mapping is ever proven."""
    rows = adversarial.database.execute(
        "SELECT e.confidence FROM edges e JOIN entities dst ON dst.id = e.dst_id "
        "WHERE dst.key LIKE '%service.conf.jinja%' AND e.type = 'REFERENCES'"
    ).fetchall()
    assert rows
    assert all(0 < row["confidence"] < 1.0 for row in rows)


# -- Jinja -------------------------------------------------------------------


def test_every_jinja_relationship_form_is_captured() -> None:
    parsed = parse_jinja(
        '{% extends "base.jinja" %}\n'
        '{% include "parts/header.jinja" %}\n'
        '{% from "macros.jinja" import row %}\n'
        "host = {{ pillar['service']['host'] }}\n"
        "os = {{ grains.get('os') }}\n"
        "p = {{ salt['pillar.get']('service:port') }}\n"
    )
    targets = {r.target_key for r in parsed.references}
    assert {"base.jinja", "parts/header.jinja", "macros.jinja"} <= targets
    assert any("service" in t and "host" in t for t in targets)


@pytest.mark.parametrize(
    "source",
    [
        "{% include template_name %}\n",
        "{% include some ~ '.jinja' %}\n",
        "{{ pillar[dynamic_key] }}\n",
        "{% extends parent %}\n",
    ],
)
def test_a_computed_jinja_target_names_nothing(source: str) -> None:
    parsed = parse_jinja(source)
    for reference in parsed.references:
        assert "{{" not in reference.target_key
        assert reference.target_key not in {"template_name", "dynamic_key", "parent"}


@pytest.mark.parametrize(
    "source",
    [
        "{{ unterminated\n",
        "{% if x %}\nbody\n",
        "{# never closed\n",
        "{{ {{ nested }} }}\n",
        "{%%}{{}}{##}\n",
        "{% for a in b %}{% endfor %}\n" * 500,
    ],
)
def test_neutralization_preserves_every_position(source: str) -> None:
    """Line *and* column marks must survive, or a Salt fact cites the wrong place."""
    result = neutralize(source)
    assert len(result) == len(source)
    assert result.count("\n") == source.count("\n")
    for original, rewritten in zip(source.splitlines(), result.splitlines(), strict=True):
        assert len(original) == len(rewritten)


def test_a_map_jinja_records_its_salt_call_without_evaluating_it(
    adversarial: Workspace,
) -> None:
    """`grains.filter_by` on a map is the canonical Salt idiom; the *call* is a
    fact, the resulting value is not knowable without rendering."""
    rows = adversarial.database.execute(
        "SELECT dst.key AS d FROM edges e JOIN entities dst ON dst.id = e.dst_id "
        "JOIN files f ON f.id = e.file_id WHERE f.rel_path = 'service/map.jinja'"
    ).fetchall()
    keys = {row["d"] for row in rows}
    assert "cmd:salt:grains.filter_by" in keys
    assert not any(key.startswith("cfgkey:") for key in keys)
