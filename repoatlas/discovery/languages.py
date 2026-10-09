"""Language detection by filename and extension (V1_SPEC §7).

Detection is independent of whether a parser exists for the language: ``repos``
reports what a repository *contains*, and Slice A parses only a subset of it.
"""

from __future__ import annotations

from pathlib import PurePosixPath

_EXTENSIONS: dict[str, str] = {
    ".py": "Python",
    ".pyi": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".mts": "TypeScript",
    ".cts": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".sh": "Bash",
    ".bash": "Bash",
    ".zsh": "Bash",
    ".rb": "Ruby",
    ".rake": "Ruby",
    ".gemspec": "Ruby",
    ".sls": "Salt",
    ".jinja": "Jinja",
    ".j2": "Jinja",
    ".jinja2": "Jinja",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".json": "JSON",
    ".graphql": "GraphQL",
    ".gql": "GraphQL",
    ".graphqls": "GraphQL",
    ".prisma": "Prisma",
    ".sql": "SQL",
}

_FILENAMES: dict[str, str] = {
    "Dockerfile": "Dockerfile",
    "Containerfile": "Dockerfile",
    "Gemfile": "Ruby",
    "Rakefile": "Ruby",
    "Makefile": "Make",
}

#: Dependency manifests (declared versions) — V1_SPEC §13.
MANIFEST_FILENAMES: frozenset[str] = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "package.json",
        "Gemfile",
    }
)

#: Lockfiles (resolved versions) — V1_SPEC §13.
LOCKFILE_FILENAMES: frozenset[str] = frozenset(
    {
        "uv.lock",
        "poetry.lock",
        "pnpm-lock.yaml",
        "package-lock.json",
        "yarn.lock",
        "Gemfile.lock",
    }
)


def detect_language(rel_path: str) -> str | None:
    """Language of ``rel_path``, or ``None`` when unrecognized."""
    pure = PurePosixPath(rel_path)
    name = pure.name

    if name in _FILENAMES:
        return _FILENAMES[name]
    if name.startswith("Dockerfile") or pure.suffix.lower() == ".dockerfile":
        return "Dockerfile"
    if name.startswith("requirements") and name.endswith(".txt"):
        return "Text"
    return _EXTENSIONS.get(pure.suffix)


def is_manifest(rel_path: str) -> bool:
    name = PurePosixPath(rel_path).name
    return name in MANIFEST_FILENAMES or (name.startswith("requirements") and name.endswith(".txt"))


def is_lockfile(rel_path: str) -> bool:
    return PurePosixPath(rel_path).name in LOCKFILE_FILENAMES
