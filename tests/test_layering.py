"""Architectural invariants enforced as tests (ARCHITECTURE.md §1).

An AST walk over every module's imports. Violations fail CI, not review.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parent.parent / "repoatlas"

#: Packages a given package may import from. Absent = unconstrained.
_FORBIDDEN: dict[str, set[str]] = {
    # A parser is a pure function of (path, content).
    "parsers": {"storage", "llm", "sync", "retrieval", "services", "cli", "indexing", "graph"},
    # So is a dependency parser, for the same reason (ARCHITECTURE.md §4).
    "dependencies": {
        "storage",
        "llm",
        "sync",
        "retrieval",
        "services",
        "cli",
        "indexing",
        "graph",
        "gitstate",
        "discovery",
        "context",
    },
    # The linker sits above storage and beside the parsers; it is below the spine.
    "graph": {"cli", "services", "sync", "retrieval", "llm", "indexing", "rendering", "context"},
    # models/ underpins everything, so it may import nothing internal.
    "models": {
        "cli",
        "services",
        "config",
        "discovery",
        "context",
        "gitstate",
        "sync",
        "indexing",
        "parsers",
        "dependencies",
        "graph",
        "retrieval",
        "storage",
        "llm",
        "rendering",
    },
    # The LLM layer receives a built EvidenceSet; it never reaches into storage.
    "llm": {"storage", "sync", "indexing", "discovery", "cli", "services"},
    # Rendering formats models; it does not query.
    "rendering": {"storage", "sync", "indexing", "retrieval", "cli", "services", "llm"},
    # Storage sits below everything except models.
    "storage": {
        "cli",
        "services",
        "sync",
        "retrieval",
        "llm",
        "discovery",
        "context",
        "indexing",
        "rendering",
    },
    # The CLI resolves context, calls a service and renders.
    "cli": {"storage", "sync", "indexing", "parsers", "graph", "discovery"},
}


def _modules() -> list[tuple[str, Path]]:
    found = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        package = path.relative_to(PACKAGE_ROOT).parts[0]
        if package.endswith(".py"):
            package = "<root>"
        found.append((package, path))
    return found


def _imported_packages(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    packages: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            if parts[0] == "repoatlas" and len(parts) > 1:
                packages.add(parts[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "repoatlas" and len(parts) > 1:
                    packages.add(parts[1])
    return packages


def test_layering_rules_hold() -> None:
    violations: list[str] = []
    for package, path in _modules():
        forbidden = _FORBIDDEN.get(package)
        if forbidden is None:
            continue
        for imported in _imported_packages(path):
            if imported == package:
                continue
            if imported in forbidden:
                violations.append(
                    f"{path.relative_to(PACKAGE_ROOT.parent)} imports repoatlas.{imported}"
                )
    assert not violations, "layering violations:\n" + "\n".join(violations)


def test_no_god_object() -> None:
    """There is no RepoAtlasAgent (ARCHITECTURE.md §1 rule 4)."""
    for _, path in _modules():
        source = path.read_text(encoding="utf-8")
        assert "class RepoAtlasAgent" not in source, path


def test_sql_appears_only_in_the_sqlite_package() -> None:
    """Only storage/sqlite/ knows SQL (ARCHITECTURE.md §1 rule 6)."""
    offenders: list[str] = []
    markers = ("SELECT ", "INSERT INTO", "DELETE FROM", "UPDATE ", "CREATE TABLE")
    for _, path in _modules():
        relative = path.relative_to(PACKAGE_ROOT)
        if relative.parts[:2] == ("storage", "sqlite"):
            continue
        source = path.read_text(encoding="utf-8")
        if any(marker in source for marker in markers):
            offenders.append(str(relative))
    assert not offenders, f"SQL outside storage/sqlite: {offenders}"


def test_parsers_do_not_perform_io() -> None:
    """Parsers never read the filesystem or execute anything (V1_SPEC §7, §17).

    ``dependencies/`` is held to the same rule: ``setup.py`` is the case that makes
    it load-bearing, since parsing it is safe and importing it is not.
    """
    banned = ("subprocess", "open(", "read_text", "read_bytes", "os.system", "eval(", "exec(")
    for package in ("parsers", "dependencies"):
        for path in sorted((PACKAGE_ROOT / package).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for marker in banned:
                assert marker not in source, f"{package}/{path.name} contains {marker!r}"


def test_language_parsers_never_import_the_dependency_layer() -> None:
    """The two answer different questions about the same file and stay separate."""
    for path in sorted((PACKAGE_ROOT / "parsers").rglob("*.py")):
        assert "repoatlas.dependencies" not in path.read_text(encoding="utf-8"), path.name


def test_only_gitstate_invokes_git() -> None:
    """Every git call goes through the hardened wrapper (ARCHITECTURE.md §8)."""
    offenders: list[str] = []
    for _, path in _modules():
        relative = path.relative_to(PACKAGE_ROOT)
        if relative.parts[0] == "gitstate":
            continue
        if "subprocess" in path.read_text(encoding="utf-8"):
            offenders.append(str(relative))
    assert not offenders, f"subprocess used outside gitstate: {offenders}"
