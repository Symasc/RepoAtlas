"""Node dependency parsers (V1_SPEC §13).

Covers ``package.json`` plus the three lockfile formats in common use. npm names
are scope-aware: ``@company/common`` and ``common`` are different packages, and
the scope is preserved through normalization (``normalize.py``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from repoatlas.dependencies.normalize import NPM, normalize
from repoatlas.models.dependencies import (
    DeclaredDependency,
    DependencyKind,
    DependencyReport,
    ProvidedPackage,
    ResolvedDependency,
)
from repoatlas.models.parsing import ParseDiagnostic, Severity
from repoatlas.parsers.marks import line_at, split_lines

_DEPENDENCY_GROUPS = {
    "dependencies": "main",
    "devDependencies": "dev",
    "peerDependencies": "peer",
    "optionalDependencies": "optional",
}

#: ``node_modules/@scope/name`` → ``@scope/name`` (npm lockfile v2/v3 keys).
_NODE_MODULES = re.compile(r"(?:^|/)node_modules/(?P<name>(?:@[^/]+/)?[^/]+)$")
#: pnpm keys: ``/foo@1.2.3``, ``/@scope/foo@1.2.3``, ``/@scope/foo/1.2.3``.
_PNPM_KEY = re.compile(
    r"^/?(?P<name>(?:@[^/]+/)?[^/@][^/]*?)[@/](?P<version>\d[^()]*?)(?:\(.*\))?$"
)
#: yarn v1 blocks: ``"@company/common@^2.1.0":`` then ``  version "2.1.0"``.
_YARN_HEADER = re.compile(r'^"?(?P<name>(?:@[^/]+/)?[^@\s"]+)@')
_YARN_VERSION = re.compile(r'^\s+version\s+"?(?P<version>[^"\s]+)"?')


def _find_line(content: str, needle: str, start: int = 0) -> int:
    index = content.find(needle, start)
    return line_at(content, index) if index != -1 else 1


def _resolved(name: str, version: str, path: Path, line: int) -> ResolvedDependency:
    return ResolvedDependency(
        ecosystem=NPM,
        name=name,
        normalized_name=normalize(NPM, name),
        version=version,
        lockfile_path=path.as_posix(),
        line=line,
    )


class PackageJsonParser:
    """``package.json`` — the declared side, and the package this repo provides."""

    ecosystem = NPM
    kind: DependencyKind = "manifest"
    name = "package-json"
    version = "2"  # 2: line numbers follow the shared source-line model

    def supports(self, path: Path) -> bool:
        return path.name == "package.json"

    def parse(self, path: Path, content: str) -> DependencyReport:
        try:
            document = json.loads(content)
        except json.JSONDecodeError as exc:
            return DependencyReport(
                ecosystem=self.ecosystem,
                kind="manifest",
                diagnostics=(
                    ParseDiagnostic(
                        severity=Severity.WARNING,
                        message=f"invalid JSON: {exc.msg}",
                        line=exc.lineno,
                    ),
                ),
            )
        if not isinstance(document, dict):
            return DependencyReport(ecosystem=self.ecosystem, kind="manifest")

        declared: list[DeclaredDependency] = []
        provides: list[ProvidedPackage] = []

        name = document.get("name")
        if isinstance(name, str):
            version = document.get("version")
            provides.append(
                ProvidedPackage(
                    ecosystem=NPM,
                    name=name,
                    normalized_name=normalize(NPM, name),
                    version=version if isinstance(version, str) else None,
                    manifest_path=path.as_posix(),
                    line=_find_line(content, f'"{name}"'),
                )
            )

        for key, group in _DEPENDENCY_GROUPS.items():
            table = document.get(key)
            if not isinstance(table, dict):
                continue
            section = content.find(f'"{key}"')
            for package, constraint in table.items():
                declared.append(
                    DeclaredDependency(
                        ecosystem=NPM,
                        name=package,
                        normalized_name=normalize(NPM, package),
                        constraint=constraint if isinstance(constraint, str) else None,
                        group=group,
                        manifest_path=path.as_posix(),
                        line=_find_line(content, f'"{package}"', max(section, 0)),
                    )
                )

        return DependencyReport(
            ecosystem=self.ecosystem,
            kind="manifest",
            declared=tuple(declared),
            provides=tuple(provides),
        )


class PackageLockParser:
    """``package-lock.json`` — v2/v3 ``packages`` and the legacy v1 ``dependencies``."""

    ecosystem = NPM
    kind: DependencyKind = "lockfile"
    name = "package-lock"
    version = "2"  # 2: line numbers follow the shared source-line model

    def supports(self, path: Path) -> bool:
        return path.name == "package-lock.json"

    def parse(self, path: Path, content: str) -> DependencyReport:
        try:
            document = json.loads(content)
        except json.JSONDecodeError as exc:
            return DependencyReport(
                ecosystem=self.ecosystem,
                kind="lockfile",
                diagnostics=(
                    ParseDiagnostic(
                        severity=Severity.WARNING,
                        message=f"invalid JSON: {exc.msg}",
                        line=exc.lineno,
                    ),
                ),
            )
        if not isinstance(document, dict):
            return DependencyReport(ecosystem=self.ecosystem, kind="lockfile")

        found: dict[str, ResolvedDependency] = {}
        packages = document.get("packages")
        if isinstance(packages, dict):
            for key, entry in packages.items():
                match = _NODE_MODULES.search(key)
                if match is None or not isinstance(entry, dict):
                    continue
                version = entry.get("version")
                if not isinstance(version, str):
                    continue
                name = match.group("name")
                found.setdefault(name, _resolved(name, version, path, _find_line(content, key)))

        legacy = document.get("dependencies")
        if isinstance(legacy, dict):
            self._legacy(legacy, path, content, found)

        return DependencyReport(
            ecosystem=self.ecosystem, kind="lockfile", resolved=tuple(found.values())
        )

    def _legacy(
        self,
        table: dict[str, object],
        path: Path,
        content: str,
        found: dict[str, ResolvedDependency],
    ) -> None:
        for name, entry in table.items():
            if not isinstance(entry, dict):
                continue
            version = entry.get("version")
            if isinstance(version, str):
                found.setdefault(
                    name, _resolved(name, version, path, _find_line(content, f'"{name}"'))
                )
            nested = entry.get("dependencies")
            if isinstance(nested, dict):
                self._legacy(nested, path, content, found)


class PnpmLockParser:
    """``pnpm-lock.yaml`` — resolved versions live in the ``packages`` keys."""

    ecosystem = NPM
    kind: DependencyKind = "lockfile"
    name = "pnpm-lock"
    version = "2"  # 2: line numbers follow the shared source-line model

    def supports(self, path: Path) -> bool:
        return path.name == "pnpm-lock.yaml"

    def parse(self, path: Path, content: str) -> DependencyReport:
        try:
            document = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            return DependencyReport(
                ecosystem=self.ecosystem,
                kind="lockfile",
                diagnostics=(
                    ParseDiagnostic(severity=Severity.WARNING, message=f"invalid YAML: {exc}"),
                ),
            )
        if not isinstance(document, dict):
            return DependencyReport(ecosystem=self.ecosystem, kind="lockfile")

        found: dict[str, ResolvedDependency] = {}
        for section in ("packages", "snapshots"):
            table = document.get(section)
            if not isinstance(table, dict):
                continue
            for key in table:
                if not isinstance(key, str):
                    continue
                match = _PNPM_KEY.match(key)
                if match is None:
                    continue
                name = match.group("name")
                found.setdefault(
                    name,
                    _resolved(name, match.group("version"), path, _find_line(content, key)),
                )

        return DependencyReport(
            ecosystem=self.ecosystem, kind="lockfile", resolved=tuple(found.values())
        )


class YarnLockParser:
    """``yarn.lock`` — classic v1 blocks and Berry's YAML, handled uniformly."""

    ecosystem = NPM
    kind: DependencyKind = "lockfile"
    name = "yarn-lock"
    version = "2"  # 2: line numbers follow the shared source-line model

    def supports(self, path: Path) -> bool:
        return path.name == "yarn.lock"

    def parse(self, path: Path, content: str) -> DependencyReport:
        found: dict[str, ResolvedDependency] = {}
        pending: list[tuple[str, int]] = []

        for number, line in enumerate(split_lines(content), start=1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if not line[0].isspace():
                pending = []
                for entry in line.rstrip(":").split(","):
                    match = _YARN_HEADER.match(entry.strip())
                    if match is not None:
                        pending.append((match.group("name"), number))
                continue
            version_match = _YARN_VERSION.match(line)
            if version_match is None or not pending:
                continue
            version = version_match.group("version")
            for name, declared_line in pending:
                found.setdefault(name, _resolved(name, version, path, declared_line))
            pending = []

        return DependencyReport(
            ecosystem=self.ecosystem, kind="lockfile", resolved=tuple(found.values())
        )
