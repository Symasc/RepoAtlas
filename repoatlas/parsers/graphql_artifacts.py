"""Where a checked-in GraphQL schema copy comes from: the commands and scripts that fetch it.

A schema file a repository did not write -- a supergraph downloaded from a registry, an
introspection result saved from an endpoint -- is an artifact. What fetches it is
evidence of that, and of nothing more: a fetch says where a copy came from, never who
provides what is in it (that is :mod:`repoatlas.graph.graphql_ownership`'s, from the
copy's own composition directives).

Two sources, both read without running anything:

* a ``package.json`` script whose command runs a schema fetch -- ``hive artifact:fetch``,
  ``rover ... fetch``, ``apollo client:download-schema``, ``get-graphql-schema``,
  ``graphql-inspector introspect``, or ``curl``/``wget`` into a schema file -- with the
  file it writes and the URL it names;
* a Python module that both makes an HTTP request (``requests``, ``httpx``,
  ``urllib.request``) and writes a schema file at a path anchored to the module
  (:mod:`repoatlas.parsers.py_graphql`).

A schema file is SDL (``.graphql``, ``.gql``, ``.graphqls``), or JSON when what writes it
asks an introspection query: a download of any other JSON is not a schema fetch.

Each is one ``graphql-artifact`` claim. A URL is kept without user information, query or
fragment.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import PurePosixPath

import yaml

from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.graphql_codegen import redact
from repoatlas.parsers.http_facts import pending_reference

GRAPHQL_ARTIFACT = "graphql-artifact"

SDL_SUFFIXES = (".graphql", ".gql", ".graphqls")
#: Text that marks a request as an introspection query, whose JSON answer is a schema.
INTROSPECTION_MARKERS = ("__schema", "IntrospectionQuery", "introspection_query")
#: Flags that name the file a fetch writes.
_OUTPUT_FLAGS = frozenset({"--outputFile", "--output-file", "--output", "-o", "-O", "--write"})
_SEPARATORS = frozenset({"&&", "||", ";", "|"})


def package_script_references(
    path: PurePosixPath, documents: Sequence[yaml.Node | None]
) -> list[ParsedReference]:
    """The scripts of a ``package.json`` that fetch a schema."""
    if (
        path.name != "package.json"
        or not documents
        or not isinstance(documents[0], yaml.MappingNode)
    ):
        return []
    fields = {str(k.value): v for k, v in documents[0].value if isinstance(k, yaml.ScalarNode)}
    scripts = fields.get("scripts")
    if not isinstance(scripts, yaml.MappingNode):
        return []
    found: list[ParsedReference] = []
    for key, value in scripts.value:
        if not (isinstance(key, yaml.ScalarNode) and isinstance(value, yaml.ScalarNode)):
            continue
        fetch = schema_fetch(str(value.value))
        if fetch is None:
            continue
        line = key.start_mark.line + 1
        found.append(
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.FILE,
                str(key.value),
                line,
                value.end_mark.line + 1,
                role=GRAPHQL_ARTIFACT,
                source="package-script",
                script=str(key.value),
                **fetch,
            )
        )
    return found


def schema_fetch(command: str) -> dict[str, str] | None:
    """What one shell command fetches: its tool, the file it writes and the URL it names."""
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return None
    for part in _commands(words):
        fetch = _fetch(part)
        if fetch is not None:
            return fetch
    return None


def _commands(words: list[str]) -> list[list[str]]:
    commands: list[list[str]] = [[]]
    for word in words:
        if word in _SEPARATORS:
            commands.append([])
        else:
            commands[-1].append(word)
    return [c for c in commands if c]


def _fetch(words: list[str]) -> dict[str, str] | None:
    program = PurePosixPath(words[0]).name
    rest = words[1:]
    if program in {"npx", "pnpm", "yarn", "bunx"} and rest:
        if program in {"pnpm", "yarn"} and rest[0] in {"exec", "dlx"}:
            rest = rest[1:]
        if not rest:
            return None
        program, rest = PurePosixPath(rest[0]).name, rest[1:]
    tool: str | None = None
    if program == "hive" and rest[:1] == ["artifact:fetch"]:
        tool = "hive artifact:fetch"
    elif program == "rover" and len(rest) >= 2 and rest[1] == "fetch":
        tool = f"rover {rest[0]} fetch"
    elif (
        program == "apollo"
        and rest[:1]
        and rest[0] in {"client:download-schema", "service:download"}
    ):
        tool = f"apollo {rest[0]}"
    elif program == "get-graphql-schema":
        tool = program
    elif program == "graphql-inspector" and rest[:1] == ["introspect"]:
        tool = "graphql-inspector introspect"
    elif program in {"curl", "wget"}:
        tool = program
    if tool is None:
        return None
    output = _output(rest)
    url = next((w for w in rest if w.lower().startswith(("http://", "https://"))), None)
    if program in {"curl", "wget"} and (
        output is None
        or not schema_output(output, introspects=any(introspects(word) for word in rest))
    ):
        return None
    if program == "apollo" and output is None:
        positional = [w for w in rest[1:] if not w.startswith("-")]
        output = positional[-1] if positional else None
    fetch = {"tool": tool}
    if output is not None:
        fetch["output"] = output
    if url is not None:
        fetch["url"] = redact(url)
    return fetch


def schema_output(output: str, *, introspects: bool) -> bool:
    """Whether a written file is a schema: SDL, or JSON answering an introspection query."""
    lowered = output.lower()
    return lowered.endswith(SDL_SUFFIXES) or (introspects and lowered.endswith(".json"))


def introspects(text: str) -> bool:
    """Whether text asks an introspection query."""
    return any(marker in text for marker in INTROSPECTION_MARKERS)


def _output(words: list[str]) -> str | None:
    for index, word in enumerate(words):
        if word in _OUTPUT_FLAGS and index + 1 < len(words):
            return words[index + 1]
        for flag in _OUTPUT_FLAGS:
            if flag.startswith("--") and word.startswith(f"{flag}="):
                return word.split("=", 1)[1]
        if word == ">" and index + 1 < len(words):
            return words[index + 1]
        if word.startswith(">") and len(word) > 1:
            return word[1:]
    return None
