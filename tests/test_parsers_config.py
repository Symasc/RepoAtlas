"""Salt, Jinja, structured config and Dockerfile parsers (V1_SPEC §7.5-§7.8, §19)."""

from __future__ import annotations

from pathlib import Path

from repoatlas.models.parsing import EdgeType, EntityType, ParsedFile, Severity
from repoatlas.parsers.base import LanguageParser
from repoatlas.parsers.dockerfile import DockerfileParser
from repoatlas.parsers.jinja import JinjaParser, neutralize
from repoatlas.parsers.salt import SaltMasterParser, SaltParser
from repoatlas.parsers.structured import StructuredParser


def references(parsed: ParsedFile, edge: EdgeType) -> list[str]:
    return [r.target_key for r in parsed.references if r.edge_type is edge]


def qualified(parsed: ParsedFile, kind: EntityType | None = None) -> list[str]:
    return [s.qualified_name for s in parsed.symbols if kind is None or s.kind is kind]


# --- Jinja neutralization ----------------------------------------------------


def test_neutralize_preserves_line_and_column_positions() -> None:
    """Line ranges reported for Salt facts must match the original file exactly."""
    source = "a: {{ pillar['x'] }}\nb: {% if y %}1{% endif %}\nc: {# note #}2\n"
    result = neutralize(source)
    assert len(result) == len(source)
    assert result.count("\n") == source.count("\n")
    for original, neutralized in zip(source.splitlines(), result.splitlines(), strict=True):
        assert len(original) == len(neutralized)
    assert "{{" not in result and "{%" not in result and "{#" not in result


def test_neutralized_expressions_survive_as_yaml_scalars() -> None:
    import yaml

    document = yaml.safe_load(neutralize("host: {{ pillar['service']['host'] }}\n"))
    assert set(document) == {"host"}


# --- Jinja parser ------------------------------------------------------------

JINJA_SOURCE = """\
{% extends "base.jinja" %}
{% include "parts/header.jinja" %}
{% from "macros.jinja" import row %}
host = {{ pillar['service']['host'] }}
port = {{ salt['pillar.get']('service:port') }}
os = {{ grains.get('os') }}
"""


def test_jinja_parser_satisfies_the_protocol() -> None:
    assert isinstance(JinjaParser(), LanguageParser)


def test_jinja_extracts_template_relationships() -> None:
    parsed = JinjaParser().parse(Path("t.jinja"), JINJA_SOURCE)
    assert set(references(parsed, EdgeType.INCLUDES)) == {"base.jinja", "parts/header.jinja"}
    assert references(parsed, EdgeType.IMPORTS) == ["macros.jinja"]


def test_jinja_normalizes_pillar_and_grain_access_to_one_key_shape() -> None:
    """Index and ``get`` syntax must produce the same key, or they cannot join."""
    parsed = JinjaParser().parse(Path("t.jinja"), JINJA_SOURCE)
    assert set(references(parsed, EdgeType.READS)) == {
        "pillar:service:host",
        "pillar:service:port",
        "grains:os",
    }


def test_jinja_records_salt_execution_calls() -> None:
    parsed = JinjaParser().parse(Path("t.jinja"), JINJA_SOURCE)
    assert references(parsed, EdgeType.CALLS) == ["salt:pillar.get"]


def test_jinja_ignores_a_non_literal_include() -> None:
    parsed = JinjaParser().parse(Path("t.jinja"), "{% include chosen_template %}\n")
    assert parsed.references == ()


# --- Salt --------------------------------------------------------------------

SALT_SOURCE = """\
include:
  - service.pillar

service_config:
  file.managed:
    - name: /etc/service/service.conf
    - source: salt://service/files/service.conf.jinja
    - template: jinja
    - context:
        SERVICE_HOST: {{ pillar['service']['host'] }}
        lowercase_key: ignored
    - require:
      - pkg: service_package
    - watch_in:
      - service: service_running

service_package:
  pkg.installed:
    - name: service
"""


def parse_salt(source: str = SALT_SOURCE, name: str = "service/init.sls") -> ParsedFile:
    return SaltParser().parse(Path(name), source)


def test_salt_parser_satisfies_the_protocol() -> None:
    assert isinstance(SaltParser(), LanguageParser)


def test_salt_extracts_state_ids_with_their_functions() -> None:
    parsed = parse_salt()
    states = {s.name: s.meta.get("functions") for s in parsed.symbols}
    assert states == {"service_config": "file.managed", "service_package": "pkg.installed"}


def test_salt_extracts_includes_and_requisites() -> None:
    parsed = parse_salt()
    assert references(parsed, EdgeType.INCLUDES) == ["service.pillar"]
    assert references(parsed, EdgeType.REQUIRES) == ["service_package"]
    assert references(parsed, EdgeType.WATCHES) == ["service_running"]


def test_salt_records_source_and_state_function() -> None:
    parsed = parse_salt()
    referenced = references(parsed, EdgeType.REFERENCES)
    assert "salt://service/files/service.conf.jinja" in referenced
    assert "salt.state:file.managed" in referenced


def test_salt_treats_upper_case_context_keys_as_configured_variables() -> None:
    """The narrow trigger for CONFIGURES: an ALL_CAPS key inside a context block."""
    parsed = parse_salt()
    configures = [r for r in parsed.references if r.edge_type is EdgeType.CONFIGURES]
    assert [r.target_key for r in configures] == ["SERVICE_HOST"]
    assert configures[0].line_start == 10
    assert configures[0].meta["block"] == "context"


def test_salt_extracts_jinja_facts_from_the_raw_text() -> None:
    parsed = parse_salt()
    assert "pillar:service:host" in references(parsed, EdgeType.READS)


def test_salt_top_file_records_environment_target_and_sls_mapping() -> None:
    parsed = parse_salt(
        "prod:\n  web01:\n    - service.api\n",
        "top.sls",
    )

    [mapping] = [
        reference
        for reference in parsed.references
        if reference.meta.get("salt_fact") == "top_entry"
    ]
    assert mapping.edge_type is EdgeType.CONFIGURES
    assert mapping.target_kind is EntityType.CONFIG
    assert mapping.target_key == "salt-top:prod:glob:web01:service.api"
    assert mapping.line_start == 3
    assert mapping.meta["environment"] == "prod"
    assert mapping.meta["selector"] == "web01"
    assert mapping.meta["matcher"] == "glob"
    assert mapping.meta["sls"] == "service.api"


def test_salt_master_records_state_and_pillar_roots_by_environment() -> None:
    parsed = SaltMasterParser().parse(
        Path("master"),
        "file_roots:\n  prod:\n    - /srv/salt\npillar_roots:\n  prod:\n    - /srv/pillar\n",
    )

    roots = [reference for reference in parsed.references if reference.meta.get("salt_fact")]
    assert [
        (root.meta["role"], root.meta["environment"], root.meta["salt_path"], root.line_start)
        for root in roots
    ] == [("state", "prod", "/srv/salt", 3), ("pillar", "prod", "/srv/pillar", 6)]


def test_salt_indexes_pillar_data_as_configuration_keys() -> None:
    """A data-only ``.sls`` has no state functions, so its keys are config keys."""
    parsed = parse_salt("service:\n  host: example\n  port: 8080\n", "service/pillar.sls")
    assert qualified(parsed, EntityType.CONFIG) == ["service", "service.host", "service.port"]
    assert not qualified(parsed, EntityType.SALT_STATE)


def test_salt_does_not_index_state_arguments_as_configuration_keys() -> None:
    parsed = parse_salt()
    assert not qualified(parsed, EntityType.CONFIG)


def test_salt_falls_back_to_a_line_scan_when_yaml_fails() -> None:
    broken = (
        "{% for host in pillar['hosts'] %}\n"
        "deploy_{{ host }}:\n"
        "  file.managed:\n"
        "\t- bad: [unclosed\n"
        "{% endfor %}\n"
        "standalone_state:\n"
        "  service.running:\n"
        "    - require:\n"
        "      - pkg: my_package\n"
    )
    parsed = parse_salt(broken, "broken.sls")
    assert parsed.diagnostics
    assert parsed.diagnostics[0].severity is Severity.WARNING
    assert "standalone_state" in [s.name for s in parsed.symbols]
    requisite = next(r for r in parsed.references if r.edge_type is EdgeType.REQUIRES)
    assert requisite.target_key == "my_package"
    assert requisite.confidence < 1.0
    assert requisite.meta["recovery"] == "line-scan"


def test_salt_fallback_skips_templated_state_ids() -> None:
    """A templated ID names nothing another file could reference."""
    parsed = parse_salt("deploy_{{ host }}:\n  file.managed:\n\t- bad: [\n", "b.sls")
    assert [s.name for s in parsed.symbols] == []


# --- Structured configuration ------------------------------------------------


def test_structured_parser_satisfies_the_protocol() -> None:
    assert isinstance(StructuredParser(), LanguageParser)


def test_yaml_keys_carry_dotted_paths_and_line_provenance() -> None:
    parsed = StructuredParser().parse(
        Path("config.yaml"), "service:\n  host: example\n  retries: 3\n"
    )
    assert qualified(parsed) == ["service", "service.host", "service.retries"]
    lines = {s.qualified_name: s.line_start for s in parsed.symbols}
    assert lines["service.retries"] == 3


def test_json_is_parsed_and_tab_indentation_is_tolerated() -> None:
    parsed = StructuredParser().parse(
        Path("package.json"), '{\n\t"name": "x",\n\t"scripts": {\n\t\t"build": "tsc"\n\t}\n}\n'
    )
    assert parsed.language == "JSON"
    assert "scripts.build" in qualified(parsed)


def test_lockfiles_are_left_to_the_dependency_layer() -> None:
    parsed = StructuredParser().parse(Path("package-lock.json"), '{"name": "x"}')
    assert parsed.symbols == ()


def test_malformed_yaml_yields_a_diagnostic_not_an_exception() -> None:
    parsed = StructuredParser().parse(Path("bad.yaml"), "a: [1\nb: }\n")
    assert parsed.symbols == ()
    assert parsed.diagnostics[0].severity is Severity.WARNING


# --- Dockerfile --------------------------------------------------------------

DOCKERFILE_SOURCE = """\
# syntax=docker/dockerfile:1
FROM python:3.13-slim AS builder

WORKDIR /app
ARG VERSION
ENV SERVICE_HOST=localhost SERVICE_PORT=8080

RUN apt-get update \\
 && apt-get install -y curl \\
 && pip install --no-cache-dir requests flask

COPY src/ /app/src/
ENTRYPOINT ["python3", "/app/src/app.py"]

FROM builder
CMD ["--serve"]
"""


def parse_docker(source: str = DOCKERFILE_SOURCE) -> ParsedFile:
    return DockerfileParser().parse(Path("Dockerfile"), source)


def test_dockerfile_parser_satisfies_the_protocol() -> None:
    assert isinstance(DockerfileParser(), LanguageParser)


def test_dockerfile_extracts_stages_with_their_base_image() -> None:
    parsed = parse_docker()
    stages = {s.name: s.meta.get("image") for s in parsed.symbols if s.meta.get("stage")}
    assert stages == {"builder": "python:3.13-slim", "stage2": "builder"}


def test_dockerfile_records_env_and_arg_as_exports() -> None:
    parsed = parse_docker()
    assert set(references(parsed, EdgeType.EXPORTS)) == {
        "VERSION",
        "SERVICE_HOST",
        "SERVICE_PORT",
    }


def test_dockerfile_joins_continuation_lines_and_keeps_the_true_line_number() -> None:
    parsed = parse_docker()
    pip = next(r for r in parsed.references if r.target_key == "requests")
    assert pip.line_start == 8
    assert pip.line_end == 10


def test_dockerfile_records_installed_packages_per_installer() -> None:
    parsed = parse_docker()
    installed = {
        r.target_key: r.meta["ecosystem"]
        for r in parsed.references
        if r.edge_type is EdgeType.DEPENDS_ON
    }
    assert installed["curl"] == "apt"
    assert installed["requests"] == "pypi"
    assert installed["flask"] == "pypi"
    assert installed["python:3.13-slim"] == "docker"


def test_dockerfile_treats_a_stage_base_as_a_reference_not_an_image() -> None:
    parsed = parse_docker()
    stage_reference = next(r for r in parsed.references if r.meta.get("role") == "stage")
    assert stage_reference.target_key == "builder"
    assert stage_reference.edge_type is EdgeType.REFERENCES


def test_dockerfile_records_copy_sources_and_entrypoint() -> None:
    parsed = parse_docker()
    assert "src/" in references(parsed, EdgeType.REFERENCES)
    assert "python3" in references(parsed, EdgeType.EXECUTES)
