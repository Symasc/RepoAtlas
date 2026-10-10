# ruff: noqa: E501 - the expected keys and edges are spelled whole, as stored
"""DF3b: request and response model fields (DF3B_PHASE_A.md, §13 for the review amendments).

Which fields exist (python-ast 18, typescript 20); which declarations use them -- through a
parameter typed by exactly one named model, by a proven construction, or by an object literal
whose contextual type is the model -- and only ever the resolved model's own field; that a
field changes nothing search, locate, the call graph, value mappings or the topology answered
before; and that impact reaches a field's exact users first and its model once, after them.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import ImpactRequest, ImpactResponse, Presentation
from repoatlas.models.search import SearchRequest
from repoatlas.models.topology import TopologyNamesRequest
from repoatlas.parsers.python import PythonParser
from repoatlas.parsers.typescript import TypeScriptParser
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import fact_snapshot
from tests.impact_corpus import build, use_edges, verify_steps
from tests.model_fields_corpus import API, repos

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


# -- Python fields and the initializer that binds them -------------------------------------


def python_fields(source: str) -> dict[str, str | None]:
    parsed = PythonParser().parse(Path("models.py"), source)
    return {
        s.qualified_name: s.meta.get("init")
        for s in parsed.symbols
        if s.meta.get("kind") == "field"
    }


def test_a_python_field_is_an_annotated_name_a_class_body_writes_once() -> None:
    assert python_fields(
        "from typing import ClassVar\n"
        "import typing\n"
        "class Shape:\n"
        "    side: int\n"
        "    label: str = 'x'\n"
        "    twice: int\n"
        "    twice: str\n"
        "    shared: ClassVar[int] = 1\n"
        "    other: typing.ClassVar = 2\n"
        "    plain = 3\n"
        "    def side_length(self) -> int:\n"
        "        self.cached: int = 1\n"
        "        return self.side\n"
        "    class Inner:\n"
        "        deep: bool\n"
        "def make():\n"
        "    class Local:\n"
        "        value: float\n"
    ) == {
        "Shape.side": None,
        "Shape.label": None,
        "Shape.Inner.deep": None,
        "make.Local.value": None,
    }


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # proven: the file's own import, the class's one base or one decorator
        ("from pydantic import BaseModel\nclass M(BaseModel):\n    a: int\n", "pydantic"),
        ("from pydantic import BaseModel as Base\nclass M(Base):\n    a: int\n", "pydantic"),
        ("import pydantic\nclass M(pydantic.BaseModel):\n    a: int\n", "pydantic"),
        ("from pydantic.v1 import BaseModel\nclass M(BaseModel):\n    a: int\n", "pydantic"),
        (
            "from pydantic import BaseModel\nclass M(BaseModel, frozen=True):\n    a: int\n",
            "pydantic",
        ),
        ("from dataclasses import dataclass\n@dataclass\nclass M:\n    a: int\n", "dataclass"),
        (
            "from dataclasses import dataclass\n@dataclass(frozen=True)\nclass M:\n    a: int\n",
            "dataclass",
        ),
        ("import dataclasses\n@dataclasses.dataclass\nclass M:\n    a: int\n", "dataclass"),
        (
            "from pydantic.dataclasses import dataclass\n@dataclass\nclass M:\n    a: int\n",
            "dataclass",
        ),
        # not proven: nothing, or something that could make a keyword mean another field
        ("class M:\n    a: int\n", None),
        ("class BaseModel:\n    pass\nclass M(BaseModel):\n    a: int\n", None),
        (
            "from pydantic import BaseModel\nBaseModel = object\nclass M(BaseModel):\n    a: int\n",
            None,
        ),
        (
            "from pydantic import BaseModel\nfrom other import *\nclass M(BaseModel):\n    a: int\n",
            None,
        ),
        ("from pydantic import BaseModel\nclass M(BaseModel, dict):\n    a: int\n", None),
        (
            "from pydantic import BaseModel\ndef deco(c): return c\n@deco\nclass M(BaseModel):\n    a: int\n",
            None,
        ),
        (
            "from pydantic import BaseModel\nclass M(BaseModel):\n    a: int\n    def __init__(self, **kw): ...\n",
            None,
        ),
        (
            "from pydantic import BaseModel, model_validator\nclass M(BaseModel):\n    a: int\n    @model_validator(mode='before')\n    def m(cls, v): return v\n",
            None,
        ),
        (
            "from pydantic import BaseModel, ConfigDict\nclass M(BaseModel):\n    model_config = ConfigDict(alias_generator=str.upper)\n    a: int\n",
            None,
        ),
        (
            "from pydantic import BaseModel\nclass M(BaseModel):\n    a: int\n    class Config:\n        alias_generator = str.upper\n",
            None,
        ),
        ("from dataclasses import dataclass\n@dataclass(init=False)\nclass M:\n    a: int\n", None),
        (
            "from dataclasses import dataclass\n@dataclass\nclass M:\n    a: int\n    def __init__(self): ...\n",
            None,
        ),
        ("from attrs import define\n@define\nclass M:\n    a: int\n", None),
        ("from typing import NamedTuple\nclass M(NamedTuple):\n    a: int\n", None),
        ("from typing import TypedDict\nclass M(TypedDict):\n    a: int\n", None),
        ("from sqlmodel import SQLModel\nclass M(SQLModel):\n    a: int\n", None),
        (
            "from pydantic import BaseModel\nclass Base(BaseModel):\n    b: int\nclass M(Base):\n    a: int\n",
            None,
        ),
    ],
)
def test_a_construction_keyword_binds_a_field_only_through_a_proven_initializer(
    source: str, expected: str | None
) -> None:
    assert python_fields(source)["M.a"] == expected


def test_a_field_s_own_alias_privacy_or_init_false_keeps_it_out_of_the_initializer() -> None:
    assert python_fields(
        "from typing import Annotated\n"
        "from dataclasses import dataclass, field\n"
        "from pydantic import BaseModel, Field\n"
        "class P(BaseModel):\n"
        "    plain: int\n"
        "    aliased: int = Field(alias='a')\n"
        "    validated: Annotated[int, Field(validation_alias='v')]\n"
        "    _private: int = 0\n"
        "@dataclass\n"
        "class D:\n"
        "    plain: int\n"
        "    _underscored: int = 0\n"
        "    later: int = field(default=0, init=False)\n"
        "    kept: int = field(default=0)\n"
    ) == {
        "P.plain": "pydantic",
        "P.aliased": None,
        "P.validated": None,
        "P._private": None,
        "D.plain": "dataclass",
        "D._underscored": "dataclass",
        "D.later": None,
        "D.kept": "dataclass",
    }


def python_members(source: str) -> dict[tuple[str | None, str, str], list[list[object]]]:
    parsed = PythonParser().parse(Path("use.py"), source)
    return {
        (r.source_symbol, r.target_key, r.meta["use"]): json.loads(r.meta["fields"])
        for r in parsed.references
        if r.meta.get("fields")
    }


def test_python_members_ride_on_the_use_that_names_the_model() -> None:
    found = python_members(
        "from typing import Annotated, Optional\n"  # 1
        "from m import M, N, make\n"  # 2
        "def bare(p: M):\n"  # 3
        "    return p.a, p.b.c\n"  # 4  a and b, never c
        "def quoted(p: 'M'):\n"  # 5
        "    return p.a\n"  # 6
        "def optional(p: Optional[M], q: M | None, r: Annotated[M, 'x']):\n"  # 7
        "    return p.a, q.b, r.c\n"  # 8
        "def rebound(p: M):\n"  # 9
        "    p = make()\n"  # 10
        "    return p.a\n"  # 11 nothing
        "def lambda_shadow(p: M):\n"  # 12
        "    return (lambda p: p.a)(1)\n"  # 13 nothing
        "def union(p: M | N):\n"  # 14
        "    return p.a\n"  # 15 nothing
        "def build(n):\n"  # 16
        "    M(a=1, b=n, **{})\n"  # 17 a, b (whether either binds is the model's to say)
        "    def inner():\n"  # 18
        "        return M(c=3)\n"  # 19 inner's, not build's
        "    return inner\n"  # 20
        "def shadowed(M):\n"  # 21
        "    return M(a=1)\n"  # 22 nothing: M is the parameter
    )
    assert found == {
        ("bare", "M", "type"): [["a", 4, "parameter"], ["b", 4, "parameter"]],
        ("quoted", "M", "type"): [["a", 6, "parameter"]],
        ("optional", "M", "type"): [
            ["a", 8, "parameter"],
            ["b", 8, "parameter"],
            ["c", 8, "parameter"],
        ],
        ("build", "M", "call"): [["a", 17, "construction"], ["b", 17, "construction"]],
        ("build.inner", "M", "call"): [["c", 19, "construction"]],
    }


# -- TypeScript fields and members -----------------------------------------------------------


def ts(
    source: str, path: str = "x.ts"
) -> tuple[dict[str, int], dict[tuple[str | None, str], list[list[object]]]]:
    parsed = TypeScriptParser().parse(Path(path), source)
    fields = {
        s.qualified_name: s.line_start for s in parsed.symbols if s.meta.get("kind") == "field"
    }
    members = {
        (r.source_symbol, r.target_key): json.loads(r.meta["fields"])
        for r in parsed.references
        if r.meta.get("fields")
    }
    return fields, members


def test_a_typescript_field_is_a_property_or_typed_class_field_written_once() -> None:
    fields, _ = ts(
        "interface A { a: string; b?: number; m(): void; 'quoted': string; [k: string]: unknown; twice: 1; twice: 2 }\n"
        "type B = { x: number; nested: { y: number } };\n"
        "type U = { u: number } | { v: number };\n"
        "class C { c: number = 1; static s: number; f = (): number => 1; #p: number = 2; untyped = 3; g: () => void = function () {}; }\n"
        "abstract class D { d!: string; }\n"
        "namespace N { export interface E { e: boolean } }\n"
    )
    # a field is qualified as its declaration is (a namespace adds nothing to either)
    assert fields == {"A.a": 1, "A.b": 1, "B.x": 2, "B.nested": 2, "C.c": 4, "D.d": 5, "E.e": 6}


def test_typescript_members_ride_on_the_type_name_they_are_read_through() -> None:
    _, members = ts(
        'import { M, N, Wrap } from "./m";\n'  # 1
        "export function read(p: M | undefined) { return p?.a + p.b; }\n"  # 2
        "export function destructured({ a, b: renamed, c = 1 }: M) { return a; }\n"  # 3
        "export function later(p: M) { const { d } = p; return d; }\n"  # 4
        "export function rebound(p: M) { p = other(); return p.a; }\n"  # 5 nothing
        "export function either(p: M | N) { return p.a; }\n"  # 6 nothing
        "export function generic<M>(p: M) { return p.a; }\n"  # 7 nothing: M is the type parameter
        "export const made: M = { a: 1, ...rest, ['b']: 2 };\n"  # 8 a
        "export function make(): M { return { a: 1 }; }\n"  # 9
        "export const arrow = (): Promise<M> => ({ b: 2 });\n"  # 10
        "export function wrapped(): Wrap<M> { return { a: 1 }; }\n"  # 11 nothing
        "export function nested(p: M) { return [1].map(() => p.e); }\n"  # 12 e (a callback reads it too)
    )
    assert members == {
        ("read", "M"): [["a", 2, "parameter"], ["b", 2, "parameter"]],
        ("destructured", "M"): [
            ["a", 3, "parameter"],
            ["b", 3, "parameter"],
            ["c", 3, "parameter"],
        ],
        ("later", "M"): [["d", 4, "parameter"]],
        ("made", "M"): [["a", 8, "construction"]],
        ("make", "M"): [["a", 9, "construction"]],
        ("arrow", "M"): [["b", 10, "construction"]],
        ("nested", "M"): [["e", 12, "parameter"]],
    }


# -- resolution, on an indexed corpus --------------------------------------------------------


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    root = tmp_path_factory.mktemp("model-fields")
    workspace = open_workspace(
        settings=Settings.model_validate({"storage": {"database": str(root / "state/atlas.db")}}),
        require_root=False,
    )
    yield build(workspace, root, repos())
    workspace.close()


def field_uses(workspace: Workspace) -> set[tuple[str, str, int, str]]:
    return {
        (e["source"], e["target"], e["line"], e["field_use"]["access"])
        for e in use_edges(workspace)
        if "field_use" in e
    }


def test_each_field_use_lands_on_the_resolved_model_s_own_field_and_nothing_else(
    corpus: Workspace,
) -> None:
    assert field_uses(corpus) == {
        ("api/handlers.py#read", "api/models.py#Query.limit", 6, "parameter"),
        ("api/handlers.py#read", "api/models.py#Query.sort", 7, "parameter"),
        ("api/handlers.py#read", "api/models.py#Out.observation_id", 8, "construction"),
        ("api/handlers.py#read", "api/models.py#Out.measurement", 8, "construction"),
        # a dataclass and a pydantic model bind their keywords; a plain class, an alias, a
        # private field and a subclass's initializer do not
        ("api/handlers.py#build_rows", "api/models.py#Row.measurement", 12, "construction"),
        ("api/handlers.py#build_rows", "api/models.py#Query.limit", 13, "construction"),
        ("api/handlers.py#deep", "api/models.py#Query.sort", 32, "parameter"),
        # a model this file declares, resolved in the file's own pass
        ("api/handlers.py#local", "api/handlers.py#Local.name", 40, "parameter"),
        # a nested class's field lies inside the model, but is not the model's own
        ("api/nested.py#outer", "api/nested.py#Outer.name", 9, "parameter"),
        # the same field name elsewhere is another field: each read lands on its own
        ("report/render.py#render", "report/models.py#ReportRow.measurement", 5, "parameter"),
        ("src/use.ts#show", "src/types.ts#Item.measurement", 3, "parameter"),
        ("src/use.ts#make", "src/types.ts#Item.id", 6, "construction"),
        ("src/use.ts#make", "src/types.ts#Item.measurement", 6, "construction"),
        ("src/use.ts#sized", "src/types.ts#Opts.limit", 8, "parameter"),
        ("src/use.ts#sized", "src/types.ts#Box.size", 9, "parameter"),
        ("src/use.ts#defaults", "src/types.ts#Opts.limit", 12, "construction"),
        ("src/other.ts#local", "src/other.ts#Other.measurement", 5, "parameter"),
    }


def test_a_field_use_says_which_field_of_which_model_and_its_model_edge_is_unchanged(
    corpus: Workspace,
) -> None:
    edges = use_edges(corpus)
    [measured] = [
        e
        for e in edges
        if e["source"] == "api/handlers.py#read" and e["target"] == "api/models.py#Out.measurement"
    ]
    assert measured["field_use"] == {
        "field": "measurement",
        "model": "Out",
        "access": "construction",
    }
    assert (measured["use"], measured["form"], measured["defined_at"]) == (
        "field",
        "construction",
        "api/models.py",
    )
    # the use of the model itself records what it always did, and no member list
    model = [
        e
        for e in edges
        if e["source"] == "api/handlers.py#read" and e["target"] == "api/models.py#Out"
    ]
    assert model and all("field_use" not in e and "fields" not in e for e in model)
    assert not [e for e in edges if "fields" in e]


def test_field_entities_and_their_initializers(corpus: Workspace) -> None:
    rows = corpus.database.execute(
        "SELECT key, json_extract(meta_json, '$.init') FROM entities WHERE json_extract(meta_json, '$.kind') = 'field'"
    ).fetchall()
    assert dict(rows) == {
        "sym:api:api/models.py:Query.limit:8": "pydantic",
        "sym:api:api/models.py:Query.sort:9": "pydantic",
        "sym:api:api/models.py:Query.hidden:10": None,
        "sym:api:api/models.py:Query._secret:11": None,
        "sym:api:api/models.py:Out.observation_id:16": "pydantic",
        "sym:api:api/models.py:Out.measurement:17": "pydantic",
        "sym:api:api/models.py:Row.measurement:22": "dataclass",
        "sym:api:api/models.py:Row.note:23": None,
        "sym:api:api/models.py:Plain.measurement:27": None,
        "sym:api:api/models.py:Child.extra:34": None,
        "sym:api:api/handlers.py:Local.name:36": None,
        "sym:api:api/nested.py:Outer.Inner.size:3": None,
        "sym:api:api/nested.py:Outer.name:5": None,
        "sym:report:report/models.py:ReportRow.observation_id:5": "pydantic",
        "sym:report:report/models.py:ReportRow.measurement:6": "pydantic",
        "sym:web:src/types.ts:Item.id:2": None,
        "sym:web:src/types.ts:Item.measurement:3": None,
        "sym:web:src/types.ts:Opts.limit:6": None,
        "sym:web:src/types.ts:Opts.sort:6": None,
        "sym:web:src/types.ts:Box.size:8": None,
        "sym:web:src/other.ts:Other.measurement:2": None,
        "sym:web:src/other.ts:Second.measurement:8": None,
        "sym:web:src/ids.ts:WithId.id:5": None,
    }
    defined = corpus.database.execute(
        """SELECT COUNT(*) FROM edges e JOIN entities d ON d.id = e.dst_id
           WHERE e.type = 'DEFINES' AND json_extract(d.meta_json, '$.kind') = 'field'"""
    ).fetchone()[0]
    assert defined == len(rows)


# -- isolation: what answered before answers the same -------------------------------------------


def test_a_field_is_no_callee_a_name_matches(corpus: Workspace) -> None:
    rows = corpus.database.execute(
        """SELECT s.qualified_name, d.qualified_name, json_extract(d.meta_json, '$.kind')
           FROM edges e JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id
           WHERE e.type = 'CALLS'"""
    ).fetchall()
    assert ("run", "id", "function") in {tuple(r) for r in rows}
    assert not [r for r in rows if r[2] == "field"]


def test_search_locate_and_names_never_name_a_field(corpus: Workspace) -> None:
    service = CapabilityService(corpus)
    for question in ("measurement", "Out.measurement", "limit", "Item"):
        found = service.search(SearchRequest(context=GLOBAL, question=question))
        keys = [item.entity.key for item in found.evidence if item.entity is not None]
        assert not [k for k in keys if ".measurement:" in k or ".limit:" in k or ".id:" in k], (
            question
        )
    names = service.topology_names(TopologyNamesRequest(context=GLOBAL, query="measurement"))
    assert not [n for n in names.names if "." in (n.qualified_name or "")]
    search = corpus.search
    assert not search.find_entities_by_name(["measurement"], None, 50)
    assert [
        h.qualified_name for h in search.find_entities_by_name(["Out.measurement"], None, 50)
    ] == ["Out.measurement"]
    assert not search.find_entities_by_name(["Out.measurement"], None, 50, module_values=False)


def test_the_topology_draws_no_field_use_and_a_file_lists_its_fields_and_their_uses(
    corpus: Workspace,
) -> None:
    store = corpus.topology()
    [(repo_id, file_id)] = corpus.database.execute(
        "SELECT f.repo_id, f.id FROM files f JOIN repositories r ON r.id = f.repo_id WHERE r.slug = 'api' AND f.rel_path = 'api/handlers.py'"
    ).fetchall()
    drawn = store.edges_under(
        repo_id, prefix=None, file_id=file_id, outgoing=True, exclude=(), limit=500
    )
    assert not [e for e in drawn if ".measurement:" in str(e[-1]) or ".limit:" in str(e[-1])]
    listed = store.references_in(file_id, exclude=(), limit=500)
    assert any(str(row[4]).endswith(":Out.measurement:17") for row in listed)
    [(models_id,)] = corpus.database.execute(
        "SELECT f.id FROM files f JOIN repositories r ON r.id = f.repo_id WHERE r.slug = 'api' AND f.rel_path = 'api/models.py'"
    ).fetchall()
    declared = {row[4] for row in store.declarations_in(models_id)}
    assert {"Out.measurement", "Row.note", "Query._secret"} <= declared


# -- impact ----------------------------------------------------------------------------------


def impact(workspace: Workspace, seed: str) -> ImpactResponse:
    response = CapabilityService(workspace).impact(
        ImpactRequest(context=GLOBAL, seed=seed, max_candidates=100, presentation=Presentation.FULL)
    )
    verify_steps(workspace, response)
    return response


def basis(response: ImpactResponse) -> dict[str, tuple[str, str]]:
    return {
        c.entity.rsplit(":", 1)[0].split(":", 3)[3]: (c.basis, str(c.step.edge or c.step.derived))
        for c in response.candidates
    }


def test_a_field_reaches_its_exact_users_then_its_model_once_then_the_model_s_users(
    corpus: Workspace,
) -> None:
    response = impact(corpus, "sym:api:api/models.py:Query.limit:8")
    assert response.summary.policy == "impact/6"
    found = basis(response)
    assert found == {
        "read": ("exact", "USES"),
        "build_rows": ("exact", "USES"),
        "Query": ("containment", "member-of"),
        "deep": ("containment", "USES"),
        "rebound": ("containment", "CALLS"),  # its call outranks its annotation's type use
        "qualified": ("containment", "USES"),
    }
    order = [c.name.rsplit("::", 1)[-1] for c in response.candidates]
    assert order.index("Query") > max(order.index("read"), order.index("build_rows"))
    by_name = {c.name.rsplit("::", 1)[-1]: c for c in response.candidates}
    assert (
        by_name["read"].chain
        == "Query.limit -> read (uses, field Query.limit through a typed parameter)"
    )
    assert (
        by_name["build_rows"].chain
        == "Query.limit -> build_rows (uses, field Query.limit by construction)"
    )
    assert by_name["read"].step.resolution["field_use"] == {
        "field": "limit",
        "model": "Query",
        "access": "parameter",
    }
    # the model and what is reached through it say nothing about the field
    for name in ("Query", "deep", "rebound", "qualified"):
        assert "field" not in by_name[name].chain.split(" -> ", 1)[1]
        assert not (by_name[name].step.resolution or {}).get("field_use")


def test_a_field_no_one_reads_is_reached_only_through_its_model(corpus: Workspace) -> None:
    found = basis(impact(corpus, "sym:api:api/models.py:Query.hidden:10"))
    assert found["Query"] == ("containment", "member-of")
    # its model's users, by its name or its construction, all at the containment tier
    assert {name for name in found if name != "Query"} == {
        "read",
        "build_rows",
        "deep",
        "rebound",
        "qualified",
    }
    assert {b[0] for name, b in found.items()} == {"containment"}


def test_same_named_fields_stay_apart(corpus: Workspace) -> None:
    found = basis(impact(corpus, "sym:report:report/models.py:ReportRow.measurement:6"))
    assert found == {"render": ("exact", "USES"), "ReportRow": ("containment", "member-of")}
    found = basis(impact(corpus, "sym:web:src/types.ts:Item.measurement:3"))
    assert found["show"] == found["make"] == ("exact", "USES")
    # a union names Item as a type: reached through the model, never through the field
    assert found["either"] == ("containment", "USES")
    assert "local" not in found


def test_a_model_seed_and_a_file_seed_take_no_field_as_a_member(corpus: Workspace) -> None:
    model = impact(corpus, "sym:api:api/models.py:Query:7")
    assert model.seed.members is None
    assert not [c for c in model.candidates if c.step.derived == "member"]
    whole = impact(corpus, "api/models.py")
    fields = {
        r[0]
        for r in corpus.database.execute(
            "SELECT key FROM entities WHERE json_extract(meta_json, '$.kind') = 'field'"
        )
    }
    members = {c.entity for c in whole.candidates if c.step.derived == "member"}
    assert members and not members & fields
    assert not {c.entity for c in whole.candidates} & fields
    assert whole.seed.members == 7  # Query, Out, Row, Plain, Plain.__init__, Child, Alias


def test_only_a_field_seed_climbs(corpus: Workspace) -> None:
    """A method is reached by what calls or uses it; its class is no step of its answer."""
    response = impact(corpus, "sym:api:api/models.py:Plain.__init__:29")
    assert not [c for c in response.candidates if c.step.derived == "member-of"]


def test_a_field_answers_to_its_qualified_name_not_its_bare_name(corpus: Workspace) -> None:
    service = CapabilityService(corpus)
    bare = service.impact(ImpactRequest(context=GLOBAL, seed="note")).seed
    assert bare.status == "unresolved"
    qualified = service.impact(ImpactRequest(context=GLOBAL, seed="Row.note")).seed
    assert (qualified.status, qualified.entity) == ("resolved", "sym:api:api/models.py:Row.note:23")


# -- incremental equals fresh ------------------------------------------------------------------

MODELS = "from pydantic import BaseModel\n\n\nclass M(BaseModel):\n    a: int\n"
USER = "from pkg.models import M\n\n\ndef f(m: M) -> int:\n    return m.a + m.b\n\n\ndef g() -> M:\n    return M(a=1, b=2)\n"


def edges_now(workspace: Workspace) -> set[tuple[str, str, int, str]]:
    return field_uses(workspace)


def test_every_change_to_a_model_or_its_user_equals_a_fresh_index(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    files = {
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "1.0"\n',
        "pkg/__init__.py": "",
        "pkg/models.py": MODELS,
        "pkg/user.py": USER,
    }
    build(workspace, tmp_path, {"pkg": files})
    repo = tmp_path / "repos" / "pkg"

    def step(tag: str) -> set[tuple[str, str, int, str]]:
        sync(workspace, workspace.resolver().resolve(repo))
        assert fact_snapshot(workspace) == fresh(settings, repo.parent, tmp_path, tag), tag
        return edges_now(workspace)

    assert edges_now(workspace) == {
        ("pkg/user.py#f", "pkg/models.py#M.a", 5, "parameter"),
        ("pkg/user.py#g", "pkg/models.py#M.a", 9, "construction"),
    }
    # a field the user already reads is added: the unchanged user gains its uses
    (repo / "pkg/models.py").write_text(MODELS + "    b: int\n")
    assert step("added") == {
        ("pkg/user.py#f", "pkg/models.py#M.a", 5, "parameter"),
        ("pkg/user.py#f", "pkg/models.py#M.b", 5, "parameter"),
        ("pkg/user.py#g", "pkg/models.py#M.a", 9, "construction"),
        ("pkg/user.py#g", "pkg/models.py#M.b", 9, "construction"),
    }
    # a field renamed: its uses go
    (repo / "pkg/models.py").write_text(MODELS.replace("a: int", "c: int") + "    b: int\n")
    assert step("renamed") == {
        ("pkg/user.py#f", "pkg/models.py#M.b", 5, "parameter"),
        ("pkg/user.py#g", "pkg/models.py#M.b", 9, "construction"),
    }
    # the initializer is no longer proven: the construction's uses go, the reads stay
    (repo / "pkg/models.py").write_text(
        MODELS.replace("a: int", "c: int").replace("(BaseModel)", "") + "    b: int\n"
    )
    assert step("unproven") == {("pkg/user.py#f", "pkg/models.py#M.b", 5, "parameter")}
    # the parameter retyped: nothing it reads is M's
    (repo / "pkg/user.py").write_text(USER.replace("m: M", "m: int"))
    assert step("retyped") == set()
    # the model's file moved away
    shutil.move(repo / "pkg/models.py", repo / "pkg/shapes.py")
    assert step("moved") == set()


def fresh(settings: Settings, root: Path, tmp_path: Path, tag: str) -> dict[str, Any]:
    from repoatlas.services.init_service import initialize

    other = open_workspace(
        settings=settings.model_copy(
            update={
                "storage": settings.storage.model_copy(
                    update={"database": tmp_path / tag / "atlas.db"}
                )
            }
        ),
        require_root=False,
    )
    try:
        initialize(other, root, config_path=tmp_path / f"{tag}-config.yaml")
        return fact_snapshot(other)
    finally:
        other.close()


def test_the_api_corpus_source_is_what_the_expectations_say() -> None:
    """The expectations above cite lines; a moved line is a broken fixture, not a result."""
    handlers = API["api/handlers.py"].splitlines()
    assert handlers[7].strip().startswith("return Out(observation_id=first")
    assert handlers[39].strip() == "return item.name"


# -- value mappings: a field is never the type a declaration names ---------------------------

PARCELS: dict[str, str] = {
    "pyproject.toml": '[project]\nname = "parcels"\nversion = "1.0"\n',
    "parcels/__init__.py": "",
    "parcels/stages.py": 'from enum import Enum\n\n\nclass Stage(str, Enum):\n    LOST = "LOST"\n',
    "parcels/models.py": (
        "from dataclasses import dataclass\n"
        "\n"
        "from parcels.stages import Stage\n"
        "\n"
        "\n"
        "@dataclass\n"
        "class Parcel:\n"
        "    state: Stage\n"
        "\n"
        "\n"
        "@dataclass\n"
        "class Holder:\n"
        "    Parcel: str\n"  # a field named like the type below
    ),
    "parcels/checks.py": (
        "from parcels.models import Holder, Parcel\n"  # 1
        "\n"  # 2
        "\n"  # 3
        "def lost(holder: Holder, parcel: Parcel) -> bool:\n"  # 4
        "    print(holder.Parcel)\n"  # 5  USES Holder.Parcel, a field named Parcel
        '    return parcel.state == "LOST"\n'  # 6  the comparison still names Parcel, the class
    ),
}


def test_a_field_named_like_a_type_leaves_the_type_s_comparison_alone(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, {"parcels": PARCELS})
    assert (
        "parcels/checks.py#lost",
        "parcels/models.py#Holder.Parcel",
        5,
        "parameter",
    ) in field_uses(workspace)
    rows = workspace.database.execute(
        """SELECT s.qualified_name, d.qualified_name FROM edges e
           JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id
           WHERE e.type = 'REFERENCES' AND e.resolution LIKE '%typed_comparison%'"""
    ).fetchall()
    assert [tuple(r) for r in rows] == [("lost", "Stage.LOST")]
