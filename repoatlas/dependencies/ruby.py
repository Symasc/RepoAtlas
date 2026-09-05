"""Ruby dependency parsers (V1_SPEC §13).

A ``Gemfile`` and a ``.gemspec`` are Ruby programs, so a complete reading would
mean evaluating them — which V1_SPEC §17 forbids. They are instead scanned for
the literal ``gem`` / ``add_dependency`` calls that make up the overwhelming
majority of real files. A dependency added inside a conditional or built from a
variable is *not* recorded: missing a fact is honest, inventing one is not.
"""

from __future__ import annotations

import re
from pathlib import Path

from repoatlas.dependencies.normalize import GEM, normalize
from repoatlas.models.dependencies import (
    DeclaredDependency,
    DependencyKind,
    DependencyReport,
    ProvidedPackage,
    ResolvedDependency,
)
from repoatlas.parsers.marks import split_lines

#: ``gem "rails", "~> 7.0"`` — name, then any number of literal constraints.
_GEM_CALL = re.compile(
    r"^\s*gem\s+(?P<quote>['\"])(?P<name>[^'\"]+)(?P=quote)"
    r"(?P<rest>(?:\s*,\s*['\"][^'\"]+['\"])*)"
)
_CONSTRAINT = re.compile(r"['\"]([^'\"]+)['\"]")
_GROUP_BLOCK = re.compile(r"^\s*group\s+(?P<groups>.+?)\s+do\s*$")
_GROUP_NAME = re.compile(r"[:'\"]([A-Za-z_][\w]*)")
_END_BLOCK = re.compile(r"^\s*end\s*$")

#: ``spec.add_dependency "x", "~> 1"`` / ``add_development_dependency``.
_SPEC_DEPENDENCY = re.compile(
    r"\.add(?P<development>_development|_runtime)?_dependency\s+"
    r"(?P<quote>['\"])(?P<name>[^'\"]+)(?P=quote)"
    r"(?P<rest>(?:\s*,\s*['\"][^'\"]+['\"])*)"
)
_SPEC_NAME = re.compile(r"\.name\s*=\s*['\"](?P<name>[^'\"]+)['\"]")
_SPEC_VERSION = re.compile(r"\.version\s*=\s*['\"](?P<version>[^'\"]+)['\"]")

#: ``    rails (7.0.4)`` — a top-level spec in the GEM section of a lockfile.
_LOCK_SPEC = re.compile(r"^ {4}(?P<name>[A-Za-z0-9._-]+) \((?P<version>[^)]+)\)\s*$")


def _constraints(rest: str) -> str | None:
    found = _CONSTRAINT.findall(rest or "")
    return ", ".join(found) if found else None


class GemfileParser:
    """``Gemfile`` — declared gems, with their ``group`` when one is open."""

    ecosystem = GEM
    kind: DependencyKind = "manifest"
    name = "gemfile"
    version = "2"  # 2: line numbers follow the shared source-line model

    def supports(self, path: Path) -> bool:
        return path.name == "Gemfile"

    def parse(self, path: Path, content: str) -> DependencyReport:
        declared: list[DeclaredDependency] = []
        groups: list[str] = []

        for number, line in enumerate(split_lines(content), start=1):
            if line.lstrip().startswith("#"):
                continue
            block = _GROUP_BLOCK.match(line)
            if block is not None:
                groups.append(",".join(_GROUP_NAME.findall(block.group("groups"))) or "group")
                continue
            if _END_BLOCK.match(line) and groups:
                groups.pop()
                continue
            match = _GEM_CALL.match(line)
            if match is None:
                continue
            gem = match.group("name")
            declared.append(
                DeclaredDependency(
                    ecosystem=GEM,
                    name=gem,
                    normalized_name=normalize(GEM, gem),
                    constraint=_constraints(match.group("rest")),
                    group=groups[-1] if groups else "main",
                    manifest_path=path.as_posix(),
                    line=number,
                )
            )

        return DependencyReport(ecosystem=self.ecosystem, kind="manifest", declared=tuple(declared))


class GemspecParser:
    """``*.gemspec`` — the gem this repository provides, plus its dependencies."""

    ecosystem = GEM
    kind: DependencyKind = "manifest"
    name = "gemspec"
    version = "2"  # 2: line numbers follow the shared source-line model

    def supports(self, path: Path) -> bool:
        return path.suffix == ".gemspec"

    def parse(self, path: Path, content: str) -> DependencyReport:
        declared: list[DeclaredDependency] = []
        provides: list[ProvidedPackage] = []

        version_match = _SPEC_VERSION.search(content)
        for number, line in enumerate(split_lines(content), start=1):
            if line.lstrip().startswith("#"):
                continue
            name_match = _SPEC_NAME.search(line)
            if name_match is not None and not provides:
                gem = name_match.group("name")
                provides.append(
                    ProvidedPackage(
                        ecosystem=GEM,
                        name=gem,
                        normalized_name=normalize(GEM, gem),
                        version=version_match.group("version") if version_match else None,
                        manifest_path=path.as_posix(),
                        line=number,
                    )
                )
            dependency = _SPEC_DEPENDENCY.search(line)
            if dependency is None:
                continue
            gem = dependency.group("name")
            declared.append(
                DeclaredDependency(
                    ecosystem=GEM,
                    name=gem,
                    normalized_name=normalize(GEM, gem),
                    constraint=_constraints(dependency.group("rest")),
                    group="dev" if dependency.group("development") == "_development" else "main",
                    manifest_path=path.as_posix(),
                    line=number,
                )
            )

        return DependencyReport(
            ecosystem=self.ecosystem,
            kind="manifest",
            declared=tuple(declared),
            provides=tuple(provides),
        )


class GemfileLockParser:
    """``Gemfile.lock`` — the resolved versions in the ``specs:`` section."""

    ecosystem = GEM
    kind: DependencyKind = "lockfile"
    name = "gemfile-lock"
    version = "2"  # 2: line numbers follow the shared source-line model

    def supports(self, path: Path) -> bool:
        return path.name == "Gemfile.lock"

    def parse(self, path: Path, content: str) -> DependencyReport:
        resolved: dict[str, ResolvedDependency] = {}
        in_specs = False

        for number, line in enumerate(split_lines(content), start=1):
            stripped = line.strip()
            if stripped == "specs:":
                in_specs = True
                continue
            if line and not line[0].isspace():
                in_specs = False
                continue
            if not in_specs:
                continue
            match = _LOCK_SPEC.match(line)
            if match is None:
                continue
            gem = match.group("name")
            resolved.setdefault(
                gem,
                ResolvedDependency(
                    ecosystem=GEM,
                    name=gem,
                    normalized_name=normalize(GEM, gem),
                    version=match.group("version"),
                    lockfile_path=path.as_posix(),
                    line=number,
                ),
            )

        return DependencyReport(
            ecosystem=self.ecosystem, kind="lockfile", resolved=tuple(resolved.values())
        )
