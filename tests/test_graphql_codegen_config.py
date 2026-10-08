"""Reading a GraphQL Code Generator configuration: one claim per output, statically.

The claim is everything the global pass needs to decide which generated declarations come
from which schema definitions, and nothing it could not use safely: a schema URL is kept
without its credentials, query or fragment, and a configuration that cannot be read
statically says so instead of being half read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from repoatlas.models.parsing import PARSE_BOUND_EXCEEDED, EdgeType, ParsedFile
from repoatlas.parsers import graphql_codegen
from repoatlas.parsers.graphql_codegen import GRAPHQL_CODEGEN_OUTPUT, redact
from repoatlas.parsers.http_facts import PENDING_FACT
from repoatlas.parsers.structured import StructuredParser

CONFIG = (
    "schema:\n"  # 1
    "  - schema/parcels.graphql\n"  # 2
    "  - 'schema/extra/**/*.graphqls'\n"  # 3
    "  - '!schema/extra/old.graphqls'\n"  # 4
    "documents: src/**/*.gql\n"  # 5
    "config:\n"  # 6
    "  enumsAsTypes: false\n"  # 7
    "  scalars:\n"  # 8
    "    Date: string\n"  # 9
    "generates:\n"  # 10
    "  src/gen/types.ts:\n"  # 11
    "    plugins:\n"  # 12
    "      - typescript:\n"  # 13
    "          namingConvention: keep\n"  # 14
    "      - typescript-operations\n"  # 15
    "      - add:\n"  # 16
    "          content: '// generated'\n"  # 17
    "  src/gen/remote.ts:\n"  # 18
    "    schema:\n"  # 19
    "      - https://user:secret@schemas.example.invalid:8443/graphql?token=abc#frag:\n"  # 20
    "          headers:\n"  # 21
    "            Authorization: Bearer abc\n"  # 22
    "      - ./build-schema.ts:\n"  # 23
    "          loader: ./loader.js\n"  # 24
    "      - introspection.json\n"  # 25
    "      - ${SCHEMA_FILE}\n"  # 26
    "    config:\n"  # 27
    "      typesPrefix: I\n"  # 28
    "    plugins: [typescript]\n"  # 29
    "  src/gen/client/:\n"  # 30
    "    preset: client\n"  # 31
)


def parse(content: str, name: str = "codegen.yml") -> ParsedFile:
    return StructuredParser().parse(Path(name), content)


def claims(parsed: ParsedFile) -> dict[str, dict[str, Any]]:
    found = {}
    for reference in parsed.references:
        if reference.meta.get(PENDING_FACT) != GRAPHQL_CODEGEN_OUTPUT:
            continue
        assert reference.edge_type is EdgeType.GENERATES
        meta: dict[str, Any] = dict(reference.meta)
        for key in ("schema", "documents", "plugins", "options"):
            meta[key] = json.loads(meta[key])
        meta["lines"] = (reference.line_start, reference.line_end)
        found[reference.target_key] = meta
    return found


def test_each_output_is_one_claim_with_the_merged_schema_and_its_own_shape() -> None:
    found = claims(parse(CONFIG))

    assert list(found) == ["src/gen/types.ts", "src/gen/remote.ts", "src/gen/client/"]
    types = found["src/gen/types.ts"]
    assert types["lines"] == (11, 17)
    assert types["schema"] == [
        {"kind": "path", "value": "schema/parcels.graphql", "line": 2, "level": "root"},
        {"kind": "glob", "value": "schema/extra/**/*.graphqls", "line": 3, "level": "root"},
        {
            "kind": "glob",
            "value": "schema/extra/old.graphqls",
            "negated": True,
            "line": 4,
            "level": "root",
        },
    ]
    assert types["documents"] == [
        {"kind": "glob", "value": "src/**/*.gql", "line": 5, "level": "root"}
    ]
    assert types["plugins"] == ["typescript", "typescript-operations", "add"]
    # Root config, then the output's, then the typescript plugin's own; only what decides
    # names and enum shape is kept.
    assert types["options"] == {"enumsAsTypes": "false", "namingConvention": "keep"}

    remote = found["src/gen/remote.ts"]
    # GraphQL Code Generator merges an output's schema with the root's: both are listed.
    assert [(s["kind"], s["level"], s["line"]) for s in remote["schema"]] == [
        ("path", "root", 2),
        ("glob", "root", 3),
        ("glob", "root", 4),
        ("url", "output", 20),
        ("code", "output", 23),
        ("introspection", "output", 25),
        ("computed", "output", 26),
    ]
    assert remote["options"] == {"enumsAsTypes": "false", "typesPrefix": "I"}
    assert found["src/gen/client/"]["preset"] == "client"


def test_a_schema_url_is_kept_without_anything_that_could_authenticate_it() -> None:
    parsed = parse(CONFIG)
    remote = claims(parsed)["src/gen/remote.ts"]

    assert remote["schema"][3]["value"] == "https://schemas.example.invalid:8443/graphql"
    generation = json.dumps(
        [r.meta for r in parsed.references if r.meta.get(PENDING_FACT) == GRAPHQL_CODEGEN_OUTPUT]
    )
    assert "secret" not in generation and "token=abc" not in generation
    assert "#frag" not in generation and "Bearer" not in generation


@pytest.mark.parametrize(
    ("written", "kind"),
    [
        ("schema/a.graphql", "path"),
        ("schema/*.graphql", "glob"),
        ("schema/@(a|b).graphql", "glob"),
        ("schema/+(a).gql", "glob"),
        ("!schema/old.graphql", "glob"),
        ("schema.json", "introspection"),
        ("build.ts", "code"),
        ("${SCHEMA}", "computed"),
        ("schema.prisma", "other"),
    ],
)
def test_each_input_is_classified_as_written(written: str, kind: str) -> None:
    [claim] = claims(
        parse(f"schema: '{written}'\ngenerates:\n  out.ts:\n    plugins: [typescript]\n")
    ).values()

    assert claim["schema"][0]["kind"] == kind


@pytest.mark.parametrize(
    ("url", "kept"),
    [
        ("https://a.example.invalid/graphql", "https://a.example.invalid/graphql"),
        ("http://u:p@a.example.invalid/g?x=1#y", "http://a.example.invalid/g"),
        ("https://[::1]:4000/graphql?k=v", "https://[::1]:4000/graphql"),
        ("https://a.example.invalid:notaport/g?k=v", "https://a.example.invalid:notaport/g"),
        ("https://[::1/graphql?k=v", "https://"),
    ],
)
def test_redaction(url: str, kept: str) -> None:
    assert redact(url) == kept


def test_a_list_of_plugins_as_the_whole_output_is_read_and_json_works_too() -> None:
    content = json.dumps({"schema": "s.graphql", "generates": {"out.ts": ["typescript"]}}, indent=2)
    found = claims(parse(content, "codegen.json"))

    assert found["out.ts"]["plugins"] == ["typescript"]
    assert found["out.ts"]["schema"][0]["value"] == "s.graphql"


@pytest.mark.parametrize(
    ("content", "refused"),
    [
        (
            "base: &base\n  schema: s.graphql\ngenerates:\n  out.ts:\n    <<: *base\n"
            "    plugins: [typescript]\n",
            "yaml-merge",
        ),
        (
            "schema: s.graphql\ngenerates:\n  ${OUT}/types.ts:\n    plugins: [typescript]\n",
            "computed-output",
        ),
    ],
    ids=["merge-key", "computed-output"],
)
def test_what_cannot_be_read_statically_is_refused_by_name(content: str, refused: str) -> None:
    [claim] = claims(parse(content)).values()

    assert claim["refused"] == refused


def test_an_oversized_configuration_is_refused_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graphql_codegen, "MAX_ENTRIES", 2)

    parsed = parse(CONFIG)

    assert claims(parsed) == {}
    assert [d.message for d in parsed.diagnostics] == [PARSE_BOUND_EXCEEDED]
    # The configuration's keys are still indexed like any YAML file's.
    assert any(s.name == "generates" for s in parsed.symbols)


def test_a_configuration_over_the_byte_bound_is_refused_whole() -> None:
    padding = "# " + "x" * 80 + "\n"
    content = CONFIG + padding * (graphql_codegen.MAX_BYTES // len(padding) + 1)

    parsed = parse(content)

    assert claims(parsed) == {}
    assert [d.message for d in parsed.diagnostics] == [PARSE_BOUND_EXCEEDED]


@pytest.mark.parametrize("name", ["config.yml", "codegen.config.yml", "apollo.yaml"])
def test_only_a_codegen_configuration_is_read_as_one(name: str) -> None:
    assert claims(parse(CONFIG, name)) == {}
