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

A Helm chart's Service and Ingress (:mod:`repoatlas.parsers.deploy_helm`) are rendered
only under a release a Tiltfile binds by a literal name, from the chart's literal values
and the release's literal ``set``; nothing renders a template. A rendered Ingress host is
an identity tied as the chart's Service is. Every read Ingress also gives host patterns
for the deployments no local source establishes (a CI job's orb is not in any
repository): never linked, they only let the join say that a request's deployment
binding is unresolved.

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
    HELM_INGRESS,
    HELM_RELEASE,
    HELM_SERVICE,
    HELM_VALUES,
    HTTP_EXPOSED_ENDPOINT,
    HTTP_UNRESOLVED_PATTERN,
    HTTP_UNSOURCED_IDENTITY,
    PENDING_FACT,
)
from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord

COMPOSE = "docker-compose-service"
KUBERNETES = "kubernetes-service"
INGRESS = "kubernetes-ingress"

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
CHART_UNNAMED = "its chart's name is not read"
BACKEND_UNMATCHED = "its backend is not its chart's own Service, by name and port as written"
NOT_RENDERED = "no deployment of its chart renders it"
RENDERED = "rendered"
BINDING_UNRESOLVED = "no locally established deployment resolves its hosts"

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
    #: ``http-unsourced-identity`` claims: identities resolved to an origin that reach no
    #: code, one per origin, so the join can say which hop a request failed.
    identities: list[UnresolvedRefRecord] = field(default_factory=list)
    #: ``http-unresolved-pattern`` claims: the hosts each read Ingress would give under a
    #: deployment no local source establishes, one per host and scheme. Never linked.
    patterns: list[UnresolvedRefRecord] = field(default_factory=list)


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
    ingresses = grouped.get(HELM_INGRESS, ())
    if not services and not kubernetes and not ingresses:
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
    if kubernetes or ingresses:
        charts = _Charts(grouped, by_id, routes)
        for service in kubernetes:
            origin = by_id.get(service.file_id)
            if origin is None:
                continue
            outcome = charts.tie(service, origin, plan)
            if service.id is not None:
                plan.outcomes[service.id] = outcome
        for ingress in ingresses:
            origin = by_id.get(ingress.file_id)
            if origin is None:
                continue
            outcome = charts.ingress(ingress, origin, plan)
            if ingress.id is not None:
                plan.outcomes[ingress.id] = outcome
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
    origins = _origins(json.loads(meta.get("names", "[]")), json.loads(meta.get("ports", "[]")))
    identity_in = f"{origin.rel_path}:{service.line_start}"
    scope = f"compose:{service.repo_id}:{origin.rel_path}:{service.target_key}"

    def untied(outcome: str, bound_in: str = "") -> str:
        """The service resolves to its origins but reaches no code: say so for the join."""
        _unsourced(
            plan,
            service,
            origins,
            source=COMPOSE,
            identity_in=identity_in,
            bound_in=bound_in,
            outcome=outcome,
            scope=scope,
        )
        return outcome

    build = json.loads(meta.get("build", "null"))
    if not isinstance(build, dict):
        return untied(NO_BUILD)
    dockerfile_id = files.get(str(build.get("dockerfile", "")))
    command = commands.get(dockerfile_id) if dockerfile_id is not None else None
    if command is None or dockerfile_id is None:
        return untied(DOCKERFILE_UNREAD)
    command_meta = _meta(command)
    read = _argv(meta, command_meta)
    paths = _application(read[0], command_meta.get("workdir")) if read is not None else None
    if read is None or paths is None:
        return untied(COMMAND_UNREAD)
    bound_in = identity_in if read[1] else f"{by_id[dockerfile_id].rel_path}:{command.line_start}"
    copies = json.loads(command_meta.get("copies", "[]"))
    context = str(build.get("context", ""))
    module = next(
        (found for path in paths if (found := _copied(path, copies, context, set(files)))),
        None,
    )
    if module is None:
        return untied(NOT_IN_IMAGE, bound_in)
    served = by_root.get(files[module], ())
    if not served:
        return untied(NO_ROUTES, bound_in)
    if not origins:
        return NO_PORT
    _expose(
        plan,
        served,
        origins,
        source=COMPOSE,
        identity_in=identity_in,
        bound_in=bound_in,
        kind="docker-compose",
        scope=scope,
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
        #: Each chart's unconditional Services: their name and port texts, as written.
        self._services: dict[tuple[int, str], list[tuple[str, frozenset[str]]]] = defaultdict(list)
        for claim in grouped.get(HELM_SERVICE, ()):
            meta = _meta(claim)
            if meta.get("controlled") != "true":
                ports = frozenset(_spelled(str(text)) for text, _ in json.loads(meta["ports"]))
                self._services[(claim.repo_id, meta.get("chart", ""))].append(
                    (_spelled(meta.get("name", "")), ports)
                )
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
        # Without one application to tie to, a bound name is still an identity: one that
        # reaches no code, which the join reports instead of a service it never heard of.
        untied = self._untied(origin.repo_id)
        scope = f"chart:{origin.repo_id}:{meta.get('chart', '')}"
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
            if untied is not None:
                _unsourced(
                    plan,
                    service,
                    origins,
                    source=KUBERNETES,
                    identity_in=f"{origin.rel_path}:{service.line_start}",
                    bound_in=bound_in,
                    outcome=untied,
                    scope=scope,
                )
                continue
            _expose(
                plan,
                served,
                origins,
                source=KUBERNETES,
                identity_in=f"{origin.rel_path}:{service.line_start}",
                bound_in=bound_in,
                kind="kubernetes",
                scope=scope,
            )
            outcome = TIED
        return untied or outcome

    def _untied(self, repo_id: int) -> str | None:
        """Why the repository's code is not one application to tie a chart to, if it is not."""
        if not self._routes[repo_id]:
            return NOTHING_SERVED
        if repo_id in self._untraced or len(self._roots[repo_id]) != 1:
            return NOT_ONE_APPLICATION
        return None

    def ingress(self, claim: UnresolvedRefRecord, origin: FileRow, plan: DeploymentPlan) -> str:
        """An Ingress's hosts under each locally bound release, tied to its repository's one
        application or recorded as reaching no code; and, for every deployment no local
        source establishes, its host patterns."""
        meta = _meta(claim)
        if "unread" in meta:
            return meta["unread"]
        key = (origin.repo_id, meta.get("chart", ""))
        chart = self._charts.get(key)
        if chart is None:
            return CHART_UNNAMED
        backends = json.loads(meta.get("backends", "[]"))
        services = self._services.get(key, [])
        if not backends or not all(
            any(
                _spelled(name) == spelled and _spelled(port) in ports for spelled, ports in services
            )
            for name, port, _ in backends
        ):
            return BACKEND_UNMATCHED
        hosts = [(parts, int(line)) for parts, line in json.loads(meta["hosts"])]
        listen = [(str(scheme), int(port)) for scheme, port in json.loads(meta["listen"])]
        scope = f"chart:{origin.repo_id}:{meta.get('chart', '')}"
        extra = {
            "scheme_in": f"{origin.rel_path}:{meta.get('scheme_at', '')}",
            "backend_in": f"{origin.rel_path}:{backends[0][2]}",
        }
        untied = self._untied(origin.repo_id)
        bindings: list[tuple[str, str]] = []
        identified: set[str] = set()
        for release in self._releases.get(key, []):
            releaser = self._by_id.get(release.file_id)
            bound_in = f"{releaser.rel_path}:{release.line_start}" if releaser else ""
            rendered = self._rendered(meta, chart, self._values(chart, _meta(release)))
            if isinstance(rendered, str):
                bindings.append((bound_in, rendered))
                continue
            found, unresolved = rendered
            for host, line in found:
                origins = _ingress_origins(host, listen)
                if not origins:
                    unresolved = True
                    continue
                identity = {"identity_in": f"{origin.rel_path}:{line}", "bound_in": bound_in}
                if untied is not None:
                    _unsourced(
                        plan,
                        claim,
                        origins,
                        source=INGRESS,
                        outcome=untied,
                        scope=scope,
                        extra=extra,
                        **identity,
                    )
                else:
                    _expose(
                        plan,
                        self._routes[origin.repo_id],
                        origins,
                        source=INGRESS,
                        kind="kubernetes",
                        scope=scope,
                        extra=extra,
                        **identity,
                    )
                identified.add(untied or TIED)
            bindings.append((bound_in, BINDING_UNRESOLVED if unresolved else RENDERED))
        states = {state for _, state in bindings}
        reached = (TIED, *([untied] if untied else []), BINDING_UNRESOLVED, NOT_RENDERED)
        outcome = next((s for s in reached if s in identified | states), RELEASE_UNBOUND)
        for parts, line in hosts:
            for scheme, port in listen:
                _pattern(
                    plan,
                    claim,
                    parts,
                    chart.name,
                    meta.get("range", ""),
                    scheme,
                    port,
                    {
                        "identity_source": INGRESS,
                        "identity_in": f"{origin.rel_path}:{line}",
                        **extra,
                        "chart": chart.name,
                        "bound_in": "; ".join(bound for bound, _ in bindings if bound),
                        "binding_outcome": "; ".join(dict.fromkeys(state for _, state in bindings))
                        or RELEASE_UNBOUND,
                        "source_outcome": untied or TIED,
                        "scope": scope,
                    },
                )
        return outcome

    @staticmethod
    def _rendered(
        meta: Mapping[str, str], chart: _Chart, values: Mapping[str, str] | None
    ) -> tuple[list[tuple[str, int]], bool] | str:
        """The hosts a release's values give an Ingress, with their lines, and whether any
        host was left unresolved; or :data:`NOT_RENDERED`, or :data:`BINDING_UNRESOLVED`."""
        if values is None:
            return BINDING_UNRESOLVED
        condition = meta.get("if", "")
        if condition:
            value = values.get(condition)
            if value is None and chart.truncated:
                return BINDING_UNRESOLVED
            # Absent, a value is nil to Helm, unless it is a collection with something in it.
            nested = any(k.startswith((f"{condition}.", f"{condition}[")) for k in values)
            if (value is None and not nested) or (value is not None and value in _FALSY):
                return NOT_RENDERED
        iterable = meta.get("range", "")
        items: list[str] = [""]
        if iterable:
            if chart.truncated:
                return BINDING_UNRESOLVED
            item = re.compile(re.escape(iterable) + r"\[(\d+)\]")
            indices = sorted({int(m.group(1)) for k in values if (m := item.match(k))})
            if not indices:
                return BINDING_UNRESOLVED
            items = [f"{iterable}[{index}]" for index in indices]
        found: list[tuple[str, int]] = []
        unresolved = False
        for current in items:
            for parts, line in json.loads(meta["hosts"]):
                host: str | None = ""
                for kind, value in parts:
                    text = (
                        value
                        if kind == "text"
                        else chart.name
                        if kind == "chart"
                        else values.get(value if kind == "value" else f"{current}.{value}")
                    )
                    if text is None or host is None:
                        host = None
                        break
                    host += text
                if host is None:
                    unresolved = True
                else:
                    found.append((host, int(line)))
        return found, unresolved

    @staticmethod
    def _values(chart: _Chart, release: Mapping[str, str]) -> dict[str, str] | None:
        """The chart's values with a release's literal ``set`` applied; ``None`` when the
        release also names values files, or a setting this does not read. A setting
        replaces whatever the values held at, above or under its path."""
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
            path = match.group(1)
            names = path.split(".")
            for above in (".".join(names[:end]) for end in range(1, len(names))):
                values.pop(above, None)
            for under in [k for k in values if k.startswith((f"{path}.", f"{path}["))]:
                del values[under]
            values[path] = match.group(2)
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
    scope: str,
    extra: Mapping[str, str] | None = None,
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
                "scope": scope,
                **(extra or {}),
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


def _unsourced(
    plan: DeploymentPlan,
    service: UnresolvedRefRecord,
    origins: Sequence[str],
    *,
    source: str,
    identity_in: str,
    bound_in: str,
    outcome: str,
    scope: str,
    extra: Mapping[str, str] | None = None,
) -> None:
    """One ``http-unsourced-identity`` claim per origin of an identity that reaches no code."""
    for deployment in origins:
        claim_meta = {
            PENDING_FACT: HTTP_UNSOURCED_IDENTITY,
            "deployment": deployment,
            "identity_source": source,
            "identity_in": identity_in,
            "bound_in": bound_in,
            "source_outcome": outcome,
            "scope": scope,
            **(extra or {}),
        }
        plan.identities.append(
            UnresolvedRefRecord(
                repo_id=service.repo_id,
                file_id=service.file_id,
                src_id=service.src_id,
                edge_type=EdgeType.REFERENCES,
                target_kind=EntityType.HTTP_ENDPOINT,
                target_key=deployment,
                line_start=service.line_start,
                line_end=service.line_end,
                meta_json=json.dumps(claim_meta, sort_keys=True),
            )
        )


# -- Ingress hosts ------------------------------------------------------------------------

_DEFAULT_PORTS = frozenset({("http", 80), ("https", 443)})
_SPACE = re.compile(r"\s+")


def _spelled(text: str) -> str:
    """A name or port as written, each action's spacing and trim markers normalised, and
    the root -- ``$`` or ``$.x`` -- read as it is written outside a ``range``: ``.``, ``.x``."""

    def action(match: re.Match[str]) -> str:
        body = _SPACE.sub(" ", match.group(1).strip().removeprefix("-").removesuffix("-").strip())
        body = re.sub(r"(?<![\w$])\$\.", ".", body)
        return "{{ " + re.sub(r"(?<![\w$.])\$(?![\w.])", ".", body) + " }}"

    return _TEMPLATE.sub(action, text).strip()


def _ingress_origins(host: str, listen: Sequence[tuple[str, int]]) -> list[str]:
    """The origins an Ingress host answers on: ``scheme://host`` on the scheme's default
    port (and with that port written), ``scheme://host:port`` on any other."""
    found: list[str] = []
    for scheme, port in listen:
        written = f"{scheme}://{host}:{port}"
        for origin in ([f"{scheme}://{host}"] if (scheme, port) in _DEFAULT_PORTS else []) + [
            written
        ]:
            target = http.client_target(origin + "/")
            if target is not None and target.origin == origin and origin not in found:
                found.append(origin)
    return found


def _pattern(
    plan: DeploymentPlan,
    claim: UnresolvedRefRecord,
    parts: Sequence[Sequence[str]],
    chart: str,
    iterable: str,
    scheme: str,
    port: int,
    evidence: Mapping[str, str],
) -> None:
    """The ``http-unresolved-pattern`` claim of one Ingress host and scheme, when it is
    anchored by the chart's name or by a literal label; a pattern of holes alone would fit
    every host."""
    # The host as the join matches it (repoatlas.graph.http_crossings): literal text, and
    # holes of one or more whole labels (bounded by dots or the host's ends) or of part
    # of one label.
    host: list[list[str]] = []
    display = ""
    unresolved: list[str] = []
    labels: list[list[str]] = [[]]
    for index, (kind, value) in enumerate(parts):
        if kind in {"text", "chart"}:
            text = value if kind == "text" else chart
            if host and host[-1][0] == "text":
                host[-1][1] += text
            else:
                host.append(["text", text])
            display += text
            for char in text:
                if char == ".":
                    labels.append([])
                else:
                    labels[-1].append(kind)
            continue
        name = value if kind == "value" else f"{iterable}[].{value}"
        before = parts[index - 1] if index else None
        after = parts[index + 1] if index + 1 < len(parts) else None
        bounded = (before is None or (before[0] == "text" and before[1].endswith("."))) and (
            after is None or (after[0] == "text" and after[1].startswith("."))
        )
        host.append(["labels" if bounded else "label", ""])
        display += "{" + name + "}"
        labels[-1].append("hole")
        if name not in unresolved:
            unresolved.append(name)
    anchored = any(kind == "chart" for kind, _ in parts) or any(
        label and all(piece == "text" for piece in label) for label in labels
    )
    if not anchored:
        return
    pattern = f"{scheme}://{display}" + ("" if (scheme, port) in _DEFAULT_PORTS else f":{port}")
    claim_meta = {
        PENDING_FACT: HTTP_UNRESOLVED_PATTERN,
        "pattern": pattern,
        "scheme": scheme,
        "port": str(port),
        "host": json.dumps(host),
        **evidence,
    }
    if unresolved:
        claim_meta["unresolved"] = ", ".join(unresolved)
    plan.patterns.append(
        UnresolvedRefRecord(
            repo_id=claim.repo_id,
            file_id=claim.file_id,
            src_id=claim.src_id,
            edge_type=EdgeType.REFERENCES,
            target_kind=EntityType.HTTP_ENDPOINT,
            target_key=pattern,
            line_start=claim.line_start,
            line_end=claim.line_end,
            meta_json=json.dumps(claim_meta, sort_keys=True),
        )
    )
