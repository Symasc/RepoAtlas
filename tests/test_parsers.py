"""Parser contract and the Python parser (V1_SPEC §7, §19).

Parser tests are pure: parse text, assert on the result. No database, no sync.
"""

from __future__ import annotations

from pathlib import Path

from repoatlas.models.parsing import EdgeType, EntityType, Severity
from repoatlas.parsers.base import LanguageParser
from repoatlas.parsers.python import PythonParser
from repoatlas.parsers.registry import ParserRegistry


def parse(source: str, name: str = "sample.py"):
    return PythonParser().parse(Path(name), source)


def test_parser_satisfies_the_protocol() -> None:
    assert isinstance(PythonParser(), LanguageParser)


def test_registry_dispatches_by_extension() -> None:
    registry = ParserRegistry()
    assert registry.for_path(Path("a/b/c.py")) is not None
    assert registry.for_path(Path("a/b/c.rb")) is None


def test_registry_version_changes_when_a_parser_is_added() -> None:
    """A version change forces reparse, which is correct for a new parser."""
    base = ParserRegistry()
    before = base.version

    class FakeParser:
        name = "fake"
        version = "1"
        language = "Fake"

        def supports(self, path: Path) -> bool:
            return path.suffix == ".fake"

        def parse(self, path: Path, content: str):  # pragma: no cover - not invoked
            raise NotImplementedError

    base.register(FakeParser())
    assert base.version != before


def test_extracts_functions_and_line_ranges() -> None:
    parsed = parse("def alpha():\n    return 1\n\n\ndef beta(x, y):\n    return x + y\n")
    functions = {s.name: s for s in parsed.symbols if s.kind is EntityType.FUNCTION}
    assert set(functions) == {"alpha", "beta"}
    assert functions["alpha"].line_start == 1
    assert functions["beta"].line_start == 5
    assert functions["beta"].signature == "def beta(x, y)"


def test_extracts_async_functions() -> None:
    parsed = parse("async def fetch(url):\n    return url\n")
    fetch = next(s for s in parsed.symbols if s.name == "fetch")
    assert fetch.meta["async"] == "true"
    assert fetch.signature is not None
    assert fetch.signature.startswith("async def")


def test_extracts_classes_and_methods() -> None:
    parsed = parse(
        "class Service:\n"
        "    def start(self):\n"
        "        return 1\n"
        "\n"
        "    async def stop(self):\n"
        "        return 2\n"
    )
    kinds = {(s.name, s.kind) for s in parsed.symbols}
    assert ("Service", EntityType.CLASS) in kinds
    assert ("start", EntityType.METHOD) in kinds
    assert ("stop", EntityType.METHOD) in kinds

    start = next(s for s in parsed.symbols if s.name == "start")
    assert start.qualified_name == "Service.start"
    assert start.parent == "Service"


def test_extracts_absolute_and_aliased_imports() -> None:
    parsed = parse("import os\nimport numpy as np\n")
    imports = {r.target_key: r for r in parsed.references if r.edge_type is EdgeType.IMPORTS}
    assert "os" in imports
    assert imports["numpy"].meta["alias"] == "np"


def test_extracts_from_imports_with_names() -> None:
    parsed = parse("from common.config import load_config, Setting\n")
    ref = next(r for r in parsed.references if r.edge_type is EdgeType.IMPORTS)
    assert ref.target_key == "common.config"
    assert ref.meta["names"] == "load_config,Setting"


def test_relative_imports_keep_their_level() -> None:
    """A relative import is recorded verbatim; resolving it is the linker's job."""
    parsed = parse("from ..pkg import thing\n")
    ref = next(r for r in parsed.references if r.edge_type is EdgeType.IMPORTS)
    assert ref.target_key == "..pkg"
    assert ref.meta["level"] == "2"


def test_extracts_decorators() -> None:
    parsed = parse("@app.route\ndef handler():\n    return 1\n")
    decorators = [r for r in parsed.references if r.meta.get("role") == "decorator"]
    assert [d.target_key for d in decorators] == ["app.route"]


def test_extracts_base_classes() -> None:
    parsed = parse("class Child(Base):\n    pass\n")
    bases = [r for r in parsed.references if r.meta.get("role") == "base"]
    assert [b.target_key for b in bases] == ["Base"]


def test_extracts_calls() -> None:
    parsed = parse("def run():\n    load_config()\n    os.path.join('a')\n")
    calls = {r.target_key for r in parsed.references if r.edge_type is EdgeType.CALLS}
    assert {"load_config", "os.path.join"} <= calls


def test_call_records_its_enclosing_symbol() -> None:
    parsed = parse("def run():\n    load_config()\n")
    call = next(r for r in parsed.references if r.target_key == "load_config")
    assert call.source_symbol == "run"


def test_extracts_env_var_reads_all_three_forms() -> None:
    parsed = parse(
        "import os\n"
        "a = os.getenv('SERVICE_HOST')\n"
        "b = os.environ['SERVICE_PORT']\n"
        "c = os.environ.get('SERVICE_TIMEOUT')\n"
    )
    env = {r.target_key: r for r in parsed.references if r.target_kind is EntityType.ENV_VAR}
    assert set(env) == {"SERVICE_HOST", "SERVICE_PORT", "SERVICE_TIMEOUT"}
    assert env["SERVICE_HOST"].edge_type is EdgeType.READS
    assert env["SERVICE_HOST"].line_start == 2


def test_non_literal_env_name_is_not_invented() -> None:
    """A dynamic variable name cannot be resolved, so nothing is recorded."""
    parsed = parse("import os\nname = 'X'\nvalue = os.getenv(name)\n")
    assert not [r for r in parsed.references if r.target_kind is EntityType.ENV_VAR]


def test_syntax_error_yields_diagnostic_not_exception() -> None:
    parsed = parse("def broken(:\n    pass\n")
    assert parsed.symbols == ()
    assert len(parsed.diagnostics) == 1
    assert parsed.diagnostics[0].severity is Severity.ERROR
    assert "syntax error" in parsed.diagnostics[0].message


def test_module_symbol_is_always_present() -> None:
    parsed = parse("x = 1\n", name="mymodule.py")
    module = next(s for s in parsed.symbols if s.kind is EntityType.MODULE)
    assert module.name == "mymodule"


def test_parser_records_its_own_provenance() -> None:
    parsed = parse("x = 1\n")
    assert parsed.parser == "python-ast"
    assert parsed.parser_version == "1"
    assert parsed.language == "Python"
