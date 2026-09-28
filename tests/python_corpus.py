"""Neutral fixtures for Python declaration-level uses (P1-P12, P18-P21).

Nothing here is taken from a real repository or a benchmark task; every file states the
binding or layout rule it exists to prove.

* ``pyfix`` is one src-layout project (distribution ``fixture-dist``, import package
  ``fixture_pkg``): relative and absolute imports, aliases, module aliases, attribute
  chains, a package ``__init__`` that re-exports (under a second name, and a submodule),
  shadowing, ``TYPE_CHECKING``, every use form, PEP 695 type parameters, star imports,
  proven calls, an unrelated same-named class, and a test calling a consumer.
* ``flatfix`` is a flat-layout project with a ``scripts/`` directory that is no root.
* ``mono`` is one Git repository holding two projects, ``alpha`` and ``beta``, each
  with its own top-level ``app``: a project is a boundary, whatever repository it is in.
"""

from __future__ import annotations

import json
from collections import Counter

from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.graph.uses_link import python_use_resolver
from repoatlas.models.parsing import USES_EXPORT_TABLE, EdgeType
from repoatlas.services.workspace import Workspace

PKG = "src/fixture_pkg"

PYFIX: dict[str, str] = {
    "pyproject.toml": (
        '[project]\nname = "fixture-dist"\nversion = "1.0"\n'
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
    ),
    f"{PKG}/__init__.py": (
        "from .models import User\nfrom .models import User as PublicUser\nfrom . import service\n"
    ),
    f"{PKG}/models.py": (
        "class User:\n    pass\n"  # 1
        "class Base:\n    pass\n"  # 3
        "class T:\n    pass\n"  # 5
        "class Bound:\n    pass\n"  # 7
        "def register(f):\n    return f\n"  # 9
        "def make() -> 'User':\n    return User()\n"  # 11
    ),
    f"{PKG}/service.py": "def run():\n    return 1\n",
    f"{PKG}/sub/__init__.py": "",
    # P1: relative imports, one of them a from-import of a submodule
    f"{PKG}/sub/rel.py": (
        "from ..models import User\n"
        "from .. import models\n"
        "def p1(u: User):\n    return u\n"
        "def p1b():\n    return models.make()\n"
    ),
    # P2 and P6
    f"{PKG}/abs_user.py": (
        "from fixture_pkg.models import User\n"
        "from src.fixture_pkg.models import Base\n"
        "def p2(u: User, b: Base):\n    return u\n"
    ),
    # P3
    f"{PKG}/alias_user.py": (
        "from fixture_pkg.models import User as LocalUser\ndef p3(u: LocalUser):\n    return u\n"
    ),
    # P4: a module alias, a chain the package binds, and a chain nothing proves
    f"{PKG}/mod_alias.py": (
        "import fixture_pkg.service as svc\n"
        "import fixture_pkg\n"
        "def p4():\n    return svc.run()\n"
        "def p4_bound():\n    return fixture_pkg.service.run()\n"
        "def p4_unproven():\n    return fixture_pkg.sub.rel.p1\n"
    ),
    # P5: the package re-exports, under its own name and another
    f"{PKG}/reexport_user.py": (
        "from fixture_pkg import User, PublicUser\ndef p5(a: User, b: PublicUser):\n    return a\n"
    ),
    # P8: shadowed every way but one
    f"{PKG}/shadow.py": (
        "from fixture_pkg.models import User\n"
        "def by_param(User):\n    return User\n"
        "def by_local():\n    User = 1\n    return User\n"
        "def by_loop():\n    return [User for User in range(2)]\n"
        "class Holder:\n"
        "    User = None\n"
        "    ref = User\n"
        "    def method(self):\n        return User\n"
    ),
    # P9
    f"{PKG}/checking.py": (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from fixture_pkg.models import User\n"
        'def p9(u: "User") -> None:\n    return None\n'
    ),
    # P10
    f"{PKG}/forms.py": (
        "from typing import Annotated\n"
        "from fixture_pkg.models import Base, User, make, register\n"
        "@register\ndef decorated(x: list[User]) -> User | None:\n    return None\n"
        "class Child(Base):\n    field: Annotated[User, 'meta']\n"
        "def check(x):\n    return isinstance(x, User)\n"
        "def build():\n    return make()\n"
    ),
    # P10-TP
    f"{PKG}/generic.py": (
        "from fixture_pkg.models import T, Base, Bound\n"
        "def f[T](x: T) -> T:\n    return x\n"
        "class C[T](Base):\n    def m(self, x: T) -> T:\n        return x\n"
        "def g[T: Bound](x: T) -> T:\n    return x\n"
        "type Alias[T] = list[T]\n"
        "type Other = T\n"
    ),
    # P11
    f"{PKG}/starpkg/__init__.py": "from .a import *\nfrom .b import *\nfrom .things import Thing\n",
    f"{PKG}/starpkg/a.py": "class User:\n    pass\n",
    f"{PKG}/starpkg/b.py": "class User:\n    pass\n",
    f"{PKG}/starpkg/things.py": "class Thing:\n    pass\n",
    f"{PKG}/star_user.py": (
        "from fixture_pkg.starpkg import User, Thing\ndef p11d(u: User, t: Thing):\n    return t\n"
    ),
    f"{PKG}/star_after.py": (
        "from fixture_pkg.starpkg.a import *\n"
        "from fixture_pkg.models import User\n"
        "def p11a(u: User):\n    return u\n"
    ),
    f"{PKG}/star_before.py": (
        "from fixture_pkg.models import User\n"
        "from fixture_pkg.starpkg.a import *\n"
        "def p11b(u: User):\n    return u\n"
    ),
    # P21
    f"{PKG}/calls.py": (
        "from fixture_pkg.service import run\n"
        "class Job:\n    def run(self):\n        return self.run()\n"
        "def helper():\n    return 1\n"
        "def main():\n    run()\n    return helper()\n"
        "def loop(n):\n    return loop(n - 1)\n"
    ),
    # P19: a same-named class nothing imports
    f"{PKG}/unrelated.py": "class User:\n    pass\n",
    # P18: a test of a consumer
    "tests/test_consumer.py": (
        "from fixture_pkg.abs_user import p2\ndef test_p2():\n    p2(None, None)\n"
    ),
}

FLATFIX: dict[str, str] = {
    "pyproject.toml": '[project]\nname = "flatfix"\n',
    "fixture_pkg/__init__.py": "",
    "fixture_pkg/models.py": "class User:\n    pass\n",
    "scripts/bar.py": "def bar():\n    return 1\n",
    "app.py": (
        "from fixture_pkg.models import User\nimport bar\ndef p7(u: User):\n    return bar.bar()\n"
    ),
}

MONO: dict[str, str] = {
    "alpha/pyproject.toml": '[project]\nname = "alpha"\n',
    "alpha/src/alpha/__init__.py": "",
    "alpha/src/alpha/models.py": "class Thing:\n    pass\n",
    "alpha/src/alpha/local.py": "from app import App\ndef mine(a: App):\n    return a\n",
    "alpha/src/app/__init__.py": "class App:\n    pass\n",
    "beta/pyproject.toml": '[project]\nname = "beta"\ndependencies = ["alpha"]\n',
    "beta/src/beta/__init__.py": "",
    "beta/src/beta/service.py": (
        "from alpha.models import Thing\nfrom app import App\n"
        "def use(t: Thing, a: App):\n    return a\n"
    ),
    "beta/src/app/__init__.py": "class App:\n    pass\n",
}


def refusals(workspace: Workspace, repository: str, path: str) -> dict[str, str]:
    """Why each Python use one file makes resolved to nothing, by its target key."""
    knowledge = workspace.knowledge
    index = ResolutionIndex.build(knowledge.list_file_rows(), knowledge.list_symbols())
    resolver = python_use_resolver(knowledge, index)
    [repo_id] = [i for i, slug in index.repo_slugs.items() if slug == repository]
    origin = index.file(repo_id, path)
    assert origin is not None
    found: dict[str, str] = {}
    for claim in knowledge.list_use_claims([origin.file_id]):
        if claim.edge_type is EdgeType.IMPORTS or claim.target_key == USES_EXPORT_TABLE:
            continue
        meta = json.loads(claim.meta_json or "{}")
        before = Counter(resolver.unresolved)
        if resolver.resolve(origin, meta) is None:
            [why] = (resolver.unresolved - before).elements()
            found[claim.target_key] = why.removeprefix("python:")
    return found
