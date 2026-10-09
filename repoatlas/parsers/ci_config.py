"""CircleCI and GitHub Actions configurations, and operator declarations, as claims (DF1).

Read, never run: what a CI configuration *configures* -- the images its jobs build and
whether they are configured to publish them, the Helm releases they invoke, and every
step it does not interpret -- recorded as pending claims for the global pass
(:mod:`repoatlas.graph.ci_facts`), which decides them (DF1_PHASE_A.md §3-§6, §9).

**Which files.** ``.circleci/config.yml`` and ``.github/workflows/*.y(a)ml`` at a
repository's root, by path; nothing else under ``.github`` is a workflow. A YAML file whose
root carries ``repoatlas: ci-semantics/v1`` is a **declaration** file (U6); only the
reserved ``@declarations`` source's are accepted, which the global pass decides.

**The YAML.** Read from PyYAML's composed node graph, never constructed: ``on`` stays the
key text it is written as, ``<<`` merges apply local keys first and then each merged
mapping in order, an alias is followed with a visited path (a cycle stops there) and the
whole walk has a budget of :data:`MAX_NODES`. Caps: :data:`MAX_JOBS` jobs,
:data:`MAX_STEPS` steps per job, :data:`MAX_CLAIMS` claims per file; reaching one marks the
claims ``truncated`` and the caller reports it.

**What a step is.**

* a ``uses:`` action: inert, builtin or unsupported by exact revision
  (:mod:`repoatlas.parsers.ci_semantics`); an unsupported one is recorded with its
  reference and kind, never its inputs;
* a ``run`` script (bash or sh): its recognised commands at top level, a nested one, or a
  delegation (:mod:`repoatlas.parsers.ci_shell`);
* a CircleCI orb job or command: inline orbs are source, read like local jobs; another
  orb's is recorded with its parameters' values as parts, credentials withheld, since an
  operator declaration may give them meaning;
* in a workflow only ``workflow_call`` triggers (a reusable workflow definition), a step
  holding a recognised command depends on its caller and is recorded as such.

**Images built** are decided here, per job: a reference is published when the same job
pushes it (or a ``docker tag`` alias of it) or builds it with ``--push``, or when the
builtin action's ``push`` is literally ``true``.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import yaml

from repoatlas.models.container_image import (
    EXPR,
    TEXT,
    VAR,
    WITHHELD,
    parse_reference,
    text_of,
)
from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.ci_semantics import (
    BUILTIN_BUILD,
    INERT_ACTION,
    action_kind,
    classify_action,
)
from repoatlas.parsers.ci_shell import (
    DOCKER_BUILD,
    DOCKER_PUSH,
    DOCKER_TAG,
    HELM,
    KUBECTL_APPLY,
    Command,
    Word,
    delegated,
    flag_values,
    has_flag,
    literal,
    positionals,
    read_script,
    recognised,
)
from repoatlas.parsers.http_facts import PENDING_FACT, pending_reference
from repoatlas.parsers.withhold import credential_named, secret_bearing

#: An image a job builds, its references and whether each is configured to publish.
CI_IMAGE_BUILD = "ci-image-build"
#: A Helm release a job invokes: chart, release, values files and settings as written.
CI_HELM_RELEASE = "ci-helm-release"
#: A step RepoAtlas does not interpret, and why (its ``kind``).
CI_STEP_UNSUPPORTED = "ci-step-unsupported"
#: One operator declaration (an orb job's semantics, or a project's CircleCI name).
CI_SEMANTICS = "ci-semantics"
CI_ROLES = frozenset({CI_IMAGE_BUILD, CI_HELM_RELEASE, CI_STEP_UNSUPPORTED, CI_SEMANTICS})

DECLARATIONS_HEADER = "ci-semantics/v1"

MAX_NODES = 10_000
MAX_JOBS = 200
MAX_STEPS = 200
MAX_CLAIMS = 256

_CIRCLE_STEPS = frozenset(
    {
        "checkout",
        "run",
        "setup_remote_docker",
        "save_cache",
        "restore_cache",
        "store_artifacts",
        "store_test_results",
        "persist_to_workspace",
        "attach_workspace",
        "add_ssh_keys",
        "when",
        "unless",
        "deploy",
    }
)
_SHELLS = frozenset({"bash", "sh"})
_PARAMETER = re.compile(r"<<\s*parameters\.([\w-]+)\s*>>")
_SHELL_VAR = re.compile(r"\$\{\{.*?\}\}|<<.*?>>|\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)")
_EXACT_ORB = re.compile(r"[a-z0-9][\w-]*/[a-z0-9][\w-]*@\d+\.\d+\.\d+")
_ENV_EXPR = re.compile(r"env\.([A-Za-z_]\w*)")
_HELM_VALUED = (
    "-f",
    "--values",
    "--set",
    "--set-string",
    "--set-file",
    "--set-json",
    "-n",
    "--namespace",
    "--version",
    "--timeout",
    "--kube-context",
    "--repo",
    "--description",
)
_DOCKER_VALUED = (
    "-t",
    "--tag",
    "-f",
    "--file",
    "--build-arg",
    "--target",
    "--platform",
    "--label",
    "--secret",
    "--ssh",
    "--cache-from",
    "--cache-to",
    "--network",
    "--progress",
    "--output",
    "-o",
)


# ------------------------------------------------------------------------- the YAML graph
@dataclass
class Scalar:
    text: str
    line: int
    block: bool = False
    null: bool = False


@dataclass
class Mapping_:
    items: dict[str, object]
    line: int
    key_lines: dict[str, int] = field(default_factory=dict)


@dataclass
class Sequence_:
    items: list[object]
    line: int


class _Reader:
    """A composed YAML node as plain values with lines, merges applied, bounded."""

    def __init__(self) -> None:
        self.visited = 0
        self.truncated = False

    def plain(self, node: yaml.Node | None, path: frozenset[int] = frozenset()) -> object:
        if node is None:
            return None
        self.visited += 1
        if self.visited > MAX_NODES or id(node) in path:
            self.truncated = True
            return None
        inner = path | {id(node)}
        line = node.start_mark.line + 1
        if isinstance(node, yaml.ScalarNode):
            return Scalar(
                str(node.value),
                line,
                block=node.style in ("|", ">"),
                null=node.tag == "tag:yaml.org,2002:null",
            )
        if isinstance(node, yaml.SequenceNode):
            return Sequence_([self.plain(item, inner) for item in node.value], line)
        if isinstance(node, yaml.MappingNode):
            items: dict[str, object] = {}
            key_lines: dict[str, int] = {}
            merged: list[yaml.Node] = []
            for key, value in node.value:
                if not isinstance(key, yaml.ScalarNode):
                    continue
                if key.value == "<<" and key.tag == "tag:yaml.org,2002:merge":
                    merged.extend(value.value if isinstance(value, yaml.SequenceNode) else [value])
                    continue
                items[str(key.value)] = self.plain(value, inner)
                key_lines[str(key.value)] = key.start_mark.line + 1
            # Local keys win; among merged mappings, an earlier one wins over a later one.
            for source in merged:
                found = self.plain(source, inner)
                if isinstance(found, Mapping_):
                    for name, value in found.items.items():
                        if name not in items:
                            items[name] = value
                            key_lines[name] = found.key_lines.get(name, found.line)
            return Mapping_(items, line, key_lines)
        return None


def _map(value: object) -> dict[str, object]:
    return value.items if isinstance(value, Mapping_) else {}


def _text(value: object) -> str | None:
    return value.text if isinstance(value, Scalar) and not value.null else None


def _texts(value: object) -> list[str] | None:
    """A scalar or a list of scalars, as texts; ``None`` for anything else."""
    if isinstance(value, Scalar) and not value.null:
        return [value.text]
    if isinstance(value, Sequence_):
        found = [_text(item) for item in value.items]
        return None if any(item is None for item in found) else [str(item) for item in found]
    return None


# ------------------------------------------------------------------------- parts
def scalar_parts(text: str) -> Word:
    """A YAML scalar as parts: CI expressions and shell variables marked, the rest text."""
    parts: Word = []
    cursor = 0
    for match in _SHELL_VAR.finditer(text):
        if match.start() > cursor:
            parts.append([TEXT, text[cursor : match.start()]])
        whole = match.group(0)
        if whole.startswith(("${{", "<<")):
            body = whole[3:-2] if whole.startswith("${{") else whole[2:-2]
            parts.append([WITHHELD, ""] if secret_bearing(body) else [EXPR, body.strip()])
        else:
            parts.append([VAR, match.group(1) or match.group(2)])
        cursor = match.end()
    if cursor < len(text):
        parts.append([TEXT, text[cursor:]])
    return parts


def _guard(name: str, word: Word) -> Word:
    """A value's parts, or one withheld part when its name or its text may carry a secret."""
    text = "".join(value for kind, value in word if kind == TEXT)
    if credential_named(name) or secret_bearing(text) or any(kind == WITHHELD for kind, _ in word):
        return [[WITHHELD, ""]]
    return word


def _environment(value: object) -> dict[str, str]:
    """A literal ``env:`` / ``environment:`` mapping, credential-named entries left out."""
    found: dict[str, str] = {}
    for name, item in _map(value).items():
        text = _text(item)
        if (
            text is not None
            and not credential_named(name)
            and not secret_bearing(text)
            and "$" not in text
        ):
            found[name] = text
    return found


def _names(value: object) -> set[str]:
    """Every name an ``env:`` / ``environment:`` mapping defines, whatever its value."""
    return {str(name) for name in _map(value)}


def _image_names(images: object) -> set[str]:
    """The names CircleCI ``docker:`` images' ``environment`` define."""
    found: set[str] = set()
    for image in images.items if isinstance(images, Sequence_) else []:
        found |= _names(_map(image).get("environment"))
    return found


def symbols(words: Sequence[Word]) -> set[str]:
    """The variables the words read: ``$X``, ``${X}`` and ``${{ env.X }}`` by name, and every
    other CI expression by its text (``github.sha``)."""
    found: set[str] = set()
    for word in words:
        for kind, value in word:
            if kind == VAR:
                found.add(value)
            elif kind == EXPR and value:
                matched = _ENV_EXPR.fullmatch(value)
                found.add(matched.group(1) if matched is not None else value)
    return found


def _overridable(words: Sequence[Word], names: Set[str]) -> str:
    """The variables the words read that a scope or the script may set (E3), as JSON."""
    return json.dumps(sorted(symbols(words) & names))


def _substitute(word: Word, environment: Mapping[str, str]) -> Word:
    """A word with each variable (``$X``, ``${{ env.X }}``) a literal environment sets."""
    out: Word = []
    for kind, value in word:
        name = (
            value
            if kind == VAR
            else (m.group(1) if kind == EXPR and (m := _ENV_EXPR.fullmatch(value)) else None)
        )
        if name is not None and name in environment:
            out.append([TEXT, environment[name]])
        else:
            out.append([kind, value])
    merged: Word = []
    for part in out:
        if merged and part[0] == TEXT and merged[-1][0] == TEXT:
            merged[-1] = [TEXT, merged[-1][1] + part[1]]
        else:
            merged.append(part)
    return merged


def _inside(directory: str, word: Word) -> str | None:
    """A literal path relative to ``directory``, normalised inside the repository."""
    text = literal(word)
    if text is None or not text or text.startswith("/") or any(c in text for c in "*?[{"):
        return None
    joined = posixpath.normpath(posixpath.join(directory or ".", text))
    if joined == ".." or joined.startswith("../"):
        return None
    return "" if joined == "." else joined


def set_pairs(words: Sequence[Word]) -> list[list[object]]:
    """``--set a=b,c=d`` values as ``[path, parts]``; a credential's value withheld."""
    found: list[list[object]] = []
    for word in words:
        pairs: list[Word] = [[]]
        for kind, text in word:
            if kind != TEXT:
                pairs[-1].append([kind, text])
                continue
            pieces = re.split(r"(?<!\\),", text)
            for index, piece in enumerate(pieces):
                if index:
                    pairs.append([])
                if piece:
                    pairs[-1].append([TEXT, piece])
        for pair in pairs:
            if not pair or pair[0][0] != TEXT or "=" not in pair[0][1]:
                if pair:
                    found.append([None, [[EXPR, ""]]])
                continue
            path, _, head = pair[0][1].partition("=")
            value: Word = ([[TEXT, head]] if head else []) + [list(p) for p in pair[1:]]
            found.append([path, _guard(path, value)])
    return found


# ------------------------------------------------------------------------- the reader
@dataclass
class _Build:
    refs: list[Word]
    line: int
    step_line: int
    context: str | None
    dockerfile: str | None
    basis: str
    push: str | None = None  # builtin: "true", "false", "expr"
    pushed_flag: bool = False
    #: Variable names its scopes define (any value) or its script assigns (E3).
    names: frozenset[str] = frozenset()


@dataclass
class _Site:
    """Where a claim is made: the CI system, workflow, job, invocation and step."""

    system: str
    workflow: str
    job: str
    invocation: int
    step: int
    step_line: int
    domains: dict[str, object]
    #: The jobs this one runs after (CircleCI ``requires``, GitHub ``needs``).
    requires: tuple[str, ...] = ()
    #: Whether a step or job condition (``if:``, ``when``) decides if this runs; the
    #: condition's text is never kept.
    conditional: bool = False

    def at(self, step: int, line: int, conditional: bool = False) -> _Site:
        return _Site(
            self.system,
            self.workflow,
            self.job,
            self.invocation,
            step,
            line,
            self.domains,
            self.requires,
            self.conditional or conditional,
        )

    def meta(self) -> dict[str, str]:
        return {
            "system": self.system,
            "workflow": self.workflow,
            "job": self.job,
            "invocation": str(self.invocation),
            "step": str(self.step),
            "step_line": str(self.step_line),
            "domains": json.dumps(self.domains, sort_keys=True),
            "requires": json.dumps(list(self.requires)),
            "conditional": "true" if self.conditional else "false",
        }


class _Claims:
    def __init__(self) -> None:
        self.found: list[ParsedReference] = []
        self.truncated = False

    def add(self, role: str, target: str, line: int, **meta: str) -> None:
        if len(self.found) >= MAX_CLAIMS:
            self.truncated = True
            return
        self.found.append(
            pending_reference(
                EdgeType.REFERENCES, EntityType.CONFIG, target or "-", line, line, role=role, **meta
            )
        )

    def step(self, site: _Site, line: int, kind: str, target: str, **extra: str) -> None:
        self.add(CI_STEP_UNSUPPORTED, target, line, kind=kind, **site.meta(), **extra)


def ci_system(path: PurePosixPath) -> str | None:
    """``circleci`` or ``github`` for a repository-root CI configuration, else ``None``."""
    parts = path.parts
    if parts == (".circleci", "config.yml"):
        return "circleci"
    if (
        len(parts) == 3
        and parts[:2] == (".github", "workflows")
        and path.suffix in (".yml", ".yaml")
    ):
        return "github"
    return None


def ci_references(
    path: PurePosixPath, documents: Sequence[yaml.Node | None]
) -> tuple[list[ParsedReference], bool]:
    """The CI claims of one configuration file, and whether a cap cut them."""
    system = ci_system(path)
    if system is None or not documents or documents[0] is None:
        return [], False
    reader = _Reader()
    root = reader.plain(documents[0])
    claims = _Claims()
    if isinstance(root, Mapping_):
        if system == "github":
            _github(root, claims)
        else:
            _circleci(root, claims)
    truncated = reader.truncated or claims.truncated
    if truncated:
        claims.found = [
            ref.model_copy(update={"meta": {**ref.meta, "truncated": "true"}})
            for ref in claims.found
        ]
    return claims.found, truncated


# ------------------------------------------------------------------------- jobs, shared
class _Job:
    """One job's reading: its images, built and pushed, and the claims of its steps."""

    def __init__(
        self,
        claims: _Claims,
        environment: Mapping[str, str],
        directory: str,
        names: Set[str] = frozenset(),
    ) -> None:
        self.claims = claims
        self.environment = dict(environment)
        self.directory = directory
        #: The names the workflow's and job's scopes define, whatever their values (E3).
        self.names = frozenset(names)
        #: Whether a step writes $GITHUB_ENV or $BASH_ENV, setting variables for later steps.
        self.env_written = False
        self.first = len(claims.found)
        self.builds: list[tuple[_Build, _Site]] = []
        #: Each reference a ``docker push`` names, and the line of the first such push.
        self.pushed: dict[str, int] = {}
        self.aliases: list[tuple[str, Word]] = []

    def run(
        self,
        script: Scalar,
        site: _Site,
        directory: str | None = None,
        step_names: Set[str] = frozenset(),
    ) -> None:
        first = script.line + 1 if script.block else script.line
        read = read_script(script.text, first_line=first)
        where = self.directory if directory is None else directory
        if read.unreadable:
            return
        self.env_written |= read.env_file
        names = self.names | step_names | read.assigned
        nested = False
        for command in read.commands:
            kind = recognised(command)
            if kind is None:
                target = delegated(command) if not command.nested else None
                if target is not None:
                    self.claims.step(site, command.line, "script", target)
                continue
            if command.nested:
                nested = True
                continue
            command = Command(
                tuple(_substitute(w, self.environment) for w in command.words), False, command.line
            )
            if kind == DOCKER_BUILD:
                context_words = positionals(
                    command, 3 if literal(command.words[1]) != "build" else 2, _DOCKER_VALUED
                )
                context = _inside(where, context_words[-1]) if context_words else None
                dockerfile = flag_values(command, "-f", "--file")
                build = _Build(
                    refs=flag_values(command, "-t", "--tag"),
                    line=command.line,
                    step_line=site.step_line,
                    context=context,
                    dockerfile=_inside(where, dockerfile[0])
                    if dockerfile
                    else (
                        _inside(context, [[TEXT, "Dockerfile"]]) if context is not None else None
                    ),
                    basis="source",
                    pushed_flag=has_flag(command, "--push"),
                    names=names,
                )
                self.builds.append((build, site))
            elif kind == DOCKER_PUSH:
                for word in positionals(command, 2, ("-a", "--all-tags")):
                    self.pushed.setdefault(text_of(word), command.line)
            elif kind == DOCKER_TAG:
                words = positionals(command, 2, ())
                if len(words) >= 2:
                    self.aliases.append((text_of(words[0]), words[1]))
            elif kind == HELM:
                self._helm(command, site, where, names)
            elif kind == KUBECTL_APPLY:
                self.claims.step(site, command.line, "kubectl-apply", "kubectl apply")
        if nested:
            self.claims.step(site, site.step_line, "control-flow", "control-flow")
        if read.truncated:
            self.claims.truncated = True

    def _helm(self, command: Command, site: _Site, where: str, names: Set[str]) -> None:
        args = positionals(command, 2, _HELM_VALUED)
        release = args[0] if args else [[EXPR, ""]]
        chart_word = args[1] if len(args) > 1 else [[EXPR, ""]]
        values = [[_inside(where, word), word] for word in flag_values(command, "-f", "--values")]
        namespace = flag_values(command, "-n", "--namespace")
        unread_sets = any(literal(w) in ("--set-file", "--set-json") for w in command.words)
        set_words = flag_values(command, None, "--set") + flag_values(command, None, "--set-string")
        self.claims.add(
            CI_HELM_RELEASE,
            text_of(release),
            command.line,
            basis="source",
            release=json.dumps(release),
            chart=_inside(where, chart_word) or "",
            chart_text=text_of(chart_word),
            values=json.dumps([[p, text_of(w)] for p, w in values]),
            set=json.dumps(set_pairs(set_words)),
            namespace=json.dumps(namespace[0] if namespace else None),
            set_unread="true" if unread_sets else "false",
            overridable=_overridable(
                [release, chart_word, *(w for _, w in values), *set_words], names
            ),
            **site.meta(),
        )

    def builtin(
        self,
        inputs: Mapping[str, object],
        site: _Site,
        line: int,
        step_names: Set[str] = frozenset(),
    ) -> None:
        tags_text = _text(inputs.get("tags")) or ""
        tags = [scalar_parts(tag.strip()) for tag in re.split(r"[\n,]", tags_text) if tag.strip()]
        push = _text(inputs.get("push"))
        state = "false" if push is None else ("expr" if "${{" in push else push.strip().lower())
        context = _text(inputs.get("context"))
        file = _text(inputs.get("file"))
        context_path = (
            _inside(self.directory, scalar_parts(context))
            if context
            else _inside(self.directory, [[TEXT, "."]])
        )
        build = _Build(
            refs=[_substitute(tag, self.environment) for tag in tags],
            line=line,
            step_line=site.step_line,
            context=context_path,
            dockerfile=_inside(self.directory, scalar_parts(file))
            if file
            else (
                _inside(context_path, [[TEXT, "Dockerfile"]]) if context_path is not None else None
            ),
            basis="builtin",
            push="true" if state == "true" else ("expr" if state == "expr" else "false"),
            names=self.names | step_names,
        )
        self.builds.append((build, site))

    def finish(self) -> None:
        """Decide each built reference's publication, job-scoped, and record the builds."""
        pushed = dict(self.pushed)
        for source, alias in self.aliases:
            if text_of(alias) in pushed:
                pushed.setdefault(source, pushed[text_of(alias)])
        for build, site in self.builds:
            refs: list[dict[str, object]] = []
            for word in build.refs or [[]]:
                reference = parse_reference(word) if word else None
                state = reference.state if reference is not None else "image-unresolved"
                if state != "resolved":
                    decision = state
                elif build.basis == "builtin":
                    decision = {"true": "publish-configured", "expr": "publication-unresolved"}.get(
                        str(build.push), "built-not-published"
                    )
                else:
                    published = build.pushed_flag or text_of(word) in pushed
                    decision = "publish-configured" if published else "built-not-published"
                refs.append(
                    {
                        "parts": word,
                        "image": text_of(word) if word else "",
                        "key": reference.join_key() if reference is not None else None,
                        "tag": reference.tag if reference is not None else None,
                        "digest": reference.digest if reference is not None else None,
                        "registry_implicit": bool(reference and reference.registry_implicit),
                        "version_unresolved": bool(reference and reference.version_unresolved),
                        "decision": decision,
                        # DF2: where publishing is configured (the push, or the build's own line).
                        "pushed_line": build.line
                        if build.pushed_flag or build.basis == "builtin"
                        else pushed.get(text_of(word))
                        if word
                        else None,
                    }
                )
            self.claims.add(
                CI_IMAGE_BUILD,
                str(refs[0]["image"]) or "build",
                build.line,
                basis=build.basis,
                refs=json.dumps(refs, sort_keys=True),
                context=build.context or "",
                context_resolved="false" if build.context is None else "true",
                dockerfile=build.dockerfile or "",
                overridable=_overridable(build.refs, build.names),
                **site.meta(),
            )
        self.builds.clear()
        # A step writing $GITHUB_ENV or $BASH_ENV may set any variable for the steps after
        # it: every build and release of the job says so (E3).
        written = "true" if self.env_written else "false"
        self.claims.found[self.first :] = [
            ref.model_copy(update={"meta": {**ref.meta, "env_written": written}})
            if ref.meta.get(PENDING_FACT) in (CI_IMAGE_BUILD, CI_HELM_RELEASE)
            else ref
            for ref in self.claims.found[self.first :]
        ]


# ------------------------------------------------------------------------- GitHub Actions
def _triggers(on: object) -> tuple[set[str], dict[str, object]]:
    """The workflow's triggers, and the literal branches its pushes are limited to."""
    if isinstance(on, Scalar):
        return {on.text}, {}
    if isinstance(on, Sequence_):
        return {item.text for item in on.items if isinstance(item, Scalar)}, {}
    names = set(_map(on))
    push = _map(_map(on).get("push"))
    branches = _texts(push.get("branches")) if "branches" in push else None
    domains: dict[str, object] = {}
    if (
        names == {"push"}
        and branches is not None
        and not any(c in b for b in branches for c in "*?[!")
    ):
        domains["branches"] = sorted(branches)
    return names, domains


def _github(root: Mapping_, claims: _Claims) -> None:
    items = root.items
    names, domains = _triggers(items.get("on", items.get("true")))
    definition = names == {"workflow_call"}
    workflow = _text(items.get("name")) or "workflow"
    environment = _environment(items.get("env"))
    names = _names(items.get("env"))
    defaults = _text(_map(_map(items.get("defaults")).get("run")).get("working-directory")) or ""
    for job_index, (job_name, job_value) in enumerate(_map(items.get("jobs")).items()):
        if job_index >= MAX_JOBS:
            claims.truncated = True
            break
        job = _map(job_value)
        job_line = job_value.line if isinstance(job_value, Mapping_) else 1
        needs = tuple(_texts(job.get("needs")) or ())
        site = _Site("github", workflow, job_name, 0, -1, job_line, domains, needs, "if" in job)
        called = _text(job.get("uses"))
        if called is not None:
            claims.step(site, job_line, "reusable-workflow", called, action=called)
            continue
        job_directory = (
            _text(_map(_map(job.get("defaults")).get("run")).get("working-directory")) or defaults
        )
        reading = _Job(
            claims,
            {**environment, **_environment(job.get("env"))},
            _inside("", scalar_parts(job_directory)) or "" if job_directory else "",
            names | _names(job.get("env")) | _names(_map(job.get("container")).get("env")),
        )
        steps = job.get("steps")
        for index, step_value in enumerate(steps.items if isinstance(steps, Sequence_) else []):
            if index >= MAX_STEPS:
                claims.truncated = True
                break
            step = _map(step_value)
            line = step_value.line if isinstance(step_value, Mapping_) else job_line
            here = site.at(index, line, "if" in step)
            uses = _text(step.get("uses"))
            run = step.get("run")
            if uses is not None:
                kind = classify_action(uses)
                if kind == INERT_ACTION:
                    continue
                if kind == BUILTIN_BUILD and not definition:
                    reading.builtin(_map(step.get("with")), here, line, _names(step.get("env")))
                    continue
                claims.step(here, line, action_kind(uses), uses, action=uses)
            elif isinstance(run, Scalar):
                shell = _text(step.get("shell"))
                if shell is not None and shell.split()[0] not in _SHELLS:
                    continue
                if definition:
                    if any(recognised(c) for c in read_script(run.text).commands):
                        claims.step(here, line, "caller-dependent", "caller-dependent")
                    continue
                step_directory = _text(step.get("working-directory"))
                saved = dict(reading.environment)
                reading.environment.update(_environment(step.get("env")))
                reading.run(
                    run,
                    here,
                    _inside(reading.directory, scalar_parts(step_directory))
                    if step_directory
                    else None,
                    _names(step.get("env")),
                )
                reading.environment = saved
        reading.finish()


# ------------------------------------------------------------------------- CircleCI
def _circleci(root: Mapping_, claims: _Claims) -> None:
    items = root.items
    if (_text(items.get("setup")) or "").lower() == "true":
        line = root.key_lines.get("setup", 1)
        claims.step(_Site("circleci", "-", "-", 0, -1, line, {}), line, "dynamic-config", "setup")
        return
    orbs = _map(items.get("orbs"))
    references = {alias: _text(value) for alias, value in orbs.items() if _text(value) is not None}
    inline = {alias: _map(value) for alias, value in orbs.items() if isinstance(value, Mapping_)}
    jobs: dict[str, object] = dict(_map(items.get("jobs")))
    commands: dict[str, object] = dict(_map(items.get("commands")))
    executors: dict[str, object] = dict(_map(items.get("executors")))
    for alias, body in inline.items():
        jobs.update({f"{alias}/{name}": value for name, value in _map(body.get("jobs")).items()})
        commands.update(
            {f"{alias}/{name}": value for name, value in _map(body.get("commands")).items()}
        )
        # An inline orb's job names its own executors unprefixed.
        executors.update(
            {f"{alias}/{name}": value for name, value in _map(body.get("executors")).items()}
        )
        executors.update(dict(_map(body.get("executors"))))
    for workflow_name, workflow in _map(items.get("workflows")).items():
        listed = _map(workflow).get("jobs")
        for invocation, entry in enumerate(listed.items if isinstance(listed, Sequence_) else []):
            if invocation >= MAX_JOBS:
                claims.truncated = True
                break
            name, params, line = _invocation(entry)
            if name is None:
                continue
            domains = _filters(params.get("filters"))
            requires = _texts(params.get("requires")) or []
            site = _Site(
                "circleci", workflow_name, name, invocation, -1, line, domains, tuple(requires)
            )
            if name in jobs:
                _circle_job(jobs[name], params, site, claims, commands, references, executors)
                continue
            alias, _, job = name.partition("/")
            if alias in references:
                parameters = {
                    key: _guard(key, scalar_parts(text))
                    for key, value in params.items()
                    if key
                    not in (
                        "requires",
                        "filters",
                        "context",
                        "name",
                        "matrix",
                        "pre-steps",
                        "post-steps",
                    )
                    and (text := _text(value)) is not None
                }
                claims.step(
                    site,
                    line,
                    "orb-job",
                    f"{references[alias]} {job}",
                    action=str(references[alias]),
                    orb_job=job,
                    parameters=json.dumps(parameters, sort_keys=True),
                )


def _invocation(entry: object) -> tuple[str | None, dict[str, object], int]:
    if isinstance(entry, Scalar):
        return entry.text, {}, entry.line
    if isinstance(entry, Mapping_) and len(entry.items) == 1:
        ((name, params),) = entry.items.items()
        return name, _map(params), entry.line
    return None, {}, 1


def _filters(value: object) -> dict[str, object]:
    """Literal ``only`` branch lists; a regex or an ``ignore`` leaves the domain open."""
    branches = _map(_map(value).get("branches"))
    only = _texts(branches.get("only")) if "only" in branches else None
    if only is None or "ignore" in branches or any(b.startswith("/") for b in only):
        return {}
    return {"branches": sorted(only)}


def _circle_job(
    job_value: object,
    params: Mapping[str, object],
    site: _Site,
    claims: _Claims,
    commands: Mapping[str, object],
    references: Mapping[str, str | None],
    executors: Mapping[str, object],
) -> None:
    job = _map(job_value)
    values = _parameter_values(job.get("parameters"), params)
    directory = _text(job.get("working_directory")) or ""
    directory = (
        "" if directory in ("", "~/project", ".") else (_inside("", scalar_parts(directory)) or "")
    )
    executor = job.get("executor")
    executor_name = _text(executor) or _text(_map(executor).get("name"))
    used = _map(executors.get(executor_name)) if executor_name else _map(None)
    names = (
        _names(job.get("environment"))
        | _image_names(job.get("docker"))
        | _names(used.get("environment"))
        | _image_names(used.get("docker"))
    )
    reading = _Job(claims, _environment(job.get("environment")), directory, names)
    steps = job.get("steps")
    _circle_steps(
        steps.items if isinstance(steps, Sequence_) else [],
        values,
        site,
        reading,
        commands,
        references,
        depth=0,
    )
    reading.finish()


def _parameter_values(declared: object, given: Mapping[str, object]) -> dict[str, str]:
    found: dict[str, str] = {}
    for name, spec in _map(declared).items():
        default = _text(_map(spec).get("default"))
        if default is not None:
            found[name] = default
    for name, value in given.items():
        text = _text(value)
        if text is not None:
            found[name] = text
    return found


def _circle_steps(
    steps: list[object],
    values: Mapping[str, str],
    site: _Site,
    reading: _Job,
    commands: Mapping[str, object],
    references: Mapping[str, str | None],
    depth: int,
) -> None:
    for index, step in enumerate(steps):
        if index >= MAX_STEPS or depth > 4:
            reading.claims.truncated = True
            return
        if isinstance(step, Scalar):
            name, body, line = step.text, None, step.line
        elif isinstance(step, Mapping_) and len(step.items) == 1:
            ((name, body),) = step.items.items()
            line = step.line
        else:
            continue
        here = site.at(index, line, name in ("when", "unless"))
        if name == "run":
            text = _text(body) if isinstance(body, Scalar) else _text(_map(body).get("command"))
            script = body if isinstance(body, Scalar) else _map(body).get("command")
            if text is None or not isinstance(script, Scalar):
                continue
            substituted = _PARAMETER.sub(lambda m: values.get(m.group(1), m.group(0)), text)
            directory = (
                _text(_map(body).get("working_directory")) if isinstance(body, Mapping_) else None
            )
            reading.run(
                Scalar(substituted, script.line, script.block),
                here,
                _inside(reading.directory, scalar_parts(directory)) if directory else None,
                _names(_map(body).get("environment")) if isinstance(body, Mapping_) else set(),
            )
        elif name in _CIRCLE_STEPS:
            continue
        elif name in commands:
            command = _map(commands[name])
            inner = command.get("steps")
            _circle_steps(
                inner.items if isinstance(inner, Sequence_) else [],
                {**values, **_parameter_values(command.get("parameters"), _map(body))},
                here,
                reading,
                commands,
                references,
                depth + 1,
            )
        elif "/" in name and name.partition("/")[0] in references:
            alias, _, command_name = name.partition("/")
            reading.claims.step(
                here,
                line,
                "orb-command",
                f"{references[alias]} {command_name}",
                action=str(references[alias]),
                orb_job=command_name,
            )


# ------------------------------------------------------------------------- declarations
def is_declaration(documents: Sequence[yaml.Node | None]) -> bool:
    if not documents or not isinstance(documents[0], yaml.MappingNode):
        return False
    return any(
        isinstance(key, yaml.ScalarNode)
        and key.value == "repoatlas"
        and isinstance(value, yaml.ScalarNode)
        and value.value == DECLARATIONS_HEADER
        for key, value in documents[0].value
    )


def declaration_references(documents: Sequence[yaml.Node | None]) -> list[ParsedReference]:
    """One claim per declared mapping, valid or not (the global pass decides it)."""
    if not is_declaration(documents):
        return []
    reader = _Reader()
    root = reader.plain(documents[0])
    claims = _Claims()
    if not isinstance(root, Mapping_):
        return []
    orbs = root.items.get("orbs")
    for entry in orbs.items if isinstance(orbs, Sequence_) else []:
        fields = _map(entry)
        line = entry.line if isinstance(entry, Mapping_) else root.line
        orb, job = _text(fields.get("orb")), _text(fields.get("job"))
        mapping, invalid = _mapping(fields)
        if orb is None or job is None:
            invalid = "an orb entry names an exact orb and a job"
        elif not _EXACT_ORB.fullmatch(orb):
            invalid = "the orb is not an exact namespace/name@major.minor.patch"
        claims.add(
            CI_SEMANTICS,
            f"{orb} {job}",
            line,
            kind="orb",
            orb=orb or "",
            orb_job=job or "",
            mapping=json.dumps(mapping, sort_keys=True),
            invalid=invalid or "",
        )
    for slug, entry in _map(root.items.get("projects")).items():
        project = _text(_map(entry).get("circleci-project"))
        line = entry.line if isinstance(entry, Mapping_) else root.line
        claims.add(
            CI_SEMANTICS,
            slug,
            line,
            kind="project",
            project=project or "",
            invalid="" if project else "a project names its circleci-project",
        )
    return claims.found


_RELEASE_FIELDS = frozenset({"chart", "release", "namespace", "set", "values"})
_BUILD_FIELDS = frozenset({"image", "publish", "context", "dockerfile"})


def _mapping(fields: Mapping[str, object]) -> tuple[dict[str, object], str | None]:
    """A declared mapping's fields (``helm-release`` or ``image-build``), or why invalid."""
    kinds = [kind for kind in ("helm-release", "image-build") if kind in fields]
    unknown = set(fields) - {"orb", "job", "helm-release", "image-build"}
    if len(kinds) != 1 or unknown:
        return {}, "an orb entry has exactly one of helm-release or image-build, and nothing else"
    kind = kinds[0]
    allowed = _RELEASE_FIELDS if kind == "helm-release" else _BUILD_FIELDS
    mapping: dict[str, object] = {"kind": kind}
    for name, spec in _map(fields[kind]).items():
        if name not in allowed:
            return {}, f"{kind} has no field {name!r}"
        if name == "publish":
            text = _text(spec)
            if text not in ("true", "false"):
                return {}, "publish is true or false"
            mapping[name] = text
            continue
        parameter = _text(_map(spec).get("parameter"))
        shape = _text(_map(spec).get("format"))
        if parameter is None or (shape not in (None, "comma-separated")):
            return {}, f"{kind}.{name} names a parameter, and only the comma-separated format"
        mapping[name] = {"parameter": parameter, "format": shape or "single"}
    required = "chart" if kind == "helm-release" else "image"
    if required not in mapping:
        return {}, f"{kind} maps {required}"
    return mapping, None
