"""Service identities a deployment declares, tied to the code that answers them (HTTP linkage).

A deployment names an address -- ``http://svc-e6:5000`` for a docker-compose service --
and runs an image there. The address is a service identity; the code behind it is
whatever that image runs. This stage ties the two only through what the files state,
and turns each tie into the same ``http-exposed-endpoint`` claim P4's join reads
(:mod:`repoatlas.graph.http_crossings`), with ``identity_source`` naming the kind of
deployment and nothing more specific:

* a **compose service** (:mod:`repoatlas.parsers.deploy_compose`) that builds its image:
  its Dockerfile, in the same repository, is read (:mod:`repoatlas.parsers.dockerfile`);
* the **command** the container runs is the Dockerfile's ``ENTRYPOINT`` and ``CMD``, as
  overridden by the service's own ``entrypoint`` and ``command``. It is read in three
  forms only: ``python[3] [-u] <file.py>``, ``uvicorn [options] <module>:<attr>`` and
  ``gunicorn [options] <module>:<attr>``, each option one these programs document and
  none that changes where the module is found (``--app-dir``, ``--chdir``, a config
  file, a factory). A relative path or module is found from the working directory.
  An ``ENTRYPOINT`` the base image sets is not known, so a command is read as the
  program the image runs;
* the **module** the command names is mapped back to a repository file through the
  final stage's ``COPY``/``ADD`` instructions, last first, as the image layers them: a
  copied file at that path, or the file at that path under a copied directory. A copy
  whose content comes from elsewhere (``--from``, a URL, a wildcard) and that could
  cover the path leaves it unknown;
* the **routes** the identity exposes are those of the application that module
  constructs: per-file routes in that module, and routes mounted onto an application
  constructed there (:mod:`repoatlas.graph.py_routes`). A specification document is
  not an application.

Each route is then exposed at ``http://<name>:<port>`` for every name and container
port the service declares (and at ``http://<name>`` too for port 80). The claim cites
the route's handler and lines, the compose service (``identity_in``) and the command
(``bound_in``).

This says that the deployment's files route that address to this code. It does not say
that the deployment runs, or that nothing else answers there.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from repoatlas.models import http
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.parsers.http_facts import (
    COMPOSE_SERVICE,
    DOCKERFILE_COMMAND,
    HELM_CHART,
    HELM_HELPER,
    HELM_RELEASE,
    HELM_SERVICE,
    HELM_VALUES,
    HTTP_EXPOSED_ENDPOINT,
    PENDING_FACT,
)
from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord

COMPOSE = "docker-compose-service"
KUBERNETES = "kubernetes-service"

TIED = "tied"
NO_BUILD = "the service builds no image here"
DOCKERFILE_UNREAD = "its Dockerfile is not indexed, or states nothing its image runs"
COMMAND_UNREAD = "the command its container runs is not read"
NOT_IN_IMAGE = "the module the command runs is not copied from this repository"
NO_ROUTES = "the module the command runs serves no route"
NO_PORT = "it declares no literal container port"
CONTROLLED = "its template renders it only under a condition or loop"
RELEASE_UNBOUND = "no release of its chart is bound by a literal name"
NAME_UNREAD = "its name is not read"
NOT_ONE_APPLICATION = "its repository's code is not one traced application"
NOTHING_SERVED = "its repository's code serves no route"

_PYTHON = re.compile(r"python(\d+(\.\d+)?)?")
_PYTHON_FLAGS = frozenset({"-u", "-O", "-OO", "-B", "-E", "-s", "-S", "-I"})
_MODULE = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*")
_ATTRIBUTE = re.compile(r"[A-Za-z_]\w*")
#: Options that take a value, and options that take none, of the two ASGI/WSGI servers
#: read here. Anything else -- above all an option that moves where the application is
#: imported from -- leaves the command unread.
_SERVER_OPTIONS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "uvicorn": (
        frozenset(
            {
                "--host",
                "--port",
                "--uds",
                "--fd",
                "--workers",
                "--log-level",
                "--root-path",
                "--limit-concurrency",
                "--backlog",
                "--limit-max-requests",
                "--timeout-keep-alive",
                "--timeout-graceful-shutdown",
                "--forwarded-allow-ips",
                "--loop",
                "--http",
                "--ws",
                "--lifespan",
                "--interface",
                "--log-config",
                "--header",
                "--ws-max-size",
                "--ws-max-queue",
                "--ws-ping-interval",
                "--ws-ping-timeout",
            }
        ),
        frozenset(
            {
                "--reload",
                "--access-log",
                "--no-access-log",
                "--use-colors",
                "--no-use-colors",
                "--proxy-headers",
                "--no-proxy-headers",
                "--server-header",
                "--no-server-header",
                "--date-header",
                "--no-date-header",
            }
        ),
    ),
    "gunicorn": (
        frozenset(
            {
                "-b",
                "--bind",
                "-w",
                "--workers",
                "-k",
                "--worker-class",
                "-t",
                "--timeout",
                "--threads",
                "--keep-alive",
                "--graceful-timeout",
                "--max-requests",
                "--max-requests-jitter",
                "--access-logfile",
                "--error-logfile",
                "--log-file",
                "--log-level",
                "--access-logformat",
                "--worker-connections",
                "--backlog",
                "-n",
                "--name",
                "--forwarded-allow-ips",
            }
        ),
        frozenset({"--reload", "--preload", "--capture-output"}),
    ),
}


@dataclass(frozen=True, slots=True)
class ServedRoute:
    """A route served by code: its handler, where it is stated, and its applications."""

    repo_id: int
    file_id: int
    src_id: int
    body: str
    line_start: int | None
    line_end: int | None
    #: The modules constructing the applications that serve it; empty when not traced.
    roots: frozenset[int]
    declared_at: int | None = None


@dataclass(slots=True)
class DeploymentPlan:
    #: ``http-exposed-endpoint`` claims for the join, one per identity and route.
    exposures: list[UnresolvedRefRecord] = field(default_factory=list)
    #: Deployment claim id → ``tied`` or why not.
    outcomes: dict[int, str] = field(default_factory=dict)


def _meta(claim: UnresolvedRefRecord) -> dict[str, str]:
    loaded = json.loads(claim.meta_json) if claim.meta_json else {}
    return {str(k): str(v) for k, v in loaded.items()}


def _absolute(path: str, workdir: str | None) -> str | None:
    if path.startswith("/"):
        return posixpath.normpath(path)
    return posixpath.normpath(posixpath.join(workdir, path)) if workdir is not None else None


def _application(argv: Sequence[str], workdir: str | None) -> list[str] | None:
    """The container paths the module the command runs could be at, or ``None``."""
    if not argv:
        return None
    program = posixpath.basename(argv[0])
    if _PYTHON.fullmatch(program):
        rest = list(argv[1:])
        while rest and rest[0] in _PYTHON_FLAGS:
            rest = rest[1:]
        if not rest or not rest[0].endswith(".py"):
            return None
        found = _absolute(rest[0], workdir)
        return [found] if found is not None else None
    if program not in _SERVER_OPTIONS:
        return None
    takes_value, flags = _SERVER_OPTIONS[program]
    positional: list[str] = []
    rest = list(argv[1:])
    while rest:
        word = rest.pop(0)
        name = word.split("=", 1)[0]
        if not word.startswith("-"):
            positional.append(word)
        elif name in takes_value:
            if "=" not in word and not rest:
                return None
            if "=" not in word:
                rest.pop(0)
        elif word not in flags:
            return None
    if len(positional) != 1:
        return None
    module, _, attribute = positional[0].partition(":")
    if not _MODULE.fullmatch(module) or not _ATTRIBUTE.fullmatch(attribute):
        return None
    relative = module.replace(".", "/")
    found = [_absolute(f"{relative}.py", workdir), _absolute(f"{relative}/__init__.py", workdir)]
    return [path for path in found if path is not None] or None


def _argv(service: Mapping[str, str], command: Mapping[str, str]) -> tuple[list[str], bool] | None:
    """The command the container runs -- ENTRYPOINT then CMD, each as the service overrides
    it -- and whether the service's own override decides it; ``None`` when unread."""
    entrypoint = json.loads(command.get("entrypoint", "null"))
    cmd = json.loads(command.get("cmd", "null"))
    if "entrypoint" in service:
        replaced = json.loads(service["entrypoint"])
        arguments = json.loads(service["command"]) if "command" in service else []
        if replaced is None or arguments is None:
            return None
        return [*replaced, *arguments], True
    overridden = "command" in service
    arguments = json.loads(service["command"]) if overridden else (cmd or {}).get("argv", [])
    if arguments is None:
        return None
    if entrypoint is not None:
        if entrypoint.get("argv") is None:
            return None
        if entrypoint.get("form") == "shell":
            return list(entrypoint["argv"]), False  # a shell-form ENTRYPOINT ignores CMD
        return [*entrypoint["argv"], *arguments], overridden
    if not overridden and cmd is None:
        return None
    return list(arguments), overridden


def _copied(
    container: str, copies: Sequence[Sequence[object]], context: str, files: set[str]
) -> str | None:
    """The repository file the image layers at ``container``, last copy first."""
    for sources, destination, _line, foreign, into in reversed(copies):
        if not isinstance(destination, str):
            return None  # a destination this file does not state could be any path
        inside = container == destination or container.startswith(destination.rstrip("/") + "/")
        if foreign:
            if inside:
                return None
            continue
        listed = [str(source) for source in sources] if isinstance(sources, list) else []
        for source in listed:
            path = posixpath.normpath(posixpath.join(context or ".", source))
            if path == ".." or path.startswith("../") or path.startswith("/"):
                return None
            path = "" if path == "." else path
            if path in files:
                target = (
                    posixpath.join(destination, posixpath.basename(path))
                    if into or len(listed) > 1
                    else destination
                )
                if target == container:
                    return path
            elif inside and container != destination:
                tail = container[len(destination.rstrip("/")) + 1 :]
                candidate = posixpath.join(path, tail) if path else tail
                if candidate in files:
                    return candidate
    return None


def _origins(names: Iterable[str], ports: Iterable[str]) -> list[str]:
    found: list[str] = []
    for name in names:
        for port in ports:
            for origin in (f"http://{name}:{port}", *([f"http://{name}"] if port == "80" else [])):
                target = http.client_target(origin + "/")
                if target is not None and target.origin == origin and origin not in found:
                    found.append(origin)
    return found


def plan_deployment_exposures(
    grouped: Mapping[str, Sequence[UnresolvedRefRecord]],
    by_id: Mapping[int, FileRow],
    routes: Sequence[ServedRoute],
) -> DeploymentPlan:
    """Every route a declared service identity reaches, as exposure claims. Writes nothing."""
    plan = DeploymentPlan()
    services = grouped.get(COMPOSE_SERVICE, ())
    kubernetes = grouped.get(HELM_SERVICE, ())
    if not services and not kubernetes:
        return plan
    files: dict[int, dict[str, int]] = defaultdict(dict)
    for row in by_id.values():
        files[row.repo_id][row.rel_path] = row.file_id
    commands = {claim.file_id: claim for claim in grouped.get(DOCKERFILE_COMMAND, ())}
    by_root: dict[int, list[ServedRoute]] = defaultdict(list)
    for route in routes:
        for root in route.roots:
            by_root[root].append(route)

    for service in services:
        origin = by_id.get(service.file_id)
        if origin is None:
            continue
        outcome = _tie(service, origin, files[origin.repo_id], commands, by_root, by_id, plan)
        if service.id is not None:
            plan.outcomes[service.id] = outcome
    if kubernetes:
        charts = _Charts(grouped, by_id, routes)
        for service in kubernetes:
            origin = by_id.get(service.file_id)
            if origin is None:
                continue
            outcome = charts.tie(service, origin, plan)
            if service.id is not None:
                plan.outcomes[service.id] = outcome
    return plan


def _tie(
    service: UnresolvedRefRecord,
    origin: FileRow,
    files: Mapping[str, int],
    commands: Mapping[int, UnresolvedRefRecord],
    by_root: Mapping[int, Sequence[ServedRoute]],
    by_id: Mapping[int, FileRow],
    plan: DeploymentPlan,
) -> str:
    meta = _meta(service)
    build = json.loads(meta.get("build", "null"))
    if not isinstance(build, dict):
        return NO_BUILD
    dockerfile_id = files.get(str(build.get("dockerfile", "")))
    command = commands.get(dockerfile_id) if dockerfile_id is not None else None
    if command is None or dockerfile_id is None:
        return DOCKERFILE_UNREAD
    command_meta = _meta(command)
    read = _argv(meta, command_meta)
    paths = _application(read[0], command_meta.get("workdir")) if read is not None else None
    if read is None or paths is None:
        return COMMAND_UNREAD
    copies = json.loads(command_meta.get("copies", "[]"))
    context = str(build.get("context", ""))
    module = next(
        (found for path in paths if (found := _copied(path, copies, context, set(files)))),
        None,
    )
    if module is None:
        return NOT_IN_IMAGE
    served = by_root.get(files[module], ())
    if not served:
        return NO_ROUTES
    origins = _origins(json.loads(meta.get("names", "[]")), json.loads(meta.get("ports", "[]")))
    if not origins:
        return NO_PORT
    identity_in = f"{origin.rel_path}:{service.line_start}"
    bound_in = identity_in if read[1] else f"{by_id[dockerfile_id].rel_path}:{command.line_start}"
    _expose(
        plan,
        served,
        origins,
        source=COMPOSE,
        identity_in=identity_in,
        bound_in=bound_in,
        kind="docker-compose",
    )
    return TIED


# -- Kubernetes Services from Helm charts -------------------------------------------------

_DNS_LABEL = re.compile(r"[a-z]([-a-z0-9]*[a-z0-9])?")
_PORT = re.compile(r"[1-9]\d{0,4}")
_TEMPLATE = re.compile(r"\{\{-?\s*(.*?)\s*-?\}\}")
_VALUE_PATH = re.compile(r"\.Values((?:\.[A-Za-z_][\w-]*)+)")
_INCLUDE = re.compile(r'include "([\w.-]+)\.(name|fullname)" \.')
_SETTING = re.compile(r"([A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*)=([^,\\{}\[\]]*)")
_FALSY = frozenset({"", "false", "False", "FALSE", "null", "Null", "NULL", "~", "0"})


def _trimmed(text: str) -> str:
    """Helm's ``trunc 63 | trimSuffix "-"``: at most 63 characters, one trailing dash dropped."""
    return text[:63].removesuffix("-")


def helm_name(chart: str, values: Mapping[str, str]) -> str:
    """The scaffold ``<x>.name``: ``nameOverride`` when set, else the chart's name."""
    override = values.get("nameOverride", "")
    return _trimmed(override if override not in _FALSY else chart)


def helm_fullname(chart: str, release: str, values: Mapping[str, str]) -> str:
    """The scaffold ``<x>.fullname``: ``fullnameOverride`` when set; else the release name
    when it contains the chart's (or ``nameOverride``'s) name; else ``<release>-<name>``."""
    override = values.get("fullnameOverride", "")
    if override not in _FALSY:
        return _trimmed(override)
    name = values.get("nameOverride", "")
    name = name if name not in _FALSY else chart
    return _trimmed(release if name in release else f"{release}-{name}")


@dataclass(frozen=True, slots=True)
class _Chart:
    name: str
    values: dict[str, str]
    #: Whether the values file was cut short: a key it lacks may be one it left out.
    truncated: bool
    helpers: frozenset[str]


class _Charts:
    """The charts of every repository, their releases, and each repository's applications."""

    def __init__(
        self,
        grouped: Mapping[str, Sequence[UnresolvedRefRecord]],
        by_id: Mapping[int, FileRow],
        routes: Sequence[ServedRoute],
    ) -> None:
        self._by_id = by_id
        names: dict[tuple[int, str], str] = {}
        for claim in grouped.get(HELM_CHART, ()):
            names[(claim.repo_id, _meta(claim).get("chart", ""))] = claim.target_key
        values: dict[tuple[int, str], tuple[dict[str, str], bool]] = {}
        for claim in grouped.get(HELM_VALUES, ()):
            meta = _meta(claim)
            values[(claim.repo_id, meta.get("chart", ""))] = (
                {str(k): str(v) for k, v in json.loads(meta.get("values", "{}")).items()},
                meta.get("truncated") == "true",
            )
        helpers: dict[tuple[int, str], set[str]] = defaultdict(set)
        for claim in grouped.get(HELM_HELPER, ()):
            helpers[(claim.repo_id, _meta(claim).get("chart", ""))].add(claim.target_key)
        self._charts = {
            key: _Chart(
                name=name,
                values=values.get(key, ({}, False))[0],
                truncated=values.get(key, ({}, False))[1],
                helpers=frozenset(helpers.get(key, set())),
            )
            for key, name in names.items()
        }
        self._releases: dict[tuple[int, str], list[UnresolvedRefRecord]] = defaultdict(list)
        for claim in grouped.get(HELM_RELEASE, ()):
            self._releases[(claim.repo_id, _meta(claim).get("chart", ""))].append(claim)
        self._routes: dict[int, list[ServedRoute]] = defaultdict(list)
        self._roots: dict[int, set[int]] = defaultdict(set)
        self._untraced: set[int] = set()
        for route in routes:
            self._routes[route.repo_id].append(route)
            self._roots[route.repo_id].update(route.roots)
            if not route.roots:
                self._untraced.add(route.repo_id)

    def tie(self, service: UnresolvedRefRecord, origin: FileRow, plan: DeploymentPlan) -> str:
        meta = _meta(service)
        if meta.get("controlled") == "true":
            return CONTROLLED
        key = (origin.repo_id, meta.get("chart", ""))
        chart = self._charts.get(key)
        releases = self._releases.get(key, [])
        if chart is None or not releases:
            return RELEASE_UNBOUND
        served = self._routes[origin.repo_id]
        if not served:
            return NOTHING_SERVED
        if origin.repo_id in self._untraced or len(self._roots[origin.repo_id]) != 1:
            return NOT_ONE_APPLICATION
        outcome = NAME_UNREAD
        for release in releases:
            values = self._values(chart, _meta(release))
            if values is None:
                continue
            name = self._render(meta.get("name", ""), chart, release.target_key, values)
            if name is None or not _DNS_LABEL.fullmatch(name) or len(name) > 63:
                continue
            ports = [
                self._render(str(text), chart, release.target_key, values)
                for text, _ in json.loads(meta.get("ports", "[]"))
            ]
            readable = [port for port in ports if port is not None and _PORT.fullmatch(port)]
            origins = _origins([name], readable)
            if not origins:
                outcome = NO_PORT
                continue
            releaser = self._by_id.get(release.file_id)
            bound_in = f"{releaser.rel_path}:{release.line_start}" if releaser else ""
            _expose(
                plan,
                served,
                origins,
                source=KUBERNETES,
                identity_in=f"{origin.rel_path}:{service.line_start}",
                bound_in=bound_in,
                kind="kubernetes",
            )
            outcome = TIED
        return outcome

    @staticmethod
    def _values(chart: _Chart, release: Mapping[str, str]) -> dict[str, str] | None:
        """The chart's values with a release's literal ``set`` applied; ``None`` when the
        release also names values files, or a setting this does not read."""
        if release.get("values_files") == "true":
            return None
        values = dict(chart.values)
        settings = json.loads(release.get("set", "[]"))
        if settings is None:
            return None
        for setting in settings:
            match = _SETTING.fullmatch(str(setting))
            if match is None:
                return None
            values[match.group(1)] = match.group(2)
        return values

    def _render(
        self, text: str, chart: _Chart, release: str, values: Mapping[str, str]
    ) -> str | None:
        """A name or port as written, every action one this reads; ``None`` otherwise."""
        rendered = ""
        cursor = 0
        for match in _TEMPLATE.finditer(text):
            rendered += text[cursor : match.start()]
            cursor = match.end()
            action = match.group(1)
            value: str | None = None
            path = _VALUE_PATH.fullmatch(action)
            included = _INCLUDE.fullmatch(action)
            if action == ".Chart.Name":
                value = chart.name
            elif path is not None:
                value = values.get(path.group(1)[1:])
                if value is None and chart.truncated:
                    return None
            elif included is not None and f"{included.group(1)}.{included.group(2)}" in (
                chart.helpers
            ):
                value = (
                    helm_fullname(chart.name, release, values)
                    if included.group(2) == "fullname"
                    else helm_name(chart.name, values)
                )
            if value is None:
                return None
            rendered += value
        rendered += text[cursor:]
        return None if "{{" in rendered or "}}" in rendered else rendered


def _expose(
    plan: DeploymentPlan,
    served: Sequence[ServedRoute],
    origins: Sequence[str],
    *,
    source: str,
    identity_in: str,
    bound_in: str,
    kind: str,
) -> None:
    """One exposure claim per route and origin."""
    for route in served:
        method, _, path = route.body.partition(" ")
        line = route.line_start if route.line_start is not None else route.declared_at
        if line is None:
            continue
        for deployment in origins:
            body = http.endpoint_body(method, f"{deployment}{path}")
            if http.canonical_endpoint(body) is None:
                continue
            claim_meta = {
                PENDING_FACT: HTTP_EXPOSED_ENDPOINT,
                "method": method,
                "route": path,
                "deployment": deployment,
                "identity_source": source,
                "identity_in": identity_in,
                "bound_in": bound_in,
                "source": kind,
            }
            plan.exposures.append(
                UnresolvedRefRecord(
                    repo_id=route.repo_id,
                    file_id=route.file_id,
                    src_id=route.src_id,
                    edge_type=EdgeType.EXPOSES_HTTP_ENDPOINT,
                    target_kind=EntityType.HTTP_ENDPOINT,
                    target_key=body,
                    line_start=line,
                    line_end=route.line_end if route.line_start is not None else line,
                    meta_json=json.dumps(claim_meta, sort_keys=True),
                )
            )
