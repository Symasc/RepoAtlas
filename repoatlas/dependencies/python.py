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
from repoatlas.models.parsing import ParseDiagnostic, Severity
from repoatlas.parsers.marks import line_at, split_lines

_REQUIREMENTS = re.compile(r"^requirements.*\.txt$")
_OPTIONS = frozenset({"-r", "--requirement", "-c", "--constraint", "-e", "--editable"})


def _find_line(content: str, needle: str, start: int = 0) -> int:
    index = content.find(needle, start)
    return line_at(content, index) if index != -1 else 1


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
    version = "2"  # 2: line numbers follow the shared source-line model

    def supports(self, path: Path) -> bool:
        return path.name == "pyproject.toml"

    def parse(self, path: Path, content: str) -> DependencyReport:
        try:
            document = tomllib.loads(content)
        except tomllib.TOMLDecodeError as exc:
            return DependencyReport(
                ecosystem=self.ecosystem,
                kind="manifest",
                diagnostics=(
                    ParseDiagnostic(severity=Severity.WARNING, message=f"invalid TOML: {exc}"),
                ),
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
                    line=_find_line(content, f'name = "{name}"'),
                )
            )

        dependencies = project.get("dependencies")
        if isinstance(dependencies, list):
            for specifier in dependencies:
                self._requirement(specifier, "main", path, content, declared)

        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group, entries in optional.items():
                if not isinstance(entries, list):
                    continue
                for specifier in entries:
                    self._requirement(specifier, f"optional:{group}", path, content, declared)

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
        declared: list[DeclaredDependency],
    ) -> None:
        if not isinstance(specifier, str):
            return
        parsed = python_requirement(specifier)
        if parsed is None:
            return
        name, constraint = parsed
        declared.append(_declared(name, constraint, group, path, _find_line(content, specifier)))


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
    version = "2"  # 2: line numbers follow the shared source-line model

    def supports(self, path: Path) -> bool:
        return path.name == "setup.py"

    def parse(self, path: Path, content: str) -> DependencyReport:
        try:
            tree = ast.parse(content, filename=path.name)
        except (SyntaxError, ValueError) as exc:
            return DependencyReport(
                ecosystem=self.ecosystem,
                kind="manifest",
                diagnostics=(
                    ParseDiagnostic(severity=Severity.WARNING, message=f"parse failed: {exc}"),
                ),
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


class SetupCfgParser:
    """``setup.cfg`` — ``[options] install_requires``."""

    ecosystem = PYPI
    kind: DependencyKind = "manifest"
    name = "setup-cfg"
    version = "2"  # 2: line numbers follow the shared source-line model

    def supports(self, path: Path) -> bool:
        return path.name == "setup.cfg"

    def parse(self, path: Path, content: str) -> DependencyReport:
        config = configparser.ConfigParser()
        try:
            config.read_string(content)
        except configparser.Error as exc:
            return DependencyReport(
                ecosystem=self.ecosystem,
                kind="manifest",
                diagnostics=(
                    ParseDiagnostic(severity=Severity.WARNING, message=f"invalid cfg: {exc}"),
                ),
            )

        declared: list[DeclaredDependency] = []
        provides: list[ProvidedPackage] = []

        name = config.get("metadata", "name", fallback=None)
        if name:
            provides.append(
                ProvidedPackage(
                    ecosystem=PYPI,
                    name=name,
                    normalized_name=normalize(PYPI, name),
                    version=config.get("metadata", "version", fallback=None),
                    manifest_path=path.as_posix(),
                    line=_find_line(content, f"name = {name}"),
                )
            )

        for section, option, group in (
            ("options", "install_requires", "main"),
            ("options", "setup_requires", "build"),
            ("options", "tests_require", "test"),
        ):
            raw = config.get(section, option, fallback="")
            for specifier in raw.splitlines():
                parsed = python_requirement(specifier)
                if parsed is None:
                    continue
                dependency, constraint = parsed
                declared.append(
                    _declared(
                        dependency, constraint, group, path, _find_line(content, specifier.strip())
                    )
                )

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
    version = "2"  # 2: line numbers follow the shared source-line model

    def supports(self, path: Path) -> bool:
        return path.name in {"uv.lock", "poetry.lock"}

    def parse(self, path: Path, content: str) -> DependencyReport:
        try:
            document = tomllib.loads(content)
        except tomllib.TOMLDecodeError as exc:
            return DependencyReport(
                ecosystem=self.ecosystem,
                kind="lockfile",
                diagnostics=(
                    ParseDiagnostic(severity=Severity.WARNING, message=f"invalid TOML: {exc}"),
                ),
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
