"""Salt SLS parser (V1_SPEC §7.5).

An ``.sls`` file is **not ordinary YAML**: it is Jinja-templated YAML, and a large
fraction of real state files do not parse as YAML until the Jinja layer is
neutralized. The strategy the specification requires, in order:

1. neutralize Jinja into same-length filler (``jinja.neutralize``), so line and
   column positions stay identical to the original file;
2. parse the remainder as YAML, leniently;
3. extract Jinja facts separately from the *raw* text;
4. if YAML still fails, fall back to a line-oriented scan that recovers state IDs
   and requisites at reduced confidence, and record a diagnostic.

Templates are neutralized, never rendered (V1_SPEC §17).

**Provenance.** A state's span ends on the last source line structurally belonging
to that state. Sibling states, top-level Jinja controls, trailing comments and
separator whitespace are not evidence for the state above them; Jinja written
inside a state's value is. Because step 1 preserves every character position,
marks taken from the neutralized text index the raw file exactly — which is what
lets :func:`~repoatlas.parsers.marks.node_span` read raw lines against neutralized
marks (ARCHITECTURE.md §5).
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import yaml

from repoatlas.models.parsing import (
    EdgeType,
    EntityType,
    ParsedFile,
    ParseDiagnostic,
    ParsedReference,
    ParsedSymbol,
    Severity,
)
from repoatlas.parsers.http_facts import PENDING_FACT
from repoatlas.parsers.jinja import extract_references, neutralize
from repoatlas.parsers.marks import diagnostic_line, node_span, split_lines
from repoatlas.parsers.structured import collect_config_keys

_SUFFIXES = frozenset({".sls"})

#: Requisite keys and the relationship each one asserts.
_REQUISITES: dict[str, EdgeType] = {
    "require": EdgeType.REQUIRES,
    "require_in": EdgeType.REQUIRES,
    "watch": EdgeType.WATCHES,
    "watch_in": EdgeType.WATCHES,
    "onchanges": EdgeType.WATCHES,
    "onchanges_in": EdgeType.WATCHES,
    "onfail": EdgeType.WATCHES,
    "onfail_in": EdgeType.WATCHES,
    "prereq": EdgeType.REQUIRES,
    "prereq_in": EdgeType.REQUIRES,
    "use": EdgeType.REQUIRES,
    "use_in": EdgeType.REQUIRES,
}

#: Blocks whose ALL_CAPS keys name environment variables the state determines.
#: Deliberately narrow: these are the Salt constructs that actually place a value
#: into a rendered file or a process environment.
_ENVIRONMENT_BLOCKS = frozenset({"context", "environ", "env", "environment", "defaults"})

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]+$")
_STATE_FUNCTION = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")

_FALLBACK_STATE_ID = re.compile(r"^(?P<id>[^\s#{][^:]*):\s*$")
_FALLBACK_FUNCTION = re.compile(r"^\s{1,4}(?P<function>[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*):\s*$")
_FALLBACK_REQUISITE = re.compile(
    r"^\s*-\s*(?P<module>[a-z_][a-z0-9_]*)\s*:\s*(?P<target>[^\s#][^#]*?)\s*$"
)

#: Reduced confidence for facts recovered without a successful YAML parse.
_FALLBACK_CONFIDENCE = 0.6
_SAFE_SLS = re.compile(r"^[A-Za-z0-9_.-]+$")


def _scalar(node: yaml.Node) -> str | None:
    return node.value if isinstance(node, yaml.ScalarNode) else None


def _top_file_references(root: yaml.Node, lines: list[str]) -> list[ParsedReference]:
    """Record literal top-file environment, target, and SLS assignments."""
    if not isinstance(root, yaml.MappingNode):
        return []
    references: list[ParsedReference] = []
    for environment_node, targets_node in root.value:
        environment = _scalar(environment_node)
        if (
            environment is None
            or not _SAFE_SLS.fullmatch(environment)
            or not isinstance(targets_node, yaml.MappingNode)
        ):
            continue
        for selector_node, entries_node in targets_node.value:
            selector = _scalar(selector_node)
            if selector is None or "{{" in selector or "{%" in selector:
                continue
            if not isinstance(entries_node, yaml.SequenceNode):
                continue
            matcher = "glob"
            sls_entries: list[yaml.Node] = []
            for entry in entries_node.value:
                if isinstance(entry, yaml.ScalarNode):
                    sls_entries.append(entry)
                elif isinstance(entry, yaml.MappingNode):
                    for key_node, value_node in entry.value:
                        if _scalar(key_node) == "match":
                            value = _scalar(value_node)
                            if value is not None:
                                matcher = value
            for entry in sls_entries:
                sls = _scalar(entry)
                if sls is None or not _SAFE_SLS.fullmatch(sls):
                    continue
                start, end = node_span(entry, lines)
                references.append(
                    ParsedReference(
                        edge_type=EdgeType.CONFIGURES,
                        target_kind=EntityType.CONFIG,
                        target_key=f"salt-top:{environment}:{matcher}:{selector}:{sls}",
                        line_start=start,
                        line_end=end,
                        meta={
                            PENDING_FACT: "salt-top-entry",
                            "salt_fact": "top_entry",
                            "environment": environment,
                            "selector": selector,
                            "matcher": matcher,
                            "sls": sls,
                        },
                    )
                )
    return references


def _master_root_references(root: yaml.Node, lines: list[str]) -> list[ParsedReference]:
    """Record statically declared Salt state and pillar roots by environment."""
    if not isinstance(root, yaml.MappingNode):
        return []
    references: list[ParsedReference] = []
    roles = {"file_roots": "state", "pillar_roots": "pillar"}
    for key_node, environments_node in root.value:
        role = roles.get(_scalar(key_node) or "")
        if role is None or not isinstance(environments_node, yaml.MappingNode):
            continue
        for environment_node, paths_node in environments_node.value:
            environment = _scalar(environment_node)
            if environment is None or not _SAFE_SLS.fullmatch(environment):
                continue
            if not isinstance(paths_node, yaml.SequenceNode):
                continue
            for path_node in paths_node.value:
                salt_path = _scalar(path_node)
                if (
                    salt_path is None
                    or not PurePosixPath(salt_path).is_absolute()
                    or ".." in PurePosixPath(salt_path).parts
                    or "$" in salt_path
                    or "{" in salt_path
                ):
                    continue
                start, end = node_span(path_node, lines)
                normalized = PurePosixPath(salt_path).as_posix()
                references.append(
                    ParsedReference(
                        edge_type=EdgeType.CONFIGURES,
                        target_kind=EntityType.CONFIG,
                        target_key=f"salt-master:{role}:{environment}:{normalized}",
                        line_start=start,
                        line_end=end,
                        meta={
                            PENDING_FACT: "salt-master-root",
                            "salt_fact": "master_root",
                            "role": role,
                            "environment": environment,
                            "salt_path": normalized,
                        },
                    )
                )
    return references


class _Collector:
    """Turns one composed YAML document into Salt symbols and references."""

    def __init__(self, lines: list[str]) -> None:
        #: The **raw** file, not the neutralized copy. Neutralization preserves
        #: positions, so marks index these lines exactly — and a line that only
        #: looks blank after neutralization still counts as content.
        self._lines = lines
        self.symbols: list[ParsedSymbol] = []
        self.references: list[ParsedReference] = []

    def _span(self, node: yaml.Node) -> tuple[int, int]:
        return node_span(node, self._lines)

    def document(self, root: yaml.Node) -> None:
        if not isinstance(root, yaml.MappingNode):
            return
        for key_node, value_node in root.value:
            key = _scalar(key_node)
            if key is None:
                continue
            if key == "include":
                self._include(value_node)
            elif key == "extend":
                self._extend(value_node)
            else:
                self._state(key, key_node, value_node)

    # -- top-level constructs ------------------------------------------------

    def _include(self, node: yaml.Node) -> None:
        if not isinstance(node, yaml.SequenceNode):
            return
        for item in node.value:
            target = _scalar(item)
            if target is None:
                continue
            start, end = self._span(item)
            self.references.append(
                ParsedReference(
                    edge_type=EdgeType.INCLUDES,
                    target_kind=EntityType.SALT_STATE,
                    target_key=target,
                    line_start=start,
                    line_end=end,
                    meta={"via": "include"},
                )
            )

    def _extend(self, node: yaml.Node) -> None:
        if not isinstance(node, yaml.MappingNode):
            return
        for key_node, _ in node.value:
            target = _scalar(key_node)
            if target is None:
                continue
            start, end = self._span(key_node)
            self.references.append(
                ParsedReference(
                    edge_type=EdgeType.REFERENCES,
                    target_kind=EntityType.SALT_STATE,
                    target_key=target,
                    line_start=start,
                    line_end=end,
                    meta={"via": "extend"},
                )
            )

    def _state(self, state_id: str, key_node: yaml.Node, value_node: yaml.Node) -> None:
        """A top-level key is either a state declaration or pillar-style data.

        An ``.sls`` file may be either; Salt itself distinguishes them only through
        the top file. The distinction that *is* in the text is structural: a state
        declares a ``module.function`` (or a short-form module with a list body),
        while data does not. Data subtrees are indexed as configuration keys, which
        is what lets a ``pillar['service']['host']`` reference resolve to the line
        that defines it.
        """
        start, _ = self._span(key_node)
        _, end = self._span(value_node)
        functions = self._state_functions(value_node)

        if not functions and not self._is_state(value_node):
            keys, _truncated = collect_config_keys(value_node, state_id, lines=self._lines)
            self.symbols.append(
                ParsedSymbol(
                    kind=EntityType.CONFIG,
                    name=state_id,
                    qualified_name=state_id,
                    line_start=start,
                    line_end=max(end, start),
                    signature=state_id,
                )
            )
            self.symbols.extend(keys)
            return

        self.symbols.append(
            ParsedSymbol(
                kind=EntityType.SALT_STATE,
                name=state_id,
                qualified_name=state_id,
                line_start=start,
                line_end=max(end, start),
                signature=f"{state_id}: {functions[0]}" if functions else state_id,
                meta={"functions": ",".join(functions)} if functions else {},
            )
        )

        if not isinstance(value_node, yaml.MappingNode):
            return
        for function_node, body in value_node.value:
            function = _scalar(function_node)
            if function is None:
                continue
            if _STATE_FUNCTION.match(function):
                self._state_function(state_id, function, function_node)
                self._arguments(state_id, function, body)
            elif isinstance(body, yaml.SequenceNode):
                # Short-declaration form: ``state_id: {module: [function, ...]}``
                self._arguments(state_id, function, body)

    def _is_state(self, value_node: yaml.Node) -> bool:
        """Short-declaration form: ``state_id: {module: [function, args...]}``."""
        if not isinstance(value_node, yaml.MappingNode):
            return False
        return any(isinstance(body, yaml.SequenceNode) for _, body in value_node.value)

    def _state_functions(self, value_node: yaml.Node) -> list[str]:
        if not isinstance(value_node, yaml.MappingNode):
            return []
        return [
            name
            for key_node, _ in value_node.value
            if (name := _scalar(key_node)) is not None and _STATE_FUNCTION.match(name)
        ]

    def _state_function(self, state_id: str, function: str, node: yaml.Node) -> None:
        start, end = self._span(node)
        self.references.append(
            ParsedReference(
                edge_type=EdgeType.REFERENCES,
                target_kind=EntityType.COMMAND,
                target_key=f"salt.state:{function}",
                line_start=start,
                line_end=end,
                source_symbol=state_id,
                meta={"module": function.split(".", 1)[0], "role": "state_function"},
            )
        )

    # -- state arguments -----------------------------------------------------

    def _arguments(self, state_id: str, function: str, body: yaml.Node) -> None:
        if not isinstance(body, yaml.SequenceNode):
            return
        for item in body.value:
            if not isinstance(item, yaml.MappingNode):
                continue
            for key_node, value_node in item.value:
                key = _scalar(key_node)
                if key is None:
                    continue
                if key in _REQUISITES:
                    self._requisite(state_id, key, value_node)
                elif key in {"source", "sources"}:
                    self._source(state_id, value_node)
                elif key in _ENVIRONMENT_BLOCKS or (key == "name" and function == "environ.setenv"):
                    self._environment(state_id, key, value_node)

    def _requisite(self, state_id: str, key: str, node: yaml.Node) -> None:
        if not isinstance(node, yaml.SequenceNode):
            return
        edge_type = _REQUISITES[key]
        for item in node.value:
            for module, target, target_node in self._requisite_targets(item):
                start, end = self._span(target_node)
                self.references.append(
                    ParsedReference(
                        edge_type=edge_type,
                        target_kind=EntityType.SALT_STATE,
                        target_key=target,
                        line_start=start,
                        line_end=end,
                        source_symbol=state_id,
                        meta={"requisite": key, **({"module": module} if module else {})},
                    )
                )

    def _requisite_targets(self, item: yaml.Node) -> list[tuple[str | None, str, yaml.Node]]:
        """``- pkg: name`` and the bare ``- name`` form.

        An empty target — ``- pkg:`` with nothing after it — names no state, so it
        yields no reference. Recording one would create an edge to the empty
        string, which is a relationship RepoAtlas cannot support with evidence.
        """
        if isinstance(item, yaml.ScalarNode):
            return [(None, item.value, item)] if item.value.strip() else []
        if not isinstance(item, yaml.MappingNode):
            return []
        found: list[tuple[str | None, str, yaml.Node]] = []
        for key_node, value_node in item.value:
            module = _scalar(key_node)
            target = _scalar(value_node)
            if target is not None and target.strip():
                found.append((module, target, value_node))
        return found

    def _source(self, state_id: str, node: yaml.Node) -> None:
        candidates = (
            node.value if isinstance(node, yaml.SequenceNode) else [node]  # type: ignore[list-item]
        )
        for candidate in candidates:
            target = _scalar(candidate)
            if target is None or "_" * 4 in target:
                continue
            start, end = self._span(candidate)
            self.references.append(
                ParsedReference(
                    edge_type=EdgeType.REFERENCES,
                    target_kind=EntityType.TEMPLATE,
                    target_key=target,
                    line_start=start,
                    line_end=end,
                    source_symbol=state_id,
                    meta={"role": "source"},
                )
            )

    def _environment(self, state_id: str, block: str, node: yaml.Node) -> None:
        """ALL_CAPS keys in a context/environ block are environment variables.

        This is the one place a Salt state is read as *determining* a variable's
        value, so the trigger is deliberately narrow: an upper-snake-case mapping
        key directly inside one of ``_ENVIRONMENT_BLOCKS``.
        """
        if not isinstance(node, yaml.MappingNode):
            return
        for key_node, _ in node.value:
            name = _scalar(key_node)
            if name is None or not _ENV_NAME.match(name):
                continue
            start, end = self._span(key_node)
            self.references.append(
                ParsedReference(
                    edge_type=EdgeType.CONFIGURES,
                    target_kind=EntityType.ENV_VAR,
                    target_key=name,
                    line_start=start,
                    line_end=end,
                    source_symbol=state_id,
                    meta={"block": block},
                )
            )


def _block_end(lines: list[str], start: int) -> int:
    """The last line of the indented block opened at 1-based ``start``.

    The recovery path's answer to the same question the YAML path answers from
    node content: a top-level construct owns the lines beneath it that are blank
    or indented, and ends at the last one that is neither blank nor a comment.
    Indentation cannot prove the *nesting* a YAML parse would have — which is why
    these facts keep their reduced confidence — but it does prove where the block
    stops, and a one-line span for a six-line state understates the evidence just
    as a six-line span for a one-line state overstates it.
    """
    end = start
    for number in range(start + 1, len(lines) + 1):
        text = lines[number - 1]
        if text.strip() and not text[0].isspace():
            break
        if text.strip() and not text.lstrip().startswith("#"):
            end = number
    return end


def _fallback_scan(content: str) -> tuple[list[ParsedSymbol], list[ParsedReference]]:
    """Line-oriented recovery when the neutralized text is still not YAML.

    Recovers state IDs and requisites only, at reduced confidence, because
    indentation alone cannot prove the nesting the YAML parser would have.

    Split with the source-grammar splitter, not ``str.splitlines()``. This path is
    reached precisely because PyYAML *refused* the content, so PyYAML's line-break
    set no longer governs the numbering — and one of the two things it refuses
    outright is a form feed, which ``splitlines()`` would count as a line break and
    every state after it would then be reported one line too low.
    """
    lines = split_lines(content)
    symbols: list[ParsedSymbol] = []
    references: list[ParsedReference] = []
    current: str | None = None
    pending: str | None = None

    for number, line in enumerate(lines, start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        state_match = _FALLBACK_STATE_ID.match(line)
        if state_match and not line[0].isspace():
            pending = None
            state_id = state_match.group("id").strip()
            if state_id in {"include", "extend"}:
                current = None
                continue
            if "{{" in state_id or "{%" in state_id:
                # A templated ID names nothing another file can reference, and the
                # raw text remains searchable through chunks either way.
                current = None
                continue
            current = state_id
            symbols.append(
                ParsedSymbol(
                    kind=EntityType.SALT_STATE,
                    name=state_id,
                    qualified_name=state_id,
                    line_start=number,
                    line_end=_block_end(lines, number),
                    meta={"recovery": "line-scan"},
                )
            )
            continue

        if _FALLBACK_FUNCTION.match(line):
            pending = None
            continue

        requisite_key = line.strip().rstrip(":")
        if requisite_key.lstrip("- ") in _REQUISITES and line.strip().endswith(":"):
            pending = requisite_key.lstrip("- ")
            continue

        if pending and current:
            requisite_match = _FALLBACK_REQUISITE.match(line)
            if requisite_match:
                references.append(
                    ParsedReference(
                        edge_type=_REQUISITES[pending],
                        target_kind=EntityType.SALT_STATE,
                        target_key=requisite_match.group("target").strip(),
                        line_start=number,
                        line_end=number,
                        confidence=_FALLBACK_CONFIDENCE,
                        source_symbol=current,
                        meta={
                            "requisite": pending,
                            "module": requisite_match.group("module"),
                            "recovery": "line-scan",
                        },
                    )
                )
                continue
            pending = None

    return symbols, references


class SaltParser:
    """Deterministic Salt SLS extraction."""

    name = "salt-sls"
    version = "3"  # 3: literal top-file mappings are retained as linker claims
    language = "Salt"

    def supports(self, path: Path) -> bool:
        return path.suffix in _SUFFIXES

    def parse(self, path: Path, content: str) -> ParsedFile:
        neutralized = neutralize(content)
        diagnostics: list[ParseDiagnostic] = []
        # ``splitlines()`` deliberately: this array is indexed by PyYAML marks, so
        # it must use PyYAML's line-break set, not the source-grammar one. They
        # differ only on characters PyYAML either rejects (form feed, vertical tab)
        # or breaks on exactly as ``splitlines()`` does (U+0085, U+2028, U+2029).
        collector = _Collector(content.splitlines())

        try:
            documents = list(yaml.compose_all(neutralized, Loader=yaml.SafeLoader))
        except yaml.YAMLError as exc:
            symbols, references = _fallback_scan(content)
            mark = getattr(exc, "problem_mark", None)
            failed_at = diagnostic_line(mark, content)
            diagnostics.append(
                ParseDiagnostic(
                    severity=Severity.WARNING,
                    message=(
                        "YAML parse failed after Jinja neutralization; "
                        "recovered state IDs and requisites by line scan"
                    ),
                    line=failed_at,
                )
            )
        else:
            for document in documents:
                if document is not None:
                    if path.name == "top.sls":
                        collector.references.extend(
                            _top_file_references(document, content.splitlines())
                        )
                    else:
                        collector.document(document)
            symbols, references = collector.symbols, collector.references

        references = [*references, *extract_references(content)]

        return ParsedFile(
            language=self.language,
            parser=self.name,
            parser_version=self.version,
            symbols=tuple(symbols),
            references=tuple(references),
            diagnostics=tuple(diagnostics),
        )


class SaltMasterParser:
    """Static root declarations from an indexed Salt master configuration."""

    name = "salt-master"
    version = "1"
    language = "Salt"

    def supports(self, path: Path) -> bool:
        return path.name == "master" or (path.parent.name == "master.d" and path.suffix == ".conf")

    def parse(self, path: Path, content: str) -> ParsedFile:
        diagnostics: tuple[ParseDiagnostic, ...] = ()
        try:
            root = yaml.compose(content, Loader=yaml.SafeLoader)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            diagnostics = (
                ParseDiagnostic(
                    severity=Severity.WARNING,
                    message="Salt master YAML could not be parsed for root mappings",
                    line=diagnostic_line(mark, content),
                ),
            )
            root = None
        references = _master_root_references(root, content.splitlines()) if root is not None else []
        return ParsedFile(
            language=self.language,
            parser=self.name,
            parser_version=self.version,
            references=tuple(references),
            diagnostics=diagnostics,
        )
