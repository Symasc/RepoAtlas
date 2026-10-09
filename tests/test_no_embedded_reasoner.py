"""RepoAtlas contains no embedded reasoning model, and works without one.

The product decision this suite pins: RepoAtlas owns deterministic repository
intelligence --- indexing, retrieval, graph and dependency facts, evidence
provenance, freshness, grounding verification and sync --- and an external agent
owns reasoning. There is no local model execution path, no provider
configuration, and nothing to install or reach before the product works.

Two halves, and the second is the one that matters.

**The reasoner is gone.** The package does not exist, the commands that drove it
are not registered, the flag that bypassed it is not accepted, and no
configuration names a provider or an endpoint. These are cheap assertions and
they are all negative, which is exactly why they are not enough on their own: a
suite of absences passes just as happily against a product that no longer works.

**The substrate still works.** Every capability, the transport, the CLI and the
evaluations run here against a real indexed workspace, with no model server, no
environment variable and --- in the offline case --- no ability to open a socket
at all. That is the claim worth making, and removing a subsystem is only correct
if it still holds afterwards.

The embedding model went later, for a different reason: once identity-based
stages answered, vector similarity added a download and no evidence. CASE 18
asserts it is gone too, so no model of any kind sits in the retrieval path.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import RetrievalSettings, Settings
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.capability_io import RepositoriesRequest, StatusRequest
from repoatlas.models.dependency_inventory import DependenciesRequest
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.relationships import RelationshipsRequest
from repoatlas.models.search import SearchRequest
from repoatlas.models.sync import SyncRequest
from repoatlas.models.verification import (
    AgentAnswer,
    AgentCitation,
    AgentClaim,
    VerifyGroundingRequest,
)
from repoatlas.services.workspace import Workspace
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parent.parent
PRODUCTION = ROOT / "repoatlas"
GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SERVICE = "python-service"

runner = CliRunner()


@pytest.fixture
def service(initialized: Workspace) -> Iterator[CapabilityService]:
    svc = CapabilityService(initialized)
    yield svc
    svc.close()


def _in_a_fresh_interpreter(source: str) -> subprocess.CompletedProcess[str]:
    """Run ``source`` where RepoAtlas has not already been imported.

    pytest has most of the package resident by the time this runs, so an
    in-process check would be asking a different question than the one a user's
    first import asks.
    """
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )


# ---------------------------------------------------------------------------
# CASES 1-2. The package is gone
# ---------------------------------------------------------------------------


def test_case_1_the_llm_package_does_not_exist_on_disk() -> None:
    """CASE 1. Not emptied, not stubbed, not left as a namespace package."""
    assert not (PRODUCTION / "llm").exists()
    assert list(PRODUCTION.rglob("llm")) == []


def test_case_1_no_prompt_template_survives_anywhere() -> None:
    """The reasoner's prompts were Markdown, which a package check would miss."""
    for name in ("ask.md", "debug.md", "repair.md", "system.md"):
        assert list(PRODUCTION.rglob(name)) == [], name


def test_case_2_importing_the_llm_package_fails() -> None:
    """CASE 2. The expected outcome, asserted rather than assumed.

    A shim that kept the import working would be the specific failure this looks
    for: it would say the subsystem is still supported when it is not.
    """
    result = _in_a_fresh_interpreter("import repoatlas.llm")
    assert result.returncode != 0
    assert "ModuleNotFoundError" in result.stderr
    assert "repoatlas.llm" in result.stderr


@pytest.mark.parametrize(
    "module",
    [
        "repoatlas.llm",
        "repoatlas.llm.grounding",
        "repoatlas.llm.protocol",
        "repoatlas.llm.null",
        "repoatlas.llm.reasoner",
        "repoatlas.llm.factory",
        "repoatlas.llm.ollama",
        "repoatlas.llm.scripted",
        "repoatlas.llm.prompts.loader",
        "repoatlas.services.query_service",
    ],
)
def test_case_2_no_module_is_left_behind_as_a_compatibility_shim(module: str) -> None:
    """CASE 2, spelled out. A deprecated shim is dead architecture that advertises
    support for a feature that was deliberately removed before release."""
    result = _in_a_fresh_interpreter(f"import {module}")
    assert result.returncode != 0, f"{module} is still importable"


def test_nothing_in_the_product_imports_the_deleted_package() -> None:
    """The static half of the rule, read from the syntax rather than from a run."""
    offenders: list[str] = []
    for path in PRODUCTION.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [
                    f"{path}: {a.name}" for a in node.names if a.name.startswith("repoatlas.llm")
                ]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "repoatlas.llm"
            ):
                offenders.append(f"{path}: {node.module}")
    assert offenders == []


def test_no_deterministic_entry_point_loads_anything_named_llm() -> None:
    """The transitive half, which the static half cannot see.

    RCA-02 established this invariant while the package still existed, by
    importing the capability layer in a clean interpreter and reading
    ``sys.modules``. Deletion turns it from "V2 does not import the legacy stack"
    into "there is no legacy stack", and the probe is kept at full strength: the
    CLI joins the list it was previously excluded from, because it is now a
    deterministic entry point like the others.
    """
    result = _in_a_fresh_interpreter("""
        import sys
        import repoatlas.capabilities.service
        import repoatlas.cli.main
        import repoatlas.mcp.server
        import repoatlas.services
        print([name for name in sys.modules if "llm" in name.lower()])
    """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


# ---------------------------------------------------------------------------
# CASES 3-4, 8. The command line
# ---------------------------------------------------------------------------

#: What `repoatlas` is, after the removal. Pinned as a set rather than described,
#: so that adding an undocumented entry point is a test failure (§22).
EXPECTED_COMMANDS = {"init", "sync", "status", "repos", "decisions", "locate", "mcp", "serve", "ui"}


def _registered_commands() -> set[str]:
    from repoatlas.cli.main import app

    names: set[str] = set()
    for command in app.registered_commands:
        callback = command.callback
        names.add(command.name or (callback.__name__ if callback is not None else "<anonymous>"))
    return names


def test_case_3_the_command_inventory_is_exactly_the_deterministic_set() -> None:
    """CASE 3, and §22's regression pin.

    Read from Typer's registry rather than from help text: a command registered
    but hidden would not appear in ``--help`` and would still be reachable.
    """
    assert _registered_commands() == EXPECTED_COMMANDS


def test_case_3_the_reasoning_commands_are_absent_from_help() -> None:
    """CASE 3. What a user is actually shown."""
    from repoatlas.cli.main import app

    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for gone in ("ask", "debug"):
        assert f" {gone} " not in result.output, gone
    for kept in sorted(EXPECTED_COMMANDS):
        assert kept in result.output, kept


@pytest.mark.parametrize("command", ["ask", "debug"])
def test_case_3_invoking_a_removed_command_fails(command: str) -> None:
    """CASE 3. Not an alias, not a deprecation notice, not a forward to `search`."""
    from repoatlas.cli.main import app

    result = runner.invoke(app, [command, "anything"])
    assert result.exit_code != 0


@pytest.mark.parametrize("command", sorted(EXPECTED_COMMANDS))
def test_case_4_no_surviving_command_accepts_no_llm(command: str) -> None:
    """CASE 4. The flag existed because `ask` could bypass the model it owned.

    It is not transplanted onto ``locate``, which has no model to bypass.
    """
    from repoatlas.cli.main import app

    result = runner.invoke(app, [command, "--help"])
    assert "--no-llm" not in result.output


def test_case_4_the_flag_is_not_spelled_anywhere_in_the_product() -> None:
    """CASE 4. Including in a help string or a docstring that still offers it."""
    for path in PRODUCTION.rglob("*.py"):
        assert "--no-llm" not in path.read_text(encoding="utf-8"), path


def test_case_8_locate_still_answers(initialized: Workspace, workspace_root: Path) -> None:
    """CASE 8. The deterministic developer tool the removal must not cost.

    ``locate`` was never a reasoning command --- it stops at the evidence set ---
    and it is the one V1 surface that had to survive intact.
    """
    from repoatlas.services.retrieval_service import locate, locate_diagnostic

    context = initialized.resolver().resolve(workspace_root / SERVICE)
    evidence = locate(initialized, "SERVICE_HOST", context)
    assert evidence.candidates
    assert any(candidate.path.endswith("config.py") for candidate in evidence.candidates)

    widened = locate_diagnostic(initialized, "SERVICE_HOST", context)
    assert widened.candidates


# ---------------------------------------------------------------------------
# CASE 5. Configuration
# ---------------------------------------------------------------------------


def test_case_5_the_default_configuration_names_no_reasoning_model() -> None:
    """CASE 5. What a fresh install writes, and what it must not mention."""
    written = yaml.safe_dump(Settings().model_dump(mode="json"), sort_keys=True)
    assert "llm" not in yaml.safe_load(written)
    for marker in ("ollama", "gpt-oss", "11434", "base_url"):
        assert marker not in written, marker


def test_case_5_the_settings_model_has_no_reasoning_section() -> None:
    """CASE 5. Read from the model, so a field that exists but defaults to None
    cannot pass by being absent from a dump."""
    assert "llm" not in Settings.model_fields
    assert "embeddings" not in Settings.model_fields
    assert "retrieval" in Settings.model_fields


def test_case_5_no_reasoning_environment_variable_is_read() -> None:
    """CASE 5. ``REPOATLAS_LLM_BASE_URL`` is not merely unused --- it is unread.

    Asserted in a fresh interpreter with the variable set to something that would
    be obvious if it landed anywhere.
    """
    result = _in_a_fresh_interpreter("""
        import json
        import os

        os.environ["REPOATLAS_LLM_BASE_URL"] = "http://sentinel.invalid:99999"
        os.environ["REPOATLAS_CONFIG"] = "/nonexistent/repoatlas/config.yaml"

        from repoatlas.config.settings import load_settings

        print(json.dumps(load_settings().model_dump(mode="json")))
    """)
    assert result.returncode == 0, result.stderr
    assert "sentinel.invalid" not in result.stdout

    # And no production module names it in code. Docstrings are exempt: a comment
    # saying a thing was removed is not a surviving reference to it.
    for path in PRODUCTION.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(ast.get_docstring(node, clean=False))
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node.value) in docstrings:
                continue
            assert "REPOATLAS_LLM" not in node.value, f"{path}:{node.lineno}"


# ---------------------------------------------------------------------------
# CASE 7. No reference to a local reasoner survives in the product
# ---------------------------------------------------------------------------

#: A provider, an endpoint or a port, in any casing and anywhere in the file.
#: These name a *product*, so a mention in prose is as much a reference as a
#: mention in code: nothing in RepoAtlas has an opinion about Ollama any more.
FORBIDDEN_PROSE = ("ollama", "/api/chat", "/api/generate", "11434", "gpt-oss")

#: Identifiers the deleted subsystem defined. Matched case-sensitively and on
#: word boundaries, because the English word "reasoner" is exactly what a comment
#: explaining the removal is supposed to say --- and a comment saying so is the
#: opposite of a surviving reference.
#:
#: ``model`` is deliberately absent from both lists. Embedding model ids are
#: legitimate and pervasive, and a rule that banned the word would either be
#: defeated immediately or would delete deterministic retrieval configuration in
#: order to satisfy itself.
FORBIDDEN_IDENTIFIERS = (
    "OllamaBackend",
    "NullBackend",
    "ScriptedBackend",
    "ReasoningModel",
    "ReasoningModelUnavailableError",
    "Reasoner",
    "ReasoningResult",
    "QueryIntent",
    "LLMSettings",
    "build_model",
    "build_messages",
    "format_evidence",
    "ModelFailure",
    "render_answer",
    "render_diagnosis",
    "confidence_label",
)


@pytest.mark.parametrize("marker", FORBIDDEN_PROSE)
def test_case_7_no_production_module_names_a_provider_or_endpoint(marker: str) -> None:
    """CASE 7. Case-insensitive: the provider name appears in prose as readily as
    in code, and neither is wanted."""
    offenders = [
        str(path.relative_to(ROOT))
        for path in PRODUCTION.rglob("*.py")
        if marker.lower() in path.read_text(encoding="utf-8").lower()
    ]
    assert offenders == [], f"{marker}: {offenders}"


@pytest.mark.parametrize("marker", FORBIDDEN_IDENTIFIERS)
def test_case_7_no_production_module_names_a_deleted_identifier(marker: str) -> None:
    """CASE 7. The code half, so that a dangling name is caught before an import
    error would have found it."""
    pattern = re.compile(rf"\b{re.escape(marker)}\b")
    offenders = [
        str(path.relative_to(ROOT))
        for path in PRODUCTION.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"{marker}: {offenders}"


def test_case_7_the_shipped_skill_does_not_offer_local_reasoning() -> None:
    """CASE 7. The skill is behavioural guidance an agent reads and acts on.

    One that mentioned a local model would be instructing an agent to expect a
    capability RepoAtlas does not have.
    """
    skill = (ROOT / "skills" / "repoatlas-code-trace" / "SKILL.md").read_text(encoding="utf-8")
    for marker in ("ollama", "repoatlas ask", "repoatlas debug", "--no-llm", "local model"):
        assert marker.lower() not in skill.lower(), marker


# ---------------------------------------------------------------------------
# CASES 9-10. Every capability still works
# ---------------------------------------------------------------------------


def test_case_9_repositories(service: CapabilityService) -> None:
    assert service.repositories(RepositoriesRequest(context=GLOBAL)).repositories


def test_case_9_status(service: CapabilityService) -> None:
    response = service.status(StatusRequest(context=GLOBAL))
    assert response.root_configured is True
    assert response.freshness is not None


def test_case_9_search(service: CapabilityService) -> None:
    response = service.search(SearchRequest(context=GLOBAL, question="SERVICE_HOST"))
    assert response.evidence


def test_case_9_search_with_a_prose_question(service: CapabilityService) -> None:
    """A question with no identifier in it, the kind a model once answered."""
    response = service.search(
        SearchRequest(context=GLOBAL, question="how does the service read its database settings")
    )
    assert response.freshness is not None


def test_case_9_inspect(service: CapabilityService) -> None:
    response = service.inspect(
        InspectRequest(
            context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=SERVICE),
            target=InspectTarget(path="src/config.py"),
        )
    )
    assert response.segments


def test_case_9_relationships(service: CapabilityService) -> None:
    response = service.relationships(
        RelationshipsRequest(context=GLOBAL, target="env:SERVICE_HOST", max_neighbors=50)
    )
    assert response.edges


def test_case_9_dependencies(service: CapabilityService) -> None:
    assert service.dependencies(DependenciesRequest(context=GLOBAL)).packages


def test_case_9_sync(service: CapabilityService) -> None:
    """The write path, which is where a model would be least expected and worst."""
    assert service.sync(SyncRequest(context=GLOBAL)).status is not None


def test_case_10_verify_grounding_still_verifies(service: CapabilityService) -> None:
    """CASE 10. The capability whose implementation the reasoner used to share.

    ``ground_answer`` moved out of ``llm/`` before any of this, precisely because
    it is deterministic provenance checking rather than model code. This asserts
    that the move was real: the whole package is gone and verification still
    accepts a true citation and refuses a fabricated one.
    """
    found = service.search(SearchRequest(context=GLOBAL, question="SERVICE_HOST"))
    item = found.evidence[0]

    grounded = service.verify_grounding(
        VerifyGroundingRequest(
            investigation_id=found.investigation_id,
            answer=AgentAnswer(
                summary="s",
                claims=(
                    AgentClaim(
                        statement="SERVICE_HOST is configured here",
                        citations=(AgentCitation(evidence_id=item.id),),
                    ),
                ),
            ),
        )
    )
    assert grounded.verdict == "grounded"
    assert grounded.sanitized_answer.claims

    fabricated = service.verify_grounding(
        VerifyGroundingRequest(
            investigation_id=found.investigation_id,
            answer=AgentAnswer(
                summary="s",
                claims=(
                    AgentClaim(
                        statement="A citation to evidence nobody supplied",
                        citations=(AgentCitation(evidence_id="E999"),),
                    ),
                ),
            ),
        )
    )
    assert fabricated.verdict == "ungrounded"
    assert fabricated.sanitized_answer.claims == ()


def test_case_17_grounding_is_importable_and_is_what_verification_uses() -> None:
    """CASE 17. The one piece of the old `llm/` package that was load-bearing."""
    from repoatlas.grounding import ground_answer

    assert callable(ground_answer)

    source = (PRODUCTION / "capabilities" / "verification.py").read_text(encoding="utf-8")
    assert "from repoatlas.grounding import" in source

    result = _in_a_fresh_interpreter("""
        import sys
        import repoatlas.capabilities.verification
        print("repoatlas.grounding" in sys.modules)
    """)
    assert result.stdout.strip() == "True", result.stderr


# ---------------------------------------------------------------------------
# CASES 11-12. The transport
# ---------------------------------------------------------------------------


def test_case_11_the_transport_publishes_exactly_ten_tools() -> None:
    """CASE 11. The public inventory, which removal must not have touched."""
    from repoatlas.mcp.tools import TOOLS

    assert len(TOOLS) == 10
    assert {spec.name for spec in TOOLS} == {
        "repoatlas_repositories",
        "repoatlas_status",
        "repoatlas_search",
        "repoatlas_inspect",
        "repoatlas_impact",
        "repoatlas_relationships",
        "repoatlas_dependencies",
        "repoatlas_http_operations",
        "repoatlas_verify_grounding",
        "repoatlas_sync",
    }


def test_case_12_the_transport_offers_no_prompts_or_resources() -> None:
    """CASE 12. A prompt is what the removed subsystem was made of.

    MCP prompts are a different mechanism from the reasoner's templates, and
    RepoAtlas publishes neither: it supplies evidence, and an agent brings its own
    instructions.
    """
    source = (PRODUCTION / "mcp" / "server.py").read_text(encoding="utf-8")
    for absent in ("list_prompts", "get_prompt", "list_resources", "read_resource"):
        assert absent not in source, absent


# ---------------------------------------------------------------------------
# CASES 15-16, 18. What had to survive
# ---------------------------------------------------------------------------


def test_case_15_the_evaluation_harness_needs_no_model() -> None:
    """CASE 15. The corpus is the retrieval measurement, and it still runs.

    The runner is imported and one case executed rather than the whole corpus,
    which is a gate of its own and far too slow for this suite. What is asserted
    here is that nothing in the harness reaches for a reasoner.
    """
    from tests.evaluation import runner as evaluation_runner

    source = Path(evaluation_runner.__file__).read_text(encoding="utf-8")
    assert "repoatlas.llm" not in source
    assert "QueryIntent" not in source

    cases = evaluation_runner.load_cases()
    assert cases
    assert {case.command for case in cases} <= {"locate", "ask", "debug"}


def test_case_16_no_schema_migration_was_required() -> None:
    """CASE 16. The reasoner owned no indexed state, so nothing in the database
    moved. Bumping the version because Python modules disappeared would force a
    migration on every existing index to change nothing about it.
    """
    from repoatlas.storage.sqlite.database import SCHEMA_VERSION

    migrations = sorted((PRODUCTION / "storage" / "sqlite" / "migrations").glob("*.sql"))
    assert len(migrations) == SCHEMA_VERSION
    for migration in migrations:
        text = migration.read_text(encoding="utf-8").lower()
        for marker in ("llm", "ollama", "prompt", "reasoning"):
            assert marker not in text, f"{migration.name}: {marker}"


def test_case_18_no_embedding_model_remains() -> None:
    """CASE 18. Retrieval is deterministic end to end: no vector backend, no
    embedding settings and no stage that ranks by similarity."""
    import importlib.util

    assert "embeddings" not in Settings.model_fields
    assert importlib.util.find_spec("repoatlas.embeddings") is None
    for field in RetrievalSettings.model_fields:
        assert "semantic" not in field, field


def test_case_18_no_semantic_stage_runs(service: CapabilityService) -> None:
    """CASE 18. Absent from configuration is not the same as absent from a run."""
    response = service.search(
        SearchRequest(context=GLOBAL, question="SERVICE_HOST", include_trace=True)
    )
    assert response.trace is not None
    assert "semantic" not in {stage.name for stage in response.trace.stages}


# ---------------------------------------------------------------------------
# CASE 13. Offline
# ---------------------------------------------------------------------------


def test_case_13_the_transport_serves_a_session_with_sockets_denied(
    initialized: Workspace, tmp_path: Path
) -> None:
    """CASE 13. The strongest statement this file makes.

    Not "no model was configured" and not "the port was closed" --- ``socket``
    itself is made to raise before RepoAtlas is imported, so any attempt to reach
    anything at all is fatal rather than merely unsuccessful. A full MCP
    handshake, the tool inventory and a real search run under that.

    An HTTP library being importable is not network use: the MCP SDK brings one
    transitively and never opens a connection on stdio.
    """
    database = initialized.database.path
    root = initialized.settings.repos.root
    assert root is not None
    initialized.close()

    config = tmp_path / "offline.yaml"
    config.write_text(yaml.safe_dump({"repos": {"root": str(root)}}), encoding="utf-8")

    source = textwrap.dedent("""
        import json
        import socket

        from repoatlas.capabilities.service import CapabilityService
        from repoatlas.models.capability import CapabilityContext, CapabilityScope
        from repoatlas.models.capability_io import RepositoriesRequest
        from repoatlas.models.search import SearchRequest
        from repoatlas.mcp.tools import TOOLS
        from repoatlas.services.workspace import open_workspace


        def refuse(*args, **kwargs):
            raise OSError("network denied")


        # After the imports, which is the only way to do this: `ssl` defines a
        # class whose base is `socket.socket`, so replacing the name beforehand
        # breaks the interpreter rather than the product. Importing an HTTP
        # library is not network use; opening a connection is, and from here on
        # nothing can.
        socket.socket = refuse
        socket.socket_type = refuse
        socket.create_connection = refuse
        socket.getaddrinfo = refuse

        workspace = open_workspace()
        service = CapabilityService(workspace)
        context = CapabilityContext(scope=CapabilityScope.GLOBAL)
        found = service.search(SearchRequest(context=context, question="SERVICE_HOST"))
        repositories = service.repositories(RepositoriesRequest(context=context))
        service.close()

        print(json.dumps({
            "tools": len(TOOLS),
            "evidence": len(found.evidence),
            "repositories": len(repositories.repositories),
        }))
    """)
    result = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "REPOATLAS_CONFIG": str(config),
            "REPOATLAS_DB": str(database),
            "PYTHONPATH": str(ROOT),
        },
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["tools"] == 10
    assert report["evidence"] > 0
    assert report["repositories"] > 0


# ---------------------------------------------------------------------------
# The two ways a removal like this quietly loses coverage
# ---------------------------------------------------------------------------

#: Vocabulary that would mark a skip as "this used to test the reasoner".
RETIRED_SUBJECTS = ("llm", "ollama", "reasoner", "reasoning model", "no-llm", "repoatlas ask")


def test_no_test_is_kept_alive_as_a_skip_for_a_removed_feature() -> None:
    """A deleted feature's tests are deleted, not neutered.

    The cheap way through a removal is to leave the suites in place and skip
    them. It looks like caution and it is the opposite: the file still counts as
    coverage in every listing, still has to be read and maintained, and asserts
    nothing at all. Worse, it reads as "temporarily disabled", which invites
    somebody to try to turn it back on.

    Scoped to skips whose *reason* names the removed subsystem. Skipping because
    ``uv`` is absent, or because the test needs root, is ordinary and untouched.
    """
    offenders: list[str] = []
    for path in (ROOT / "tests").rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_skip(node.func):
                continue
            reasons = [
                argument.value
                for argument in [*node.args, *(k.value for k in node.keywords)]
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
            ]
            for reason in reasons:
                if any(subject in reason.lower() for subject in RETIRED_SUBJECTS):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {reason}")
    assert offenders == [], offenders


def _is_skip(func: ast.expr) -> bool:
    """``pytest.skip``, ``pytest.mark.skip``, ``pytest.mark.skipif`` or a bare alias.

    Read from the call itself rather than from the surrounding text, so this
    module's own list of retired vocabulary is not mistaken for a skip reason ---
    which is exactly what a proximity heuristic did on the first attempt.
    """
    if isinstance(func, ast.Attribute):
        return func.attr in {"skip", "skipif"}
    return isinstance(func, ast.Name) and func.id in {"skip", "skipif"}


#: Deterministic trust assertions that were carried *out* of the mixed reasoning
#: suites rather than deleted with them.
#:
#: Those suites tested two things at once: what ``ground_answer`` does with a
#: hostile answer, and what V1's reasoner and renderer did with one. Deleting them
#: wholesale would have been the easy reading of "delete tests for behaviour that
#: is gone", and it would have taken the grounding coverage with it --- silently,
#: because nothing fails when a test disappears.
#:
#: So the census is explicit. Renaming one of these is fine; losing it is not.
RETAINED_TRUST_COVERAGE: dict[str, tuple[str, ...]] = {
    "tests/adversarial/test_trust_boundary.py": (
        "test_an_injected_instruction_does_not_change_what_is_retrieved",
        "test_an_answer_that_obeys_an_injected_instruction_is_still_ungrounded",
        "test_a_citation_to_a_repository_that_does_not_exist_is_dropped",
        "test_a_citation_to_a_file_that_was_not_supplied_is_dropped",
        "test_a_line_range_outside_the_supplied_span_is_dropped",
        "test_a_partially_overlapping_range_is_clamped_to_what_was_shown",
        "test_a_real_citation_beside_a_fabricated_one_sinks_the_whole_claim",
        "test_confidence_does_not_protect_an_unsupported_citation",
        "test_a_finding_with_no_evidence_at_all_cannot_stand",
        "test_an_answer_whose_findings_are_all_demoted_becomes_insufficient",
        "test_an_empty_evidence_set_supports_no_citation_whatsoever",
    ),
    "tests/adversarial/test_answer_trust_model.py": (
        "test_an_unsupported_repository_reference_never_survives_any_field",
        "test_a_valid_citation_does_not_launder_a_fabricated_statement",
        "test_one_fabricated_citation_beside_real_ones_sinks_the_hypothesis",
        "test_a_claim_without_path_syntax_is_still_never_presented_as_evidence",
        "test_the_surviving_citations_are_only_retrieved_locations",
        "test_no_free_text_field_can_become_a_citation",
        "test_a_clamped_citation_cannot_keep_its_original_confidence",
        "test_the_independent_review_reproduction_is_closed",
        "test_a_supported_hypothesis_survives_as_a_hypothesis",
        "test_no_surviving_hypothesis_yields_insufficient_evidence",
        "test_an_empty_evidence_set_supports_nothing",
    ),
    "tests/adversarial/test_unanswerable.py": (
        "test_a_named_literal_that_exists_nowhere_yields_no_evidence",
        "test_the_suppression_is_recorded_so_it_can_be_audited",
        "test_secrets_are_unreachable_rather_than_approximated",
    ),
}


@pytest.mark.parametrize("module", sorted(RETAINED_TRUST_COVERAGE))
def test_no_deterministic_trust_assertion_was_lost_with_the_reasoner(module: str) -> None:
    """The census, checked by name."""
    path = ROOT / module
    assert path.is_file(), module

    tree = ast.parse(path.read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    missing = sorted(set(RETAINED_TRUST_COVERAGE[module]) - defined)
    assert missing == [], f"{module}: {missing}"


def test_the_retained_trust_assertions_really_exercise_grounding() -> None:
    """A census of names is only worth having if the names still do the work.

    Without this, the census could be satisfied by empty functions --- which is
    mutation O wearing mutation P's clothes.
    """
    for module in RETAINED_TRUST_COVERAGE:
        source = (ROOT / module).read_text(encoding="utf-8")
        tree = ast.parse(source)
        wanted = set(RETAINED_TRUST_COVERAGE[module])
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name not in wanted:
                continue
            asserts = [child for child in ast.walk(node) if isinstance(child, ast.Assert)]
            assert asserts, f"{module}::{node.name} asserts nothing"
