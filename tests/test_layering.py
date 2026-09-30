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
    # The capability layer is what an external agent talks to. It sits above
    # services/ and may use everything below it, with three exceptions: it has no
    # terminal (cli), it returns models rather than formatted text (rendering),
    # and it is deterministic — a capability that could reach a model could
    # manufacture evidence (llm).
    "capabilities": {"cli", "rendering", "llm", "mcp", "api"},
    # ...and the arrow only runs one way. A service reaching up into
    # capabilities/ for one typed value would reverse the layering for a single
    # import, which is exactly how it would happen.
    "services": {"capabilities", "mcp", "api"},
    # The transport adapter is the top of the stack and the narrowest layer in
    # it. It may name the capability service, the V2 models it passes through and
    # the public error envelope; everything else on this list is a piece of
    # RepoAtlas it would have to *reimplement* to use. A transport that reached a
    # store, a retriever or a lock directly would be a second RepoAtlas with a
    # second set of answers, reachable only over a protocol.
    "mcp": {
        "storage",
        "retrieval",
        "graph",
        "sync",
        "indexing",
        "services",
        "discovery",
        "context",
        "gitstate",
        "parsers",
        "dependencies",
        "embeddings",
        "llm",
        "rendering",
        "cli",
        "api",
    },
}
#: The local HTTP transport is held to the same line, and neither transport
#: reaches the other: each is an adapter over the capability layer, not over one
#: another.
_FORBIDDEN["api"] = (_FORBIDDEN["mcp"] - {"api"}) | {"mcp"}


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


def test_services_never_import_the_capability_layer() -> None:
    """The dependency arrow, asserted in the direction that is tempting to break.

    Shared logic both layers need — freshness above all — belongs *below* both,
    which is why the state machine is a top-level module rather than
    ``capabilities/freshness.py`` (V1_SPEC §12).
    """
    offenders: list[str] = []
    for package, path in _modules():
        if package != "services":
            continue
        if "capabilities" in _imported_packages(path):
            offenders.append(str(path.relative_to(PACKAGE_ROOT.parent)))
    assert not offenders, f"services must not import capabilities: {offenders}"


def test_the_capability_layer_never_infers_a_working_directory() -> None:
    """A capability call's scope comes from the call, never from the process.

    The server was started wherever its launcher happened to be, serves many
    tasks and outlives all of them, so a working directory there is evidence about
    nothing. Asserted as an absence because the failure is silent: inferring a
    scope produces a confident answer about the wrong repository.
    """
    banned = ("Path.cwd(", "os.getcwd(", "os.curdir")
    offenders: list[str] = []
    for path in sorted((PACKAGE_ROOT / "capabilities").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for marker in banned:
            if marker in source:
                offenders.append(f"{path.name} contains {marker!r}")
    assert not offenders, offenders


def test_the_capability_layer_carries_no_transport_vocabulary() -> None:
    """MCP is a transport adapter, not the RepoAtlas domain API.

    A protocol name reaching the domain models is how the next transport becomes
    a rewrite rather than an adapter.
    """
    banned = ("mcp", "MCP", "json_rpc", "JSONRPC", "jsonrpc")
    offenders: list[str] = []
    for directory in ("capabilities", "models"):
        for path in sorted((PACKAGE_ROOT / directory).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for marker in banned:
                if marker in source:
                    offenders.append(f"{directory}/{path.name} contains {marker!r}")
    assert not offenders, offenders


#: Capability modules that hold no handle on anything. They receive values a
#: caller gathered and compare them, which is what makes every rule in them
#: testable without a database and stops "compare these" becoming "go and read
#: the index" (V1_SPEC §12). ``service.py`` is deliberately not here: owning the
#: workspace is its entire job.
_PURE_CAPABILITY_MODULES = ("ledger.py", "session.py")


def test_the_pure_capability_modules_hold_no_database_handle() -> None:
    """Ledger and session state never reach storage, by import or by name.

    This is the narrowed form of a rule Slice B wrote against the whole package.
    Slice B had no module that legitimately owned a workspace, so banning the name
    everywhere cost nothing and documented the intent. Slice C introduces exactly
    such a module, and a ban broad enough to catch it would have been satisfied by
    the wrong fix — re-assembling stores inside the capability layer to avoid
    naming ``Workspace`` — which is the duplication the rule exists to prevent.

    So the guarantee is kept where it means something and dropped where it does
    not. These two modules stay pure; ``service.py`` is allowed to own a workspace
    and is forbidden a concrete backend by the package-wide test below.
    """
    offenders: list[str] = []
    for name in _PURE_CAPABILITY_MODULES:
        path = PACKAGE_ROOT / "capabilities" / name
        source = path.read_text(encoding="utf-8")
        for marker in ("Workspace", "Database", "sqlite3", "Sqlite", "storage.sqlite"):
            if marker in source:
                offenders.append(f"capabilities/{name} contains {marker!r}")
        if "storage" in _imported_packages(path):
            offenders.append(f"capabilities/{name} imports repoatlas.storage")
    assert not offenders, offenders


def test_no_capability_imports_a_concrete_storage_backend() -> None:
    """The whole layer talks to protocols and services, never to a driver.

    An import check rather than a text search, because ``service.py`` reaches
    stores *through* the workspace and mentions neither SQL nor a driver — the
    thing worth forbidding is acquiring one directly.
    """
    offenders: list[str] = []
    for path in sorted((PACKAGE_ROOT / "capabilities").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            for module in modules:
                if module == "sqlite3" or module.startswith("repoatlas.storage.sqlite"):
                    offenders.append(f"capabilities/{path.name} imports {module}")
    assert not offenders, offenders


def test_the_capability_layer_imports_no_transport_or_vendor_package() -> None:
    """No client, protocol or vendor package is reachable from a capability.

    Checked at the import level rather than by substring, so prose may discuss a
    transport without the guard mistaking a sentence for a dependency. The
    substring rule above still covers the domain vocabulary; this one covers the
    thing that would actually couple the layer to a transport, which is importing
    one.
    """
    banned = {
        "mcp",
        "fastmcp",
        "anthropic",
        "openai",
        "claude",
        "codex",
        "copilot",
        "jsonrpc",
        "json_rpc",
        "websockets",
        "starlette",
        "fastapi",
    }
    offenders: list[str] = []
    for directory in ("capabilities", "models"):
        for path in sorted((PACKAGE_ROOT / directory).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                elif isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                for module in modules:
                    if module.split(".")[0] in banned:
                        offenders.append(f"{directory}/{path.name} imports {module}")
    assert not offenders, offenders


def test_the_capability_layer_names_no_protocol_message_type() -> None:
    """Wire-message vocabulary never reaches the domain models.

    These are not ordinary English: each one names a concrete message shape from a
    specific transport, so a match is coupling rather than a coincidence of
    wording.
    """
    banned = ("tool_call", "toolCall", "content_block", "contentBlock", "stdio")
    offenders: list[str] = []
    for directory in ("capabilities", "models"):
        for path in sorted((PACKAGE_ROOT / directory).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for marker in banned:
                if marker in source:
                    offenders.append(f"{directory}/{path.name} contains {marker!r}")
    assert not offenders, offenders


#: The one package above ``capabilities`` and the only package allowed to reach
#: it. Slice J adds a transport, so "nothing imports the capability layer" stops
#: being the right rule: the whole purpose of an adapter is to import it. What
#: survives is the rule that actually mattered — *V1* has acquired no dependency
#: on V2 — and it is asserted by naming the single exception rather than by
#: loosening the walk.
_TRANSPORTS = {"mcp", "api"}


def test_nothing_outside_the_capability_layer_imports_it() -> None:
    """V2 state is additive: no V1 module has acquired a dependency on it.

    An AST walk rather than a text search, so a docstring that *names* a
    capability class to cross-reference it is not mistaken for a dependency on one.
    """
    offenders: list[str] = []
    for package, path in _modules():
        if package == "capabilities" or package in _TRANSPORTS:
            continue
        if "capabilities" in _imported_packages(path):
            offenders.append(str(path.relative_to(PACKAGE_ROOT.parent)))
    assert not offenders, f"capabilities/ is imported only by a transport, not by: {offenders}"


def test_only_the_cli_reaches_the_transport_adapter() -> None:
    """Nothing below the transport imports it, and the CLI only registers it.

    The asymmetric half of the arrow ``mcp -> capabilities``. A capability or a
    service importing the adapter back would make a protocol type reachable from
    the domain, and the next transport would then be a rewrite rather than a
    second adapter. The CLI is the one legitimate importer, because a command has
    to exist for a launcher to start.
    """
    offenders: list[str] = []
    for package, path in _modules():
        if package in _TRANSPORTS or package == "cli":
            continue
        if _imported_packages(path) & _TRANSPORTS:
            offenders.append(str(path.relative_to(PACKAGE_ROOT.parent)))
    assert not offenders, f"a transport is imported by: {offenders}"


def test_the_transport_adapter_holds_no_repository_intelligence() -> None:
    """The adapter names no engine, no store, no lock and no evidence machinery.

    A text guard beside the import guard above, because the failure it catches is
    not a new import but a new *name*: an adapter that grew a ledger lookup, a
    hash comparison or a writer lock would be reproducing a capability contract
    rather than carrying it, and the tell is that the vocabulary appears at all.

    ``EvidenceLedger`` and ``SessionRegistry`` are on the list even though the
    service exposes a registry, because reaching one from a transport is how an
    investigation would come to mean "this connection" (V2 Slice B).
    """
    banned = (
        "Retriever",
        "RepositoryBoundary",
        "GitClient",
        "SyncEngine",
        "ReferenceLinker",
        "SqliteKnowledgeStore",
        "SqliteFileStore",
        "SqliteRepositoryStore",
        "sqlite3",
        "EvidenceLedger",
        "SessionRegistry",
        "writer_lock",
        "ground_answer",
        "expand_neighbors",
        "open_workspace",
        "Workspace",
    )
    assert not _transport_sources_containing(banned)


def _transport_sources_containing(banned: tuple[str, ...]) -> list[str]:
    offenders: list[str] = []
    for transport in sorted(_TRANSPORTS):
        for path in sorted((PACKAGE_ROOT / transport).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for marker in banned:
                if marker in source:
                    offenders.append(f"{transport}/{path.name} contains {marker!r}")
    return offenders


def test_the_transport_adapter_infers_no_working_directory() -> None:
    """§13: scope arrives in the request, even over a protocol that offers roots.

    The MCP protocol lets a client advertise workspace roots, and reading one here
    would be the single most natural way to reintroduce the ambient default the
    capability layer spent a slice removing --- with the added property that the
    scope would then depend on which transport the caller used.
    """
    banned = ("Path.cwd(", "os.getcwd(", "os.curdir", "list_roots", "ListRoots")
    assert not _transport_sources_containing(banned)
