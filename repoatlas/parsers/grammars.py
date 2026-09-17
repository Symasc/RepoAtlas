"""The tree-sitter grammars RepoAtlas ships, and exactly where each comes from (OFF-01).

**A grammar is an installed artifact, never a download.** Indexing a repository
must not fetch, build or discover parser code at run time, so every grammar a
parser uses is a pinned package dependency that the installer places in the
environment, and :func:`repoatlas.parsers.treesitter.load_parser` loads it from
there or not at all. The network boundary is package installation; nothing after
it reaches for one.

This table is the single place that says which grammars exist. Each entry names:

* the distribution and the **exact** version ``pyproject.toml`` pins --- the loader
  refuses any other installed version, because a different grammar yields
  different facts from the same file without a parser version bump to invalidate
  the stored ones;
* the module and the function that returns the language, which is how the
  upstream Python binding exposes it;
* the upstream repository, release tag and the commit that tag names, and the
  license, so the provenance is auditable from the source tree alone.

The wheels are built and published by the tree-sitter organisation itself, and
``uv.lock`` records the SHA-256 of every sdist and wheel, so an installation from
the lockfile verifies the bytes. ``THIRD_PARTY_NOTICES.md`` carries the same facts
for a human reader.

**Ruby is absent on purpose.** The only offline-installable Ruby grammar,
``tree-sitter-ruby`` 0.23.1, predates upstream commit ``ad907a69`` ("scanner: fix
heredoc serialization buffer overflows"), whose external scanner can write one
attacker-chosen byte past tree-sitter's serialization buffer. RepoAtlas reads
untrusted repositories, so no Ruby grammar is shipped and Ruby has no structural
parser at all: those files are discovered, chunked and searchable as text, and
``Gemfile`` / ``*.gemspec`` dependencies are read by the text-level dependency
parsers. Adding one means pinning a release that contains the fix, beside these.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class Grammar:
    """One shipped grammar and its provenance."""

    language: str
    distribution: str
    version: str
    module: str
    function: str
    upstream: str
    tag: str
    commit: str
    license: str


_TYPESCRIPT = dict(
    distribution="tree-sitter-typescript",
    version="0.23.2",
    module="tree_sitter_typescript",
    upstream="https://github.com/tree-sitter/tree-sitter-typescript",
    tag="v0.23.2",
    commit="f975a621f4e7f532fe322e13c4f79495e0a7b2e7",
    license="MIT",
)

#: Grammar name, as the parsers ask for it, to the artifact that provides it.
GRAMMARS: Mapping[str, Grammar] = MappingProxyType(
    {
        grammar.language: grammar
        for grammar in (
            Grammar(language="typescript", function="language_typescript", **_TYPESCRIPT),
            Grammar(language="tsx", function="language_tsx", **_TYPESCRIPT),
            Grammar(
                language="javascript",
                distribution="tree-sitter-javascript",
                version="0.25.0",
                module="tree_sitter_javascript",
                function="language",
                upstream="https://github.com/tree-sitter/tree-sitter-javascript",
                tag="v0.25.0",
                commit="44c892e0be055ac465d5eeddae6d3e194424e7de",
                license="MIT",
            ),
            Grammar(
                language="bash",
                distribution="tree-sitter-bash",
                version="0.25.1",
                module="tree_sitter_bash",
                function="language",
                upstream="https://github.com/tree-sitter/tree-sitter-bash",
                tag="v0.25.1",
                commit="a06c2e4415e9bc0346c6b86d401879ffb44058f7",
                license="MIT",
            ),
        )
    }
)
