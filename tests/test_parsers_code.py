"""TypeScript/JavaScript, Bash and Ruby parsers (V1_SPEC §7.2-§7.4, §19).

Parser tests are pure: parse text, assert on the result. No database, no sync.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.models.parsing import EdgeType, EntityType, ParsedFile, Severity
from repoatlas.parsers.base import LanguageParser
from repoatlas.parsers.bash import BashParser
from repoatlas.parsers.ruby import RubyParser
from repoatlas.parsers.typescript import TypeScriptParser


def symbols(parsed: ParsedFile, kind: EntityType | None = None) -> list[str]:
    return [s.name for s in parsed.symbols if kind is None or s.kind is kind]


def references(parsed: ParsedFile, edge: EdgeType) -> list[str]:
    return [r.target_key for r in parsed.references if r.edge_type is edge]


def reference_at(parsed: ParsedFile, edge: EdgeType, target: str) -> int:
    return next(
        r.line_start for r in parsed.references if r.edge_type is edge and r.target_key == target
    )


# --- TypeScript / JavaScript -------------------------------------------------

TS_SOURCE = """\
import { getConfig, Other } from "@company/common";
import fs from "node:fs";
import { local } from "./settings";

export interface Options {
  retries: number;
}

export type Alias = string;

export class Server extends Base implements Options {
  listen(port: number): number {
    return port;
  }
}

export function start(): string {
  const host = process.env.SERVICE_HOST ?? "localhost";
  const port = process.env["SERVICE_PORT"];
  return `${getConfig()}:${host}:${port}`;
}

export const handler = async () => {
  const late = await import("./late");
  return require("./legacy");
};
"""


def parse_ts(source: str = TS_SOURCE, name: str = "index.ts") -> ParsedFile:
    return TypeScriptParser().parse(Path(name), source)


def test_typescript_parser_satisfies_the_protocol() -> None:
    assert isinstance(TypeScriptParser(), LanguageParser)


def test_typescript_extracts_definitions() -> None:
    parsed = parse_ts()
    assert "Server" in symbols(parsed, EntityType.CLASS)
    assert "listen" in symbols(parsed, EntityType.METHOD)
    assert {"start", "handler"} <= set(symbols(parsed, EntityType.FUNCTION))
    kinds = {s.name: s.meta.get("kind") for s in parsed.symbols}
    assert kinds["Options"] == "interface"
    assert kinds["Alias"] == "type"


def test_typescript_distinguishes_package_from_relative_imports() -> None:
    parsed = parse_ts()
    specifiers = {
        r.target_key: r.meta["specifier"]
        for r in parsed.references
        if r.edge_type is EdgeType.IMPORTS
    }
    assert specifiers["@company/common"] == "package"
    assert specifiers["node:fs"] == "package"
    assert specifiers["./settings"] == "relative"


def test_typescript_records_dynamic_import_and_require() -> None:
    parsed = parse_ts()
    imports = references(parsed, EdgeType.IMPORTS)
    assert "./late" in imports
    assert "./legacy" in imports


def test_typescript_marks_exports() -> None:
    parsed = parse_ts()
    assert {"Server", "start", "handler", "Options", "Alias"} <= set(
        references(parsed, EdgeType.EXPORTS)
    )


def test_typescript_records_heritage() -> None:
    parsed = parse_ts()
    bases = {r.target_key for r in parsed.references if r.meta.get("role") == "base"}
    assert {"Base", "Options"} <= bases


def test_typescript_reads_process_env_in_both_forms() -> None:
    parsed = parse_ts()
    assert set(references(parsed, EdgeType.READS)) == {"SERVICE_HOST", "SERVICE_PORT"}
    assert reference_at(parsed, EdgeType.READS, "SERVICE_HOST") == 18


def test_typescript_ignores_a_computed_env_key() -> None:
    """A key that is not a literal cannot be resolved without executing the code."""
    parsed = parse_ts("const v = process.env[keyName];\n", "a.ts")
    assert references(parsed, EdgeType.READS) == []


def test_javascript_and_jsx_use_the_javascript_grammar() -> None:
    parsed = TypeScriptParser().parse(
        Path("app.jsx"), "export const App = () => <div>{process.env.API_URL}</div>;"
    )
    assert parsed.language == "JavaScript"
    assert references(parsed, EdgeType.READS) == ["API_URL"]


def test_typescript_syntax_error_yields_a_diagnostic_and_partial_results() -> None:
    parsed = parse_ts("export function ok() {}\nfunction (((\n", "broken.ts")
    assert "ok" in symbols(parsed)
    assert parsed.diagnostics
    assert parsed.diagnostics[0].severity is Severity.WARNING


# --- Bash --------------------------------------------------------------------

BASH_SOURCE = """\
#!/usr/bin/env bash
set -euo pipefail

source ./env.sh
. ../shared/common.sh

LOCAL_TMP=/tmp/build
export SERVICE_HOST="${SERVICE_HOST:-localhost}"
export DEPLOY_TARGET

deploy() {
  python3 ../python-service/src/app.py --verbose
  ruby tools/task.rb
  echo "${LOCAL_TMP}/${SERVICE_PORT}"
}

./scripts/preflight.sh
salt-call state.apply service
"""


def parse_bash(source: str = BASH_SOURCE, name: str = "deploy.sh") -> ParsedFile:
    return BashParser().parse(Path(name), source)


def test_bash_parser_satisfies_the_protocol() -> None:
    assert isinstance(BashParser(), LanguageParser)


def test_bash_extracts_functions_and_the_script_itself() -> None:
    parsed = parse_bash()
    assert symbols(parsed, EntityType.SCRIPT) == ["deploy.sh"]
    assert symbols(parsed, EntityType.FUNCTION) == ["deploy"]


def test_bash_records_sourced_scripts() -> None:
    parsed = parse_bash()
    assert set(references(parsed, EdgeType.SOURCES)) == {"./env.sh", "../shared/common.sh"}


def test_bash_records_executed_commands_and_scripts() -> None:
    parsed = parse_bash()
    executed = {
        r.target_key: r.target_kind for r in parsed.references if r.edge_type is EdgeType.EXECUTES
    }
    assert executed["set"] is EntityType.COMMAND
    assert executed["../python-service/src/app.py"] is EntityType.SCRIPT
    assert executed["tools/task.rb"] is EntityType.SCRIPT
    assert executed["./scripts/preflight.sh"] is EntityType.SCRIPT


def test_bash_records_exported_variables() -> None:
    parsed = parse_bash()
    assert set(references(parsed, EdgeType.EXPORTS)) == {"SERVICE_HOST", "DEPLOY_TARGET"}


def test_bash_reads_environment_but_not_its_own_locals() -> None:
    """``LOCAL_TMP`` is assigned and never exported, so reading it back proves nothing."""
    parsed = parse_bash()
    reads = set(references(parsed, EdgeType.READS))
    assert "SERVICE_PORT" in reads
    assert "SERVICE_HOST" in reads  # exported from its own environment default
    assert "LOCAL_TMP" not in reads


def test_bash_records_salt_state_invocation() -> None:
    parsed = parse_bash()
    salt = [r for r in parsed.references if r.target_kind is EntityType.SALT_STATE]
    assert [r.target_key for r in salt] == ["service"]
    assert salt[0].meta["via"] == "state.apply"


def test_bash_does_not_guess_a_computed_source_path() -> None:
    parsed = parse_bash('source "$CONFIG_FILE"\n', "x.sh")
    assert references(parsed, EdgeType.SOURCES) == []
    assert "CONFIG_FILE" in references(parsed, EdgeType.READS)


# --- Ruby --------------------------------------------------------------------
#
# Structural Ruby parsing is gated (OFF-01; tests/test_ruby_gate.py). The tests
# below characterize the extraction the gate keeps unreachable, and they are kept
# rather than deleted because re-enabling Ruby starts by re-running them against
# a grammar that contains upstream fix ad907a69. No such grammar is installed, so
# they cannot run, and a skip that says why is more honest than a silent absence.

ruby_structural = pytest.mark.skip(
    reason="OFF-01: Ruby structural parsing is gated until an offline-installable "
    "tree-sitter-ruby containing ad907a69 is adopted"
)

RUBY_SOURCE = """\
require "json"
require_relative "logging"

module Deploy
  class Runner < Base
    def run(target)
      host = ENV["SERVICE_HOST"]
      timeout = ENV.fetch("SERVICE_TIMEOUT", "30")
      notify(host, timeout)
    end

    def self.build
      new
    end
  end
end
"""


def parse_ruby(source: str = RUBY_SOURCE, name: str = "runner.rb") -> ParsedFile:
    return RubyParser()._structure(Path(name), source)


def test_ruby_parser_satisfies_the_protocol() -> None:
    assert isinstance(RubyParser(), LanguageParser)


@ruby_structural
def test_ruby_extracts_modules_classes_and_methods() -> None:
    parsed = parse_ruby()
    qualified = {s.qualified_name for s in parsed.symbols}
    assert "Deploy" in qualified
    assert "Deploy::Runner" in qualified
    assert "Deploy::Runner::run" in qualified
    assert "Deploy::Runner::self.build" in qualified


@ruby_structural
def test_ruby_distinguishes_require_from_require_relative() -> None:
    parsed = parse_ruby()
    specifiers = {
        r.target_key: r.meta["specifier"]
        for r in parsed.references
        if r.edge_type is EdgeType.IMPORTS
    }
    assert specifiers == {"json": "package", "logging": "relative"}


@ruby_structural
def test_ruby_reads_environment_in_both_forms() -> None:
    parsed = parse_ruby()
    assert set(references(parsed, EdgeType.READS)) == {"SERVICE_HOST", "SERVICE_TIMEOUT"}


@ruby_structural
def test_ruby_records_superclass_and_calls() -> None:
    parsed = parse_ruby()
    assert "Base" in [r.target_key for r in parsed.references if r.meta.get("role") == "base"]
    assert "notify" in references(parsed, EdgeType.CALLS)


@ruby_structural
def test_ruby_records_gem_declarations() -> None:
    parsed = RubyParser()._structure(Path("Gemfile"), 'gem "rails", "~> 7.0"\n')
    dependency = next(r for r in parsed.references if r.edge_type is EdgeType.DEPENDS_ON)
    assert dependency.target_key == "rails"
    assert dependency.meta == {"ecosystem": "gem", "constraint": "~> 7.0"}
