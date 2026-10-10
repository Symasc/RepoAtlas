# ruff: noqa: E501 - the expected keys and configurations are spelled whole, as stored
"""DF3a's parser half (DF3A_PHASE_A.md §2, §12.4): which declaration an environment read
belongs to (python-ast 17, typescript 19), and what a compose entry or a workload's env entry
records of its value (structured-config 13) -- never the value.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

import pytest
import yaml
from repoatlas.models.parsing import EdgeType
from repoatlas.parsers.deploy_compose import compose_references
from repoatlas.parsers.deploy_helm import workload_references
from repoatlas.parsers.python import PythonParser
from repoatlas.parsers.typescript import TypeScriptParser
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build

# -- Python ---------------------------------------------------------------------------------

PYTHON = """\
import os

LIMIT = int(os.getenv("LIMIT", "1"))
TYPED: str = os.environ.get("TYPED", "")
FIRST = SECOND = os.getenv("CHAINED")
LEFT, RIGHT = os.getenv("TUPLE"), 1
print(os.getenv("CALLED"))
if os.getenv("GUARD"):
    GUARDED = os.environ["NESTED"]
try:
    TRIED = os.environ["TRIED"]
except KeyError:
    pass
LAZY = lambda: os.getenv("LAZY")  # noqa: E731


def read() -> str:
    return os.environ["IN_FUNCTION"]


class Config:
    BODY = os.getenv("IN_CLASS")
"""


def python_reads(source: str) -> dict[str, tuple[str | None, str | None]]:
    parsed = PythonParser().parse(Path("settings.py"), source)
    return {
        r.target_key: (r.source_symbol, r.meta.get("owner"))
        for r in parsed.references
        if r.edge_type is EdgeType.READS
    }


def test_a_python_read_belongs_to_the_one_value_its_statement_declares() -> None:
    assert python_reads(PYTHON) == {
        "LIMIT": ("LIMIT", "value"),
        "TYPED": ("TYPED", "value"),
        # two names bound by one statement: neither owns it
        "CHAINED": (None, None),
        "TUPLE": (None, None),
        # a call at module level, an if test: the file's
        "CALLED": (None, None),
        "GUARD": (None, None),
        # a declaring statement nested in a module-level if or try is a module value
        "NESTED": ("GUARDED", "value"),
        "TRIED": ("TRIED", "value"),
        # a lambda belongs to the statement holding it, as its uses do
        "LAZY": ("LAZY", "value"),
        # inside a function or a class body: theirs, as before
        "IN_FUNCTION": ("read", None),
        "IN_CLASS": ("Config", None),
    }


# -- TypeScript and JavaScript ----------------------------------------------------------------

TYPESCRIPT = """\
export const apiUrl = process.env.API_URL;
const { DESTRUCTURED } = process.env;
const fromObject = { region: process.env["REGION"] };
export const store = create(() => ({ mode: process.env.MODE }));
const handler = () => process.env.IN_ARROW;
function read() {
  return process.env.IN_FUNCTION;
}
describe("settings", () => {
  const nested = process.env.IN_CALLBACK;
});
console.log(process.env.CALLED);
"""


def typescript_reads(path: str, source: str) -> dict[str, str | None]:
    parsed = TypeScriptParser().parse(Path(path), source)
    return {
        r.target_key: r.source_symbol for r in parsed.references if r.edge_type is EdgeType.READS
    }


@pytest.mark.parametrize("path", ["config.ts", "config.js"])
def test_a_typescript_read_belongs_to_its_module_binding_as_a_use_there_does(path: str) -> None:
    assert typescript_reads(path, TYPESCRIPT) == {
        "API_URL": "apiUrl",
        "REGION": "fromObject",
        # an anonymous function in a module binding's initializer is the binding's
        "MODE": "store",
        # a function value or declaration: the function, as before
        "IN_ARROW": "handler",
        "IN_FUNCTION": "read",
        # a const nested in a top-level callback is no module binding; a call is the file's
        "IN_CALLBACK": None,
        "CALLED": None,
    }


def test_each_read_is_linked_from_its_declaration_and_never_a_same_named_one(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(
        workspace,
        tmp_path,
        {
            "svc": {
                "settings.py": 'import os\n\nLIMIT = os.getenv("LIMIT")\n',
                "other.py": "def LIMIT():\n    return 1\n",
                "config.ts": "export const url = process.env.URL;\n",
            }
        },
    )
    rows = workspace.database.execute(
        """
        SELECT s.key, d.key FROM edges e JOIN entities s ON s.id = e.src_id
        JOIN entities d ON d.id = e.dst_id WHERE e.type = 'READS'
        """
    ).fetchall()
    assert sorted(tuple(r) for r in rows) == [
        ("sym:svc:config.ts:url:1", "env:URL"),
        ("sym:svc:settings.py:LIMIT:3", "env:LIMIT"),
    ]


# -- compose entries (structured-config 13) ------------------------------------------------------


def entries(text: str) -> list[list[object]]:
    [service] = compose_references(
        PurePosixPath("docker-compose.yml"), list(yaml.compose_all(text, Loader=yaml.SafeLoader))
    )
    return json.loads(service.meta["environment"])


def test_a_compose_entry_records_whether_and_how_it_has_a_value_never_the_value() -> None:
    mapping = (
        "services:\n  web:\n    environment:\n"
        '      LITERAL: "8000"\n      EMPTY: ""\n      NULLED:\n      TILDE: ~\n      NAMED_NULL: null\n'
        "      NUMBER: 3\n      ESCAPED: $$HOME\n      BRACED: ${HOST:-x}\n      BARE: x$Y\n"
        "      LISTED: [1]\n"
    )
    assert entries(mapping) == [
        ["LITERAL", 4, "literal"],
        ["EMPTY", 5, "literal"],
        ["NULLED", 6, "passthrough"],
        ["TILDE", 7, "passthrough"],
        ["NAMED_NULL", 8, "passthrough"],
        ["NUMBER", 9, "literal"],
        ["ESCAPED", 10, "literal"],
        ["BRACED", 11, "symbolic"],
        ["BARE", 12, "symbolic"],
        ["LISTED", 13, "unread"],
    ]
    listed = (
        "services:\n  web:\n    environment:\n"
        "      - LITERAL=8000\n      - EMPTY=\n      - PASSED\n      - SYMBOLIC=${X}\n      - ESCAPED=$$X\n"
    )
    assert entries(listed) == [
        ["LITERAL", 4, "literal"],
        ["EMPTY", 5, "literal"],
        ["PASSED", 6, "passthrough"],
        ["SYMBOLIC", 7, "symbolic"],
        ["ESCAPED", 8, "literal"],
    ]
    assert "8000" not in json.dumps(entries(mapping) + entries(listed))


# -- a workload's env entries ------------------------------------------------------------------

WORKLOAD = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: app
spec:
  template:
    spec:
      containers:
        - name: app
          image: "registry.example/app:1"
          env:
            - name: FROM_MAP
              valueFrom:
                configMapKeyRef:
                  name: settings
                  key: FROM_MAP
            - name: FROM_SECRET
              valueFrom:
                secretKeyRef:
                  name: credentials
                  key: token
            - name: DEFAULTED
              value: {{ .Values.level | default "info" | quote }}
          envFrom:
            - configMapRef:
                name: settings
            - secretRef:
                name: credentials
"""


def test_a_workload_reads_block_references_defaults_and_source_lines() -> None:
    [workload] = workload_references(
        PurePosixPath("charts/app/templates/deployment.yaml"), WORKLOAD
    )
    [container] = json.loads(workload.meta["containers"])
    assert container["env"] == [
        # the block form, read as a reference (DF1 read it as unread)
        {
            "line": 12,
            "name": "FROM_MAP",
            "source": "configMapKeyRef",
            "ref_name": "settings",
            "ref_key": "FROM_MAP",
        },
        {
            "line": 17,
            "name": "FROM_SECRET",
            "source": "secretKeyRef",
            "ref_name": "credentials",
            "ref_key": "token",
        },
        {"line": 22, "name": "DEFAULTED", "source": "value", "values": "level", "default": "true"},
    ]
    assert container["env_from"] == [
        {"line": 25, "name": "settings", "source": "configMapRef"},
        {"line": 27, "name": "credentials", "source": "secretRef"},
    ]
