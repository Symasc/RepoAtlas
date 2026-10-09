"""docker-compose services as claims for a deployment identity (HTTP linkage).

A compose file names services that other services reach by name, inside the network
compose creates: ``http://<service>:<container port>``. That address is an identity --
it is where requests addressed to it go -- but a compose file alone does not say which
code answers. So each service is recorded as a claim for the global pass
(:mod:`repoatlas.graph.deploy_identity`), which ties it to code only through the image
the service builds: its Dockerfile's command names the module that runs.

For a service under ``services:`` with a literal key, the claim records:

* its **names**: the key, ``container_name``, ``hostname``, and the aliases of each
  network it joins in the mapping form (``networks: {net: {aliases: [...]}}``);
* its **container ports**: ``expose`` entries, the container side of each ``ports``
  entry (short ``[ip:][host:]container[/proto]``, or long ``{target: …}``); a range is
  not read;
* its **build**: ``build: <dir>`` or ``build: {context, dockerfile}``, resolved against
  the compose file's directory to repository paths (``dockerfile`` against the
  context, ``Dockerfile`` by default); a path leaving the repository is not read;
* ``command`` and ``entrypoint``, which replace the image's ``CMD`` and ``ENTRYPOINT``:
  a list, or a string split on whitespace; and ``image``;
* its ``environment``: each variable's **name** and line, in the mapping or the list
  form -- never a value (DF1_PHASE_A.md §7, §11).

Compose interpolates ``${VAR}`` and ``$VAR`` in values from the environment it runs in,
so a value holding one is not read: that field is left out, never guessed.

Parsed, never run (V1_SPEC §17).
"""

from __future__ import annotations

import json
import posixpath
import re
from pathlib import PurePosixPath

import yaml

from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.http_facts import COMPOSE_SERVICE, pending_reference

_COMPOSE_NAMES = re.compile(r"(docker-)?compose([.-][\w.-]+)?\.ya?ml")
_INTERPOLATION = re.compile(r"\$")
_PORT = re.compile(r"\d{1,5}")
_NULL_TAG = "tag:yaml.org,2002:null"


def is_compose_file(path: PurePosixPath) -> bool:
    """``docker-compose.yml``, ``docker-compose.prod.yaml``, ``compose.yml`` and the like."""
    return _COMPOSE_NAMES.fullmatch(path.name) is not None


def _mapping(node: yaml.Node | None) -> dict[str, tuple[yaml.Node, yaml.Node]] | None:
    if not isinstance(node, yaml.MappingNode):
        return None
    found: dict[str, tuple[yaml.Node, yaml.Node]] = {}
    for key, value in node.value:
        if isinstance(key, yaml.ScalarNode):
            found[str(key.value)] = (key, value)
    return found


def _scalar(node: yaml.Node | None) -> str | None:
    """A literal scalar's text, or ``None`` for anything else -- and for interpolation."""
    if not isinstance(node, yaml.ScalarNode) or node.tag == _NULL_TAG:
        return None
    text = str(node.value)
    return None if _INTERPOLATION.search(text) else text


def _strings(node: yaml.Node | None) -> list[str] | None:
    """A list of literal scalars, or ``None`` when any element is not one."""
    if not isinstance(node, yaml.SequenceNode):
        return None
    found = [_scalar(item) for item in node.value]
    return None if any(item is None for item in found) else [str(item) for item in found]


def _container_port(entry: yaml.Node) -> str | None:
    if isinstance(entry, yaml.MappingNode):
        fields = _mapping(entry) or {}
        target = _scalar(fields["target"][1]) if "target" in fields else None
        return target if target is not None and _PORT.fullmatch(target) else None
    text = _scalar(entry)
    if text is None:
        return None
    container = text.split("/", 1)[0].rsplit(":", 1)[-1]
    return container if _PORT.fullmatch(container) else None


def _command(node: yaml.Node | None) -> list[str] | None:
    if node is None:
        return None
    listed = _strings(node)
    if listed is not None:
        return listed
    text = _scalar(node)
    return text.split() if text is not None else None


def _inside(directory: str, relative: str) -> str | None:
    """``relative`` from a repository directory, normalised; ``None`` outside the repository."""
    if relative.startswith("/"):
        return None
    joined = posixpath.normpath(posixpath.join(directory or ".", relative))
    if joined == ".." or joined.startswith("../"):
        return None
    return "" if joined == "." else joined


def _environment(node: yaml.Node) -> list[list[object]]:
    """A service's environment variable names with their lines; values are never kept."""
    found: list[list[object]] = []
    if isinstance(node, yaml.MappingNode):
        for key, _ in node.value:
            if isinstance(key, yaml.ScalarNode) and not _INTERPOLATION.search(str(key.value)):
                found.append([str(key.value), key.start_mark.line + 1])
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            if isinstance(item, yaml.ScalarNode):
                name = str(item.value).split("=", 1)[0].strip()
                if name and not _INTERPOLATION.search(name):
                    found.append([name, item.start_mark.line + 1])
    return found


def compose_references(path: PurePosixPath, documents: list[yaml.Node]) -> list[ParsedReference]:
    """A :data:`~repoatlas.parsers.http_facts.COMPOSE_SERVICE` claim per literal service."""
    directory = "" if str(path.parent) == "." else str(path.parent)
    found: list[ParsedReference] = []
    for document in documents:
        root = _mapping(document)
        services = _mapping(root["services"][1]) if root and "services" in root else None
        for name, (key, value) in (services or {}).items():
            if _INTERPOLATION.search(name):
                continue
            fields = _mapping(value) or {}
            names = [name]
            for field in ("container_name", "hostname"):
                alias = _scalar(fields[field][1]) if field in fields else None
                if alias is not None and alias not in names:
                    names.append(alias)
            networks = _mapping(fields["networks"][1]) if "networks" in fields else None
            for _, (_, network) in (networks or {}).items():
                attached = _mapping(network) or {}
                aliases = _strings(attached["aliases"][1]) if "aliases" in attached else None
                names.extend(alias for alias in aliases or [] if alias not in names)
            ports: list[str] = []
            exposed = fields["expose"][1] if "expose" in fields else None
            published = fields["ports"][1] if "ports" in fields else None
            for listed in (exposed, published):
                for entry in listed.value if isinstance(listed, yaml.SequenceNode) else []:
                    port = _container_port(entry)
                    if port is not None and port not in ports:
                        ports.append(port)
            meta: dict[str, str] = {
                "names": json.dumps(names),
                "ports": json.dumps(ports),
            }
            build = fields["build"][1] if "build" in fields else None
            context_text, dockerfile_text = _scalar(build), "Dockerfile"
            if isinstance(build, yaml.MappingNode):
                parts = _mapping(build) or {}
                context_text = _scalar(parts["context"][1]) if "context" in parts else "."
                if "dockerfile" in parts:
                    dockerfile_text = _scalar(parts["dockerfile"][1])
            context = _inside(directory, context_text) if context_text is not None else None
            dockerfile = (
                _inside(context, dockerfile_text)
                if context is not None and dockerfile_text is not None
                else None
            )
            if context is not None and dockerfile is not None:
                meta["build"] = json.dumps({"context": context, "dockerfile": dockerfile})
            for field in ("command", "entrypoint"):
                argv = _command(fields[field][1]) if field in fields else None
                if field in fields:
                    meta[field] = json.dumps(argv)
            image = _scalar(fields["image"][1]) if "image" in fields else None
            if image is not None:
                meta["image"] = image
            environment = (
                _environment(fields["environment"][1]) if "environment" in fields else None
            )
            if environment:
                meta["environment"] = json.dumps(environment)
            line = key.start_mark.line + 1
            found.append(
                pending_reference(
                    EdgeType.REFERENCES,
                    EntityType.CONFIG,
                    name,
                    line,
                    line,
                    role=COMPOSE_SERVICE,
                    **meta,
                )
            )
    return found
