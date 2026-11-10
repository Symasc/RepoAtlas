"""A synthetic corpus for the ordinary-claim account (ORDINARY_REFERENCE_RESOLUTION.md).

Every name here is invented. Each repository is small and was written to put one family of
ordinary claim in front of the linker with a known answer: a claim that links, one that
reaches a package or the standard library, one the ordinary rules cannot establish, and the
lookalikes that must never be called external. Nothing in it is copied from a real source.
"""

from __future__ import annotations

import json

#: A Python application. ``requests`` is declared; ``undeclared_lib`` is not.
TOY_PY = {
    "requirements.txt": "requests==2.0\n",
    "toy_app/__init__.py": "from .codec import parse_record as reexported\n",
    "toy_app/codec.py": (
        "def parse_record(payload):\n"
        "    return payload\n"
        "\n"
        "\n"
        "class Store:\n"
        "    def get(self):\n"
        "        return 1\n"
    ),
    "toy_app/values.py": (
        "from .workers import OtherWorker, PropertyWorker, Worker\n"
        "worker: Worker\n"
        "untyped_worker = {}\n"
        "ambiguous_worker: Worker | OtherWorker\n"
        "property_worker: PropertyWorker\n"
    ),
    "toy_app/dup.py": (
        "class A:\n"
        "    def run(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        "class B:\n"
        "    def run(self):\n"
        "        return 2\n"
    ),
    "toy_app/audit.py": "def audit_action(fn):\n    return fn\n",
    "toy_app/workers.py": (
        "class BaseWorker:\n"
        "    pass\n"
        "\n"
        "\n"
        "class Worker:\n"
        "    def run(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        "class OtherWorker:\n"
        "    def run(self):\n"
        "        return 2\n"
        "\n"
        "\n"
        "class PropertyWorker:\n"
        "    @property\n"
        "    def run(self):\n"
        "        return None\n"
    ),
    "toy_app/main.py": (
        "import json\n"
        "import requests\n"
        "import undeclared_lib\n"
        "import toy_app.codec as codec_mod\n"
        "import toy_app.dup as dup\n"
        "from toy_app.codec import parse_record\n"
        "from toy_app import reexported\n"
        "from toy_app.audit import audit_action as tracked\n"
        "from toy_app.workers import BaseWorker\n"
        "from . import codec\n"
        "from . import notamodule\n"
        "from .missing import nothing\n"
        "from ..climb import up\n"
        "\n"
        "\n"
        "@tracked\n"
        "class RecordWorker(BaseWorker):\n"
        "    def run(self, payload):\n"
        "        parse_record(payload)\n"
        "        json.loads('{}')\n"
        "        requests.get('https://example.invalid')\n"
        "        undeclared_lib.go()\n"
        "        codec_mod.parse_record(payload)\n"
        "        codec_mod.store.get()\n"
        "        dup.run()\n"
        "        reexported(payload)\n"
        "        from toy_app import reexported as public_parse\n"
        "        public_parse(payload)\n"
        "        handlers = {}\n"
        "        handler = handlers[payload]\n"
        "        handler(payload)\n"
        "\n"
        "\n"
        "from toy_app.values import (\n"
        "    ambiguous_worker,\n"
        "    property_worker as descriptor_worker,\n"
        "    untyped_worker as dynamic_worker,\n"
        "    worker as typed_worker,\n"
        ")\n"
        "\n"
        "\n"
        "def typed_value_use():\n"
        "    typed_worker.run()\n"
        "    dynamic_worker.run()\n"
        "    ambiguous_worker.run()\n"
        "    descriptor_worker.run()\n"
        "\n"
        "\n"
        "def shadowed_value(typed_worker):\n"
        "    typed_worker.run()\n"
        "\n"
        "\n"
        "from toy_app.values import worker as rebound_worker\n"
        "rebound_worker = {}\n"
        "\n"
        "\n"
        "def rebound_value():\n"
        "    rebound_worker.run()\n"
    ),
}

#: A TypeScript application with a tsconfig alias, a workspace package of its own, a package
#: another repository provides, and a package it declares and nobody provides.
TOY_WEB = {
    "package.json": json.dumps(
        {
            "name": "toy-web",
            "dependencies": {"react": "1.0.0", "toy-shared": "1.0.0"},
            "workspaces": ["packages/*"],
        }
    ),
    "tsconfig.json": json.dumps({"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}),
    "packages/codec/package.json": json.dumps({"name": "@toy/codec", "main": "index.ts"}),
    "packages/codec/index.ts": "export function parse() {\n  return 1;\n}\n",
    "src/local.ts": (
        "export function a() {\n  return 1;\n}\nexport function b() {\n  return 2;\n}\n"
    ),
    "src/lib/x.ts": "export const x = 1;\n",
    "src/index.ts": (
        'import React from "react";\n'
        'import fs from "node:fs";\n'
        'import { readFileSync } from "fs";\n'
        'import { x } from "@/lib/x";\n'
        'import { missing } from "./missing";\n'
        'import { far } from "../../../../outside";\n'
        'import stray from "undeclared-pkg";\n'
        'import { parse } from "@toy/codec";\n'
        'import { shared } from "toy-shared";\n'
        'import { a } from "./local";\n'
        'import { b as renamed } from "./local";\n'
        'import * as ns from "./local";\n'
        "\n"
        "export function run() {\n"
        "  a();\n"
        "  renamed();\n"
        "  ns.nothing();\n"
        "  React.createElement('div');\n"
        "  fs.readFileSync('x');\n"
        "  readFileSync('y');\n"
        "  parse();\n"
        "  shared();\n"
        "  stray();\n"
        "  x;\n"
        "  missing();\n"
        "  far();\n"
        "  const callbacks: Record<string, () => void> = {};\n"
        "  callbacks['k']();\n"
        "}\n"
    ),
}

#: A repository that provides ``toy-shared`` to the one above.
TOY_SHARED = {
    "package.json": json.dumps({"name": "toy-shared", "main": "index.ts"}),
    "index.ts": "export function shared() {\n  return 1;\n}\n",
}

#: Shell scripts: a literal path that links, one that does not exist, one that leaves every
#: managed root, and a variable-selected command that is not a claim at all.
TOY_SCRIPTS = {
    "scripts/common.sh": "#!/bin/sh\necho common\n",
    "scripts/check.sh": "#!/bin/sh\necho check\n",
    "scripts/run.sh": (
        "#!/bin/sh\n"
        ". ./scripts/common.sh\n"
        "./scripts/check.sh\n"
        "./scripts/absent.sh\n"
        "../../shared/outside.sh\n"
        '"$TOOL_SCRIPT"\n'
    ),
}


def repos() -> dict[str, dict[str, str]]:
    return {
        "toy-py": TOY_PY,
        "toy-web": TOY_WEB,
        "toy-shared": TOY_SHARED,
        "toy-scripts": TOY_SCRIPTS,
    }
