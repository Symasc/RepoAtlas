"""Ordinary claims resolved through a declaration use: what the trace may and may not link.

``ReferenceLinker._resolve_via_use`` resolves a ``CALLS``, ``REFERENCES`` or ``EXPORTS`` claim
only when a recorded use of the same name proves its target. Two kinds of test pin it:

* two Python repositories (a provider and a consumer, and the same shape inside one repository)
  show that a use which reached its declaration only as the receiver of a longer chain
  (``Outer.Inner``, ``@Outer.register``) is no reference to the declaration, and that a
  ``REFERENCES`` crossing, once proven, is what impact follows;
* the guards inside the method are each pinned on their own, by handing it a use and an answer
  and nothing else, because a corpus cannot always reach them: a resolver answers with a
  namespace, a module value, a type-only use, a declaration of another repository without
  package evidence, and so on. Each test names the guard it holds.

Every name here is invented.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.graph.linker import GLOBAL_PARSER, ReferenceLinker
from repoatlas.graph.modules import ModuleTarget
from repoatlas.graph.packages import CROSSING
from repoatlas.graph.py_packages import PythonLink
from repoatlas.graph.py_uses import PythonUseResolver
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import ImpactRequest
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import EdgeRecord, FileRow, SymbolRow, UnresolvedRefRecord
from tests.impact_corpus import build
from tests.python_packages_corpus import PROVIDER

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

# -- the receiver rule, through two real repositories --------------------------------------

#: The provider's models, with a class that has no ``Inner`` and no ``register``.
OUTER_MODELS = (
    "class Widget:\n    pass\n\n\ndef make() -> Widget:\n    return Widget()\n\n\n"
    "class Outer:\n    pass\n"
)
OUTER_PROVIDER = {**PROVIDER, "src/internal_fixture/models.py": OUTER_MODELS}

USE_PATH = "src/consumer_app/use.py"


def _use_source(imported_from: str) -> str:
    return (
        f"from {imported_from} import Outer\n"  # 1
        "\n\n"
        "class C(Outer.Inner):\n"  # an attribute of Outer the provider never declares
        "    pass\n"
        "\n\n"
        "@Outer.register\n"  # nor this one
        "def f():\n"
        "    pass\n"
        "\n\n"
        "class D(Outer):\n"  # the control: the name itself
        "    pass\n"
    )


CROSS_CONSUMER = {
    "pyproject.toml": (
        '[project]\nname = "consumer-app"\ndependencies = ["internal-fixture>=1.0"]\n'
    ),
    "src/consumer_app/__init__.py": "",
    USE_PATH: _use_source("internal_fixture.models"),
}

#: The same three declarations inside one repository: ``helper`` is a sibling top-level
#: package under the source root, as in ``test_python_source_root_wins_...``.
SAME_REPOSITORY = {
    "pyproject.toml": '[project]\nname = "app"\n',
    "src/app/__init__.py": "",
    "src/helper/__init__.py": "class Outer:\n    pass\n",
    "src/app/use.py": _use_source("helper"),
}


def _references(
    workspace: Workspace, path: str
) -> dict[str, list[tuple[str, str, dict[str, Any] | None]]]:
    """The global pass's ``REFERENCES`` edges of one file: source -> (repository, target, how)."""
    rows = workspace.database.execute(
        """
        SELECT s.qualified_name, dr.slug, d.qualified_name, e.resolution
        FROM edges e JOIN files sf ON sf.id = e.file_id
        JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id
        JOIN repositories dr ON dr.id = d.repo_id
        WHERE sf.rel_path = ? AND e.type = 'REFERENCES' AND e.parser = ?
        """,
        (path, GLOBAL_PARSER),
    ).fetchall()
    found: dict[str, list[tuple[str, str, dict[str, Any] | None]]] = {}
    for source, repository, target, resolution in rows:
        found.setdefault(source, []).append(
            (repository, target, json.loads(resolution) if resolution else None)
        )
    return found


@pytest.fixture
def crossed(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, {"provider": OUTER_PROVIDER, "consumer": CROSS_CONSUMER})


def test_an_attribute_of_an_imported_class_is_no_reference_to_the_class(
    crossed: Workspace,
) -> None:
    """S2. ``class C(Outer.Inner)`` and ``@Outer.register`` reach ``Outer`` only as the
    receiver of a chain that ends in an attribute nothing proves; neither is a reference to
    ``Outer``. The one reference the file makes is ``D(Outer)``, the name itself."""
    found = _references(crossed, USE_PATH)
    assert "C" not in found
    assert "f" not in found
    assert set(found) == {"D"}
    for _, target, resolution in found["D"]:
        assert target == "Outer"
        assert resolution is None or resolution.get("form") != "receiver"


def test_a_reference_to_the_imported_class_itself_still_crosses_with_its_trace(
    crossed: Workspace,
) -> None:
    """The control of the test above: the rule drops receivers, not references."""
    [(repository, target, resolution)] = _references(crossed, USE_PATH)["D"]
    assert (repository, target) == ("provider", "Outer")
    assert resolution is not None
    assert (resolution["use"], resolution["form"], resolution["binding"]) == (
        "value",
        "base",
        "import",
    )
    assert resolution["crossing"]["package"] == "pypi:internal-fixture"
    assert resolution["crossing"]["provider"] == "provider"
    assert resolution["crossing"]["imported_at"] == 1


def test_the_receiver_rule_holds_inside_one_repository(
    workspace: Workspace, tmp_path: Path
) -> None:
    solo = build(workspace, tmp_path, {"solo": SAME_REPOSITORY})
    found = _references(solo, "src/app/use.py")
    assert "C" not in found
    assert "f" not in found
    assert set(found) == {"D"}
    [(repository, target, resolution)] = found["D"]
    assert (repository, target) == ("solo", "Outer")
    assert resolution is None or resolution.get("form") != "receiver"


def test_impact_follows_a_proven_reference_crossing_without_a_uses_edge(
    crossed: Workspace,
) -> None:
    """A package-proven ``REFERENCES`` edge joins two repositories by itself: with every
    cross-repository ``USES`` and ``CALLS`` edge gone, a change to ``Outer`` still reaches the
    class that names it as a base."""
    seed = crossed.database.execute(
        """
        SELECT e.key FROM entities e JOIN repositories r ON r.id = e.repo_id
        WHERE r.slug = 'provider' AND e.qualified_name = 'Outer'
        """
    ).fetchone()[0]
    with crossed.database.transaction() as conn:
        conn.execute(
            """
            DELETE FROM edges WHERE type IN ('USES', 'CALLS') AND EXISTS (
                SELECT 1 FROM entities s, entities d
                WHERE s.id = edges.src_id AND d.id = edges.dst_id AND s.repo_id != d.repo_id)
            """
        )
    response = CapabilityService(crossed).impact(ImpactRequest(context=GLOBAL, seed=seed))
    prefix = f"sym:consumer:{USE_PATH}:D:"
    reached = [c for c in response.candidates if c.entity.startswith(prefix)]
    assert [str(c.step.edge) for c in reached] == ["REFERENCES"]


# -- the guards of _resolve_via_use, one by one --------------------------------------------

MODULE = "pkg.models"
ORIGIN_REPO, OTHER_REPO = 1, 2


def _file(file_id: int, repo_id: int, path: str) -> FileRow:
    return FileRow(
        file_id=file_id,
        file_entity_id=1000 + file_id,
        repo_id=repo_id,
        repo_name=f"r{repo_id}",
        repo_slug=f"r{repo_id}",
        repo_path=Path(f"/nowhere/r{repo_id}"),
        rel_path=path,
    )


ORIGIN = _file(1, ORIGIN_REPO, "src/use.py")


def _symbol(
    entity_id: int = 7,
    entity_type: EntityType = EntityType.CLASS,
    *,
    repo_id: int = ORIGIN_REPO,
    **meta: str,
) -> dict[int, SymbolRow]:
    return {
        entity_id: SymbolRow(
            entity_id=entity_id,
            file_id=50 + repo_id,
            repo_id=repo_id,
            type=entity_type,
            name="target",
            qualified_name="target",
            line_start=1,
            meta_json=json.dumps(meta) if meta else None,
        )
    }


def _claim(
    edge_type: EdgeType,
    *,
    key: str = "target",
    module: str = MODULE,
    parser: str = "python",
) -> UnresolvedRefRecord:
    return UnresolvedRefRecord(
        repo_id=ORIGIN_REPO,
        file_id=1,
        src_id=500,
        edge_type=edge_type,
        target_kind=EntityType.SYMBOL,
        target_key=key,
        line_start=7,
        line_end=7,
        meta_json=json.dumps({"module": module, "parser": parser}),
    )


def _import(line: int, target: str = MODULE, **meta: str) -> UnresolvedRefRecord:
    return UnresolvedRefRecord(
        repo_id=ORIGIN_REPO,
        file_id=1,
        src_id=ORIGIN.file_entity_id,
        edge_type=EdgeType.IMPORTS,
        target_kind=EntityType.MODULE,
        target_key=target,
        line_start=line,
        line_end=line,
        meta_json=json.dumps(meta),
    )


def _use(**over: str) -> dict[str, str]:
    return {
        "module": MODULE,
        "local": "target",
        "binding": "import",
        "imported": "target",
        "use": "call",
        "form": "call",
        "occurrences": "1",
        **over,
    }


Answer = Callable[[dict[str, str]], tuple[int, dict[str, Any]] | None]


def _reaches(entity_id: int = 7, /, **fields: Any) -> Answer:
    """A resolver that answers every use with this declaration and these fields."""
    return lambda _use_meta: (entity_id, dict(fields))


class _Resolver:
    """Stands in for a use resolver: whatever it is asked, it answers as told."""

    def __init__(self, answer: Answer, asked: list[dict[str, str]]) -> None:
        self._answer = answer
        self._asked = asked

    def module_target(self, origin: FileRow, specifier: str) -> object:
        return object()  # anything that is not a refusal (a ``str``)

    def resolve(self, origin: FileRow, use: dict[str, str]) -> tuple[int, dict[str, Any]] | None:
        self._asked.append(dict(use))
        return self._answer(use)


def _via(
    claim: UnresolvedRefRecord,
    answer: Answer,
    symbols: dict[int, SymbolRow],
    *,
    uses: Sequence[dict[str, str]] | None = None,
    siblings: Sequence[UnresolvedRefRecord] = (),
    asked: list[dict[str, str]] | None = None,
) -> EdgeRecord | None:
    recorded: list[dict[str, str]] = [] if asked is None else asked
    resolver: Any = _Resolver(answer, recorded)
    module = json.loads(claim.meta_json or "{}")["module"]
    candidates = [_use()] if uses is None else list(uses)
    linker: Any = ReferenceLinker(cast(Any, None))
    return linker._resolve_via_use(
        claim,
        ORIGIN,
        siblings,
        lambda _file_id: {(claim.src_id, claim.target_key, module): [dict(u) for u in candidates]},
        lambda: symbols,
        lambda: resolver,
        lambda: resolver,
    )


CLAIMED = (EdgeType.CALLS, EdgeType.REFERENCES)


@pytest.mark.parametrize("edge_type", CLAIMED)
def test_a_use_that_proves_the_target_links_it(edge_type: EdgeType) -> None:
    """The control every guard below departs from."""
    edge = _via(_claim(edge_type), _reaches(basis="x"), _symbol())
    assert edge is not None
    assert (edge.src_id, edge.dst_id, edge.type, edge.parser) == (
        500,
        7,
        edge_type,
        GLOBAL_PARSER,
    )
    assert edge.resolution is not None
    assert json.loads(edge.resolution)["basis"] == "x"
    assert json.loads(edge.resolution)["use"] == "call"


@pytest.mark.parametrize("edge_type", CLAIMED)
def test_g1_a_use_that_reached_a_whole_module_links_nothing(edge_type: EdgeType) -> None:
    """M-G1 (``fields.get("namespace")``): a name bound to a module answers with the module's
    file; a class is the declaration that passes every other guard, so only this one holds."""
    assert _via(_claim(edge_type), _reaches(namespace=True), _symbol()) is None


@pytest.mark.parametrize("edge_type", CLAIMED)
@pytest.mark.parametrize("kind", ["variable", "type-alias"])
def test_g2_a_module_value_is_never_the_target_of_a_call_or_a_reference(
    edge_type: EdgeType, kind: str
) -> None:
    """M-G2 (``USES_ONLY_KINDS``): a class-typed row with a module value's kind, so no other
    guard can refuse it."""
    assert _via(_claim(edge_type), _reaches(), _symbol(kind=kind)) is None
    assert _via(_claim(edge_type), _reaches(), _symbol(kind="class")) is not None


def test_g3_a_call_reaches_only_a_function_or_a_class() -> None:
    """M-G3 (the ``CALLS`` ``symbol.type`` check): an enum or an interface is no callee, but a
    reference may name one."""
    enum = _symbol(entity_type=EntityType.SYMBOL, kind="enum")
    assert _via(_claim(EdgeType.CALLS), _reaches(), enum) is None
    assert _via(_claim(EdgeType.REFERENCES), _reaches(), enum) is not None
    for callable_type in (EntityType.FUNCTION, EntityType.CLASS):
        assert _via(_claim(EdgeType.CALLS), _reaches(), _symbol(entity_type=callable_type))


def test_a_call_needs_a_use_that_is_a_call() -> None:
    """The second half of the ``CALLS`` check: a trace that ended as a value read is no call,
    and a reference does not mind."""
    as_value = _reaches(use="value", form="read")
    assert _via(_claim(EdgeType.CALLS), as_value, _symbol()) is None
    assert _via(_claim(EdgeType.REFERENCES), as_value, _symbol()) is not None


@pytest.mark.parametrize("edge_type", CLAIMED)
def test_s2_a_use_that_stopped_at_its_declaration_as_a_receiver_links_nothing(
    edge_type: EdgeType,
) -> None:
    """S2: ``Outer.Inner`` reached ``Outer`` and stopped; that is a receiver, whatever the claim."""
    receiver = _reaches(use="value", form="receiver")
    assert _via(_claim(edge_type), receiver, _symbol()) is None
    assert _via(_claim(edge_type), _reaches(form="base"), _symbol()) is not None


def test_g4_a_type_only_import_is_never_called() -> None:
    """M-G4 (``type_only``): a call of a name imported for its type is no call, and a
    reference to it is still one."""
    typed = [_use(type_only="true")]
    assert _via(_claim(EdgeType.CALLS), _reaches(), _symbol(), uses=typed) is None
    assert _via(_claim(EdgeType.REFERENCES), _reaches(), _symbol(), uses=typed) is not None
    assert _via(_claim(EdgeType.CALLS), _reaches(), _symbol(), uses=[_use()]) is not None


def test_g5_a_declaration_of_another_repository_needs_package_evidence() -> None:
    """M-G5 (``not crossing.get("package")``): a crossing without a package is no evidence.
    The import line is supplied, so the guard after this one cannot be what refuses."""
    elsewhere = _symbol(repo_id=OTHER_REPO)
    siblings = [_import(3, bindings="target")]
    for fields in ({}, {CROSSING: None}, {CROSSING: {}}, {CROSSING: {"provider": "other"}}):
        refused = _via(_claim(EdgeType.CALLS), _reaches(**fields), elsewhere, siblings=siblings)
        assert refused is None
    edge = _via(
        _claim(EdgeType.CALLS),
        _reaches(**{CROSSING: {"package": "npm:@fx/lib"}}),
        elsewhere,
        siblings=siblings,
    )
    assert edge is not None


def test_g6_a_crossing_needs_the_line_of_the_import_that_binds_the_name() -> None:
    """M-G6 (``_import_line is None``): the line is recorded with the crossing, so a crossing
    no import of this file binds is refused rather than recorded without one."""
    crossing = {CROSSING: {"package": "npm:@fx/lib"}}
    elsewhere = _symbol(repo_id=OTHER_REPO)
    assert _via(_claim(EdgeType.CALLS), _reaches(**crossing), elsewhere) is None
    other = [_import(2, bindings="another")]
    assert _via(_claim(EdgeType.CALLS), _reaches(**crossing), elsewhere, siblings=other) is None
    twice = [_import(5, bindings="target"), _import(3, bindings="target")]
    edge = _via(_claim(EdgeType.CALLS), _reaches(**crossing), elsewhere, siblings=twice)
    assert edge is not None and edge.resolution is not None
    assert json.loads(edge.resolution)[CROSSING] == {"package": "npm:@fx/lib", "imported_at": 3}


# -- an EXPORTS claim: the one import that binds the name ------------------------------------

LIB = "@fx/lib"


def _export(module: str = LIB, parser: str = "typescript") -> UnresolvedRefRecord:
    return _claim(EdgeType.EXPORTS, key="X", module=module, parser=parser)


def _exported(
    siblings: Sequence[UnresolvedRefRecord], **kwargs: Any
) -> tuple[EdgeRecord | None, list[dict[str, str]]]:
    asked: list[dict[str, str]] = []
    claim = kwargs.pop("claim", None) or _export()
    return _via(claim, _reaches(), _symbol(), uses=[], siblings=siblings, asked=asked), asked


def test_an_export_of_an_imported_name_is_resolved_as_a_use_of_that_import() -> None:
    edge, asked = _exported([_import(1, LIB, bindings="X")])
    assert edge is not None and edge.resolution is not None
    assert asked == [
        {
            "module": LIB,
            "binding": "import",
            "local": "X",
            "imported": "X",
            "use": "value",
            "form": "export",
        }
    ]
    assert json.loads(edge.resolution)["form"] == "export"


@pytest.mark.parametrize(
    ("meta", "imported"),
    [({"bindings": "Y:X"}, "Y"), ({"default": "X"}, "default")],
)
def test_an_export_asks_for_the_name_the_module_exports(
    meta: dict[str, str], imported: str
) -> None:
    """The name asked of the module is the imported one, not the local: ``Y as X`` asks for
    ``Y`` and a default import for ``default``."""
    edge, asked = _exported([_import(1, LIB, **meta)])
    assert edge is not None
    assert [use["imported"] for use in asked] == [imported]


def test_g7_role_a_re_export_and_a_dynamic_import_bind_no_name() -> None:
    """M-G7 (the ``role`` check): neither is a second binder, and neither is the only one."""
    plain = _import(1, LIB, bindings="X")
    re_export = _import(2, LIB, role="re-export", bindings="X")
    dynamic = _import(3, LIB, role="dynamic", names="X")
    edge, _ = _exported([plain, re_export, dynamic])
    assert edge is not None
    assert _exported([re_export, dynamic])[0] is None


def test_g7_count_a_name_bound_twice_is_not_known() -> None:
    """M-G7 (``len(binders) != 1``): two imports of the name, from one module or two."""
    twice = [_import(1, LIB, bindings="X"), _import(2, LIB, bindings="X")]
    two_modules = [_import(1, LIB, bindings="X"), _import(2, "@fx/other", bindings="X")]
    assert _exported(twice)[0] is None
    assert _exported(two_modules)[0] is None
    assert _exported([])[0] is None


def test_g7_specifier_the_binding_must_come_from_the_claims_own_module() -> None:
    """M-G7 (``binders[0][0] != specifier``): the claim names one module, the import another."""
    edge, asked = _exported([_import(1, "@fx/other", bindings="X")])
    assert edge is None
    assert asked == []


def test_g7_imported_a_namespace_binding_exports_no_declaration() -> None:
    """M-G7 (``binders[0][1] is None``): ``import * as X`` binds the module, not a name of it."""
    edge, asked = _exported([_import(1, LIB, namespace="X")])
    assert edge is None
    assert asked == []


def test_an_export_is_read_for_script_files_only() -> None:
    edge, _ = _exported([_import(1, LIB, bindings="X")], claim=_export(parser="python"))
    assert edge is None


# -- more than one declaration ----------------------------------------------------------------


def test_g8_a_claim_two_uses_resolve_differently_is_not_linked() -> None:
    """M-G8 (``len(reached) != 1``): the same name reached two declarations."""
    uses = [_use(imported="a"), _use(imported="b")]
    symbols = {**_symbol(7), **_symbol(8)}

    def split(use: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return (7 if use["imported"] == "a" else 8), {}

    assert _via(_claim(EdgeType.CALLS), split, symbols, uses=uses) is None
    edge = _via(_claim(EdgeType.CALLS), _reaches(7), symbols, uses=uses)
    assert edge is not None and edge.dst_id == 7  # both reach one declaration: one edge


def test_g8_a_use_that_resolves_to_nothing_does_not_count() -> None:
    uses = [_use(imported="a"), _use(imported="b")]

    def one(use: dict[str, str]) -> tuple[int, dict[str, Any]] | None:
        return (7, {}) if use["imported"] == "a" else None

    edge = _via(_claim(EdgeType.CALLS), one, _symbol(), uses=uses)
    assert edge is not None and edge.dst_id == 7
    assert _via(_claim(EdgeType.CALLS), _reaches(), _symbol(), uses=[]) is None


# -- the module edge, and a provider's submodule ---------------------------------------------


def _import_claim() -> UnresolvedRefRecord:
    return _import(3, "pkg.models", level="0")


def test_g9_a_module_in_another_repository_is_linked_only_with_its_crossing() -> None:
    """M-G9 (``_module_edge``): a target of another repository without a crossing is no edge,
    though the same target in the importer's own repository is."""
    linker: Any = ReferenceLinker(cast(Any, None))
    foreign = ModuleTarget(_file(30, OTHER_REPO, "src/pkg/models.py"), "python-dependency")
    own = ModuleTarget(_file(31, ORIGIN_REPO, "src/pkg/models.py"), "python-dependency")
    claim = _import_claim()
    assert linker._module_edge(claim, ORIGIN, foreign) is None
    assert linker._module_edge(claim, ORIGIN, foreign, {}) is None
    edge = linker._module_edge(claim, ORIGIN, foreign, {"package": "pypi:lib"})
    assert edge is not None and edge.resolution is not None
    assert edge.dst_id == foreign.file.file_entity_id
    assert json.loads(edge.resolution)[CROSSING] == {"package": "pypi:lib", "imported_at": 3}
    inside = linker._module_edge(claim, ORIGIN, own)
    assert inside is not None and inside.resolution is not None
    assert CROSSING not in json.loads(inside.resolution)


def test_g9_a_provider_submodule_is_the_file_its_own_link_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M-G9 (``provider_submodule``): the submodule the package leaves unbound and the module
    the child specifier links to must be one file."""
    resolver: Any = PythonUseResolver(cast(Any, None), cast(Any, None), lambda _file_id: None)
    link = {"package": "pypi:lib"}
    parent = PythonLink(ModuleTarget(_file(30, OTHER_REPO, "src/pkg/__init__.py"), "b"), link)
    submodule = _file(31, OTHER_REPO, "src/pkg/sub.py")
    answers: dict[str, object] = {}

    def module_target(self: object, origin: FileRow, spec: str) -> object:
        return answers[spec]

    monkeypatch.setattr(PythonUseResolver, "module_target", module_target)
    monkeypatch.setattr(
        PythonUseResolver, "_unbound_submodule", lambda self, origin, module, name: submodule
    )

    answers.update({"pkg": parent, "pkg.sub": PythonLink(ModuleTarget(submodule, "b"), link)})
    found = resolver.provider_submodule(ORIGIN, "pkg", "sub")
    assert found is not None and found.target.file.file_id == submodule.file_id

    elsewhere = _file(32, OTHER_REPO, "src/pkg/other.py")
    answers["pkg.sub"] = PythonLink(ModuleTarget(elsewhere, "b"), link)
    assert resolver.provider_submodule(ORIGIN, "pkg", "sub") is None

    answers["pkg.sub"] = ModuleTarget(submodule, "b")  # not a provider's module at all
    assert resolver.provider_submodule(ORIGIN, "pkg", "sub") is None

    answers.update({"pkg": ModuleTarget(parent.target.file, "b"), "pkg.sub": parent})
    assert resolver.provider_submodule(ORIGIN, "pkg", "sub") is None
