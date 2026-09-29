"""Python dependency parsers (V1_SPEC §13).

Covers ``pyproject.toml``, ``requirements*.txt``, ``setup.py``, ``setup.cfg``,
``uv.lock`` and ``poetry.lock``.

``setup.py`` is **inspected with ``ast``, never imported**. Importing it would
execute arbitrary code from an untrusted repository at index time, which V1_SPEC
§17 forbids outright — so only literal values reachable in the syntax tree are
recorded, and a computed ``install_requires`` yields a diagnostic instead of a
guess.
"""

from __future__ import annotations

import ast
import configparser
import re
import tomllib
from pathlib import Path

from repoatlas.dependencies.normalize import PYPI, normalize, python_requirement
from repoatlas.models.dependencies import (
    DeclaredDependency,
    DependencyKind,
    DependencyReport,
    ProvidedPackage,
    ResolvedDependency,
)
from repoatlas.models.parsing import (
    INVALID_CFG,
    INVALID_TOML,
    PARSE_FAILED,
    ParseDiagnostic,
    Severity,
)
from repoatlas.parsers.interpreter import interpreter_tag
from repoatlas.parsers.marks import line_at, split_lines
from repoatlas.parsers.setuptools_project import cfg_values

_REQUIREMENTS = re.compile(r"^requirements.*\.txt$")
_OPTIONS = frozenset({"-r", "--requirement", "-c", "--constraint", "-e", "--editable"})
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _find_line(content: str, needle: str, start: int = 0) -> int:
    index = content.find(needle, start)
    return line_at(content, index) if index != -1 else 1


class _TomlPlaces:
    """Where each key's value and each array element starts in a TOML document, by key path.

    ``tomllib`` gives values but no positions, and searching the file for a value's text
    finds ``"flask"`` in ``name = "flaskr"``, or ``name = "app"`` in another table. This
    walks the document's syntax instead -- table headers, dotted and quoted keys, inline
    tables, arrays, the four kinds of string, comments -- and records where each value
    starts, with the string it holds. It only ever reads text ``tomllib`` has accepted, so
    it diagnoses nothing.
    """

    def __init__(self, content: str) -> None:
        self._text = content
        self._at = 0
        #: Each key's value, as (offset, the string it holds or ``None``).
        self.values: dict[tuple[str, ...], tuple[int, str | None]] = {}
        #: Each array's elements, as (offset, the string it holds or ``None``).
        self.items: dict[tuple[str, ...], list[tuple[int, str | None]]] = {}

    def walk(self) -> _TomlPlaces:
        table: tuple[str, ...] = ()
        text = self._text
        while True:
            self._skip(newlines=True)
            if self._at >= len(text):
                return self
            if text[self._at] == "[":
                width = 2 if text.startswith("[[", self._at) else 1
                self._at += width
                table = self._key()
                self._skip()
                self._at += width
            else:
                key = self._key()
                self._skip()
                self._at += 1  # "="
                self._skip()
                self._keyed(table + key)

    def _keyed(self, path: tuple[str, ...]) -> None:
        start = self._at
        self.values[path] = (start, self._value(path))

    def _skip(self, *, newlines: bool = False) -> None:
        text = self._text
        while self._at < len(text):
            char = text[self._at]
            if char in " \t" or (newlines and char in "\r\n"):
                self._at += 1
            elif char == "#":
                while self._at < len(text) and text[self._at] not in "\r\n":
                    self._at += 1
            else:
                return

    def _key(self) -> tuple[str, ...]:
        parts: list[str] = []
        while True:
            self._skip()
            if self._text[self._at] in "\"'":
                parts.append(self._string())
            else:
                found = _BARE_KEY.match(self._text, self._at)
                if found is None:
                    raise ValueError("key expected")
                parts.append(found.group())
                self._at = found.end()
            self._skip()
            if self._text[self._at] != ".":
                return tuple(parts)
            self._at += 1

    def _value(self, path: tuple[str, ...]) -> str | None:
        text = self._text
        char = text[self._at]
        if char in "\"'":
            return self._string()
        if char == "[":
            self._at += 1
            items: list[tuple[int, str | None]] = []
            self.items[path] = items
            while True:
                self._skip(newlines=True)
                if text[self._at] == "]":
                    self._at += 1
                    return None
                start = self._at
                items.append((start, self._value((*path, str(len(items))))))
                self._skip(newlines=True)
                if text[self._at] == ",":
                    self._at += 1
        if char == "{":
            self._at += 1
            while True:
                self._skip()
                if text[self._at] == "}":
                    self._at += 1
                    return None
                key = self._key()
                self._skip()
                self._at += 1  # "="
                self._skip()
                self._keyed(path + key)
                self._skip()
                if text[self._at] == ",":
                    self._at += 1
        while self._at < len(text) and text[self._at] not in ",]}#\r\n":
            self._at += 1  # a number, a boolean or a date
        return None

    def _string(self) -> str:
        """The string starting here, decoded as ``tomllib`` decodes it."""
        text, start = self._text, self._at
        quote = text[start]
        if text.startswith(quote * 3, start):
            at = start + 3
            while True:
                if quote == '"' and text[at] == "\\":
                    at += 2
                elif text.startswith(quote * 3, at):
                    run = 3
                    while run < 5 and at + run < len(text) and text[at + run] == quote:
                        run += 1
                    at += run
                    break
                else:
                    at += 1
        else:
            at = start + 1
            while text[at] != quote:
                at += 2 if quote == '"' and text[at] == "\\" else 1
            at += 1
        self._at = at
        return str(tomllib.loads(f"v = {text[start:at]}")["v"])


def _value_line(content: str, place: tuple[int, str | None] | None, value: str) -> int:
    """The line a value starts on, when it holds the ``value`` read there."""
    if place is not None and place[1] == value:
        return line_at(content, place[0])
    return 1


def _item_line(content: str, items: list[tuple[int, str | None]], index: int, value: str) -> int:
    """The line array element ``index`` starts on, when it holds the ``value`` read there."""
    return _value_line(content, items[index] if index < len(items) else None, value)


def _declared(
    name: str, constraint: str | None, group: str, path: Path, line: int
) -> DeclaredDependency:
    return DeclaredDependency(
        ecosystem=PYPI,
        name=name,
        normalized_name=normalize(PYPI, name),
        constraint=constraint,
        group=group,
        manifest_path=path.as_posix(),
        line=line,
    )


class PyProjectParser:
    """``pyproject.toml`` — PEP 621 plus Poetry's own table."""

    ecosystem = PYPI
    kind: DependencyKind = "manifest"
    name = "pyproject"
    # 2: line numbers follow the shared source-line model. 3: a PEP 621 requirement's
    # line is its own array element's, read from the TOML syntax. 4: so is the line of
    # the name [project] provides. +pyX.Y: read with the running interpreter's standard
    # library (repoatlas.parsers.interpreter).
    version = f"4+{interpreter_tag()}"

    def supports(self, path: Path) -> bool:
        return path.name == "pyproject.toml"

    def parse(self, path: Path, content: str) -> DependencyReport:
        try:
            document = tomllib.loads(content)
        except tomllib.TOMLDecodeError:
            return DependencyReport(
                ecosystem=self.ecosystem,
                kind="manifest",
                diagnostics=(ParseDiagnostic(severity=Severity.WARNING, message=INVALID_TOML),),
            )

        declared: list[DeclaredDependency] = []
        provides: list[ProvidedPackage] = []

        project = document.get("project", {})
        if isinstance(project, dict):
            self._pep621(project, path, content, declared, provides)

        poetry = document.get("tool", {})
        poetry = poetry.get("poetry", {}) if isinstance(poetry, dict) else {}
        if isinstance(poetry, dict):
            self._poetry(poetry, path, content, declared, provides)

        return DependencyReport(
            ecosystem=self.ecosystem,
            kind="manifest",
            declared=tuple(declared),
            provides=tuple(provides),
        )

    def _pep621(
        self,
        project: dict[str, object],
        path: Path,
        content: str,
        declared: list[DeclaredDependency],
        provides: list[ProvidedPackage],
    ) -> None:
        found = _TomlPlaces(content).walk()
        name = project.get("name")
        if isinstance(name, str):
            version = project.get("version")
            provides.append(
                ProvidedPackage(
                    ecosystem=PYPI,
                    name=name,
                    normalized_name=normalize(PYPI, name),
                    version=version if isinstance(version, str) else None,
                    manifest_path=path.as_posix(),
                    line=_value_line(content, found.values.get(("project", "name")), name),
                )
            )

        dependencies = project.get("dependencies")
        if isinstance(dependencies, list):
            places = found.items.get(("project", "dependencies"), [])
            for index, specifier in enumerate(dependencies):
                self._requirement(specifier, "main", path, content, places, index, declared)

        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group, entries in optional.items():
                if not isinstance(entries, list):
                    continue
                places = found.items.get(("project", "optional-dependencies", group), [])
                for index, specifier in enumerate(entries):
                    self._requirement(
                        specifier, f"optional:{group}", path, content, places, index, declared
                    )

    def _poetry(
        self,
        poetry: dict[str, object],
        path: Path,
        content: str,
        declared: list[DeclaredDependency],
        provides: list[ProvidedPackage],
    ) -> None:
        name = poetry.get("name")
        if isinstance(name, str) and not any(p.name == name for p in provides):
            version = poetry.get("version")
            provides.append(
                ProvidedPackage(
                    ecosystem=PYPI,
                    name=name,
                    normalized_name=normalize(PYPI, name),
                    version=version if isinstance(version, str) else None,
                    manifest_path=path.as_posix(),
                    line=_find_line(content, f'name = "{name}"'),
                )
            )

        for group, table in (
            ("main", poetry.get("dependencies")),
            ("dev", poetry.get("dev-dependencies")),
        ):
            if not isinstance(table, dict):
                continue
            for package, constraint in table.items():
                if package == "python":
                    continue
                text = (
                    constraint
                    if isinstance(constraint, str)
                    else constraint.get("version")
                    if isinstance(constraint, dict)
                    else None
                )
                declared.append(
                    _declared(
                        package,
                        text if isinstance(text, str) else None,
                        group,
                        path,
                        _find_line(content, package),
                    )
                )

    def _requirement(
        self,
        specifier: object,
        group: str,
        path: Path,
        content: str,
        places: list[tuple[int, str | None]],
        index: int,
        declared: list[DeclaredDependency],
    ) -> None:
        if not isinstance(specifier, str):
            return
        parsed = python_requirement(specifier)
        if parsed is None:
            return
        name, constraint = parsed
        line = _item_line(content, places, index, specifier)
        declared.append(_declared(name, constraint, group, path, line))


class RequirementsParser:
    """``requirements.txt`` and its variants."""

    ecosystem = PYPI
    kind: DependencyKind = "manifest"
    name = "requirements-txt"
    version = "2"  # 2: line numbers follow the shared source-line model

    def supports(self, path: Path) -> bool:
        return bool(_REQUIREMENTS.match(path.name))

    def parse(self, path: Path, content: str) -> DependencyReport:
        group = "main"
        stem = path.stem
        if stem.startswith("requirements-"):
            group = stem.removeprefix("requirements-")

        declared: list[DeclaredDependency] = []
        for number, line in enumerate(split_lines(content), start=1):
            text = line.strip()
            if not text or text.startswith("#") or text.split(" ", 1)[0] in _OPTIONS:
                continue
            if text.startswith("-"):
                continue
            parsed = python_requirement(text)
            if parsed is None:
                continue
            name, constraint = parsed
            declared.append(_declared(name, constraint, group, path, number))

        return DependencyReport(ecosystem=self.ecosystem, kind="manifest", declared=tuple(declared))


class SetupPyParser:
    """``setup.py`` — read with ``ast``, never imported (V1_SPEC §13, §17)."""

    ecosystem = PYPI
    kind: DependencyKind = "manifest"
    name = "setup-py"
    # 2: line numbers follow the shared source-line model. +pyX.Y: read with the
    # running interpreter's standard library (repoatlas.parsers.interpreter).
    version = f"2+{interpreter_tag()}"

    def supports(self, path: Path) -> bool:
        return path.name == "setup.py"

    def parse(self, path: Path, content: str) -> DependencyReport:
        try:
            tree = ast.parse(content, filename=path.name)
        except (SyntaxError, ValueError):
            return DependencyReport(
                ecosystem=self.ecosystem,
                kind="manifest",
                diagnostics=(ParseDiagnostic(severity=Severity.WARNING, message=PARSE_FAILED),),
            )

        declared: list[DeclaredDependency] = []
        provides: list[ProvidedPackage] = []
        diagnostics: list[ParseDiagnostic] = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            callee_name = (
                callee.id
                if isinstance(callee, ast.Name)
                else callee.attr
                if isinstance(callee, ast.Attribute)
                else None
            )
            if callee_name != "setup":
                continue
            for keyword in node.keywords:
                if keyword.arg == "name":
                    self._name(keyword, path, provides)
                elif keyword.arg in {"install_requires", "requires"}:
                    self._requirements(keyword, path, declared, diagnostics)
                elif keyword.arg == "extras_require":
                    self._extras(keyword, path, declared, diagnostics)

        return DependencyReport(
            ecosystem=self.ecosystem,
            kind="manifest",
            declared=tuple(declared),
            provides=tuple(provides),
            diagnostics=tuple(diagnostics),
        )

    def _name(self, keyword: ast.keyword, path: Path, provides: list[ProvidedPackage]) -> None:
        value = keyword.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            provides.append(
                ProvidedPackage(
                    ecosystem=PYPI,
                    name=value.value,
                    normalized_name=normalize(PYPI, value.value),
                    manifest_path=path.as_posix(),
                    line=value.lineno,
                )
            )

    def _requirements(
        self,
        keyword: ast.keyword,
        path: Path,
        declared: list[DeclaredDependency],
        diagnostics: list[ParseDiagnostic],
        group: str = "main",
    ) -> None:
        value = keyword.value
        if not isinstance(value, ast.List | ast.Tuple):
            diagnostics.append(
                ParseDiagnostic(
                    severity=Severity.WARNING,
                    message=(
                        f"{keyword.arg} is computed, not a literal list; "
                        "its value is not knowable without executing setup.py"
                    ),
                    line=getattr(value, "lineno", None),
                )
            )
            return
        for element in value.elts:
            if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
                continue
            parsed = python_requirement(element.value)
            if parsed is None:
                continue
            name, constraint = parsed
            declared.append(_declared(name, constraint, group, path, element.lineno))

    def _extras(
        self,
        keyword: ast.keyword,
        path: Path,
        declared: list[DeclaredDependency],
        diagnostics: list[ParseDiagnostic],
    ) -> None:
        value = keyword.value
        if not isinstance(value, ast.Dict):
            return
        for key, entries in zip(value.keys, value.values, strict=True):
            group = (
                f"optional:{key.value}"
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
                else "optional"
            )
            self._requirements(
                ast.keyword(arg="extras_require", value=entries), path, declared, diagnostics, group
            )


def _cfg_option(
    content: str, section: str, option: str, default_section: str
) -> tuple[int, list[tuple[int, str]]] | None:
    """Where an option is written and each line of its value, as ``configparser`` read it:
    its option names are lowercased, and a ``[DEFAULT]`` value counts for every section."""
    values = cfg_values(content)
    for where in (section, default_section):
        found = [v for (at, key), v in values.items() if at == where and key.lower() == option]
        if found:
            return min(found)
    return None


def _cfg_option_line(content: str, section: str, option: str, default_section: str) -> int | None:
    """The line an option is written on (:func:`_cfg_option`)."""
    found = _cfg_option(content, section, option, default_section)
    return found[0] if found is not None else None


class SetupCfgParser:
    """``setup.cfg`` — ``[metadata] name`` and ``[options] install_requires``."""

    ecosystem = PYPI
    kind: DependencyKind = "manifest"
    name = "setup-cfg"
    # 2: line numbers follow the shared source-line model. 3: the provided name's line is
    # its own option's, read from the file's syntax (repoatlas.parsers.setuptools_project).
    # 4: so is each requirement's: the value line it is written on.
    version = "4"

    def supports(self, path: Path) -> bool:
        return path.name == "setup.cfg"

    def parse(self, path: Path, content: str) -> DependencyReport:
        config = configparser.ConfigParser()
        try:
            config.read_string(content)
        except configparser.Error:
            return DependencyReport(
                ecosystem=self.ecosystem,
                kind="manifest",
                diagnostics=(ParseDiagnostic(severity=Severity.WARNING, message=INVALID_CFG),),
            )

        declared: list[DeclaredDependency] = []
        provides: list[ProvidedPackage] = []

        name = config.get("metadata", "name", fallback=None)
        line = _cfg_option_line(content, "metadata", "name", config.default_section)
        if name and line is not None:
            provides.append(
                ProvidedPackage(
                    ecosystem=PYPI,
                    name=name,
                    normalized_name=normalize(PYPI, name),
                    version=config.get("metadata", "version", fallback=None),
                    manifest_path=path.as_posix(),
                    line=line,
                )
            )

        for section, option, group in (
            ("options", "install_requires", "main"),
            ("options", "setup_requires", "build"),
            ("options", "tests_require", "test"),
        ):
            raw = config.get(section, option, fallback="")
            entries = [entry.strip() for entry in raw.splitlines() if entry.strip()]
            written = _cfg_option(content, section, option, config.default_section)
            # Each entry's own value line, when the file's syntax gives exactly the entries
            # configparser read; otherwise the option's line -- never a search for the text,
            # which finds `Flask` in `name = Flask-Moment`.
            lines = (
                [number for number, _ in written[1]]
                if written is not None and [text for _, text in written[1]] == entries
                else None
            )
            for index, specifier in enumerate(entries):
                parsed = python_requirement(specifier)
                if parsed is None:
                    continue
                dependency, constraint = parsed
                line = lines[index] if lines is not None else written[0] if written else 1
                declared.append(_declared(dependency, constraint, group, path, line))

        return DependencyReport(
            ecosystem=self.ecosystem,
            kind="manifest",
            declared=tuple(declared),
            provides=tuple(provides),
        )


class UvLockParser:
    """``uv.lock`` / ``poetry.lock`` — resolved versions, TOML in both cases."""

    ecosystem = PYPI
    kind: DependencyKind = "lockfile"
    name = "python-lock"
    # 2: line numbers follow the shared source-line model. +pyX.Y: read with the
    # running interpreter's standard library (repoatlas.parsers.interpreter).
    version = f"2+{interpreter_tag()}"

    def supports(self, path: Path) -> bool:
        return path.name in {"uv.lock", "poetry.lock"}

    def parse(self, path: Path, content: str) -> DependencyReport:
        try:
            document = tomllib.loads(content)
        except tomllib.TOMLDecodeError:
            return DependencyReport(
                ecosystem=self.ecosystem,
                kind="lockfile",
                diagnostics=(ParseDiagnostic(severity=Severity.WARNING, message=INVALID_TOML),),
            )

        resolved: list[ResolvedDependency] = []
        cursor = 0
        packages = document.get("package")
        for entry in packages if isinstance(packages, list) else []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            version = entry.get("version")
            if not isinstance(name, str) or not isinstance(version, str):
                continue
            line = _find_line(content, f'name = "{name}"', cursor)
            cursor = content.find(f'name = "{name}"', cursor) + 1 or cursor
            resolved.append(
                ResolvedDependency(
                    ecosystem=PYPI,
                    name=name,
                    normalized_name=normalize(PYPI, name),
                    version=version,
                    lockfile_path=path.as_posix(),
                    line=line,
                )
            )

        return DependencyReport(ecosystem=self.ecosystem, kind="lockfile", resolved=tuple(resolved))
