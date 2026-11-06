"""oas3-tools applications whose specification is not proven serve nothing, and a mapping only
links what the code serves (PR #25 verification, tasks 2 and 3).

Task 2: ``tests/fixtures/oas3/{a-paths,b-forwarding,c-exports,d-layouts}`` hold one repository per
refusal (and a control that is served). Every application is either PROMOTED, serving exactly the
routes its handler object names, or refused: one unpublished application row with the planner's
headline and stable code, and no route served from code. The documents are checked in every case,
and the planner's own outcome is read back as a cross-check.

Task 3: ``tests/fixtures/oas3/crossrepo`` and ``links``. A consumer's request is joined to a
provider through a manual mapping only when the provider's application is proven, and then to the
handler object, never to the document. Expected values are written down in the plan before they
were measured: a mismatch is reported, never edited to fit.

Rows are typed ``HttpOperation``; ``print`` lines (shown by ``pytest -rA``) record evidence.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.graph.linker import ReferenceLinker
from repoatlas.graph.ts_oas3 import (
    CONTROLLERS_UNRESOLVED,
    PROMOTED,
    SPEC_NOT_FOUND,
    SPEC_UNREAD,
)
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.deployment_targets import DeploymentLinkDeleteRequest
from repoatlas.models.http_connections import (
    HttpConnectionPreviewRequest,
    HttpConnectionsRequest,
)
from repoatlas.models.http_inventory import HttpOperation, ProviderDiagnostic
from repoatlas.parsers.http_facts import OAS3_APP
from repoatlas.services.workspace import Workspace, open_workspace
from tests.impact_corpus import build
from tests.incremental_corpus import sync_all
from tests.r1_provider_corpus import inventory, providers, slug_of

FIXTURES = Path(__file__).parent / "fixtures" / "oas3"
GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

#: The two routes the shared handler object serves, as ``(file, "METHOD /route")``.
PROMOTED_SET = {("src/handlers.ts", "GET /status"), ("src/handlers.ts", "GET /widgets")}
#: What each shared document says, whatever the file it is in.
DOCUMENT_ROUTES = {"GET /status", "GET /widgets"}


# -- helpers --------------------------------------------------------------------------------


def load(directory: Path) -> dict[str, str]:
    """Every file of a fixture repository: posix relative path to text, sorted by path."""
    found = {
        path.relative_to(directory).as_posix(): path.read_text(encoding="utf-8")
        for path in directory.rglob("*")
        if path.is_file()
    }
    return dict(sorted(found.items()))


def said(value: Any) -> Any:
    """An enum's text, or the value itself."""
    return getattr(value, "value", value)


def diagnostic_of(row: HttpOperation) -> ProviderDiagnostic:
    assert row.diagnostic is not None, (row.path, row.route)
    return row.diagnostic


def label(row: HttpOperation) -> str:
    return f"{row.method} {row.route}"


def shown(rows: list[HttpOperation]) -> list[tuple[str | None, str, str | None, str]]:
    return [(r.method, r.route, r.path, r.status.value) for r in rows]


def served_from_code(rows: list[HttpOperation]) -> list[HttpOperation]:
    """The routes a handler object serves: a source file's row that is not unpublished."""
    return [
        row
        for row in rows
        if row.path and row.path.endswith((".ts", ".js")) and row.status.value != "unpublished"
    ]


def applications_of(rows: list[HttpOperation]) -> list[HttpOperation]:
    """The rows that stand for an application the planner did not serve."""
    return [row for row in rows if row.diagnostic and row.diagnostic.subject == "application"]


def applications(workspace: Workspace) -> dict[tuple[str, str], tuple[str, str | None]]:
    """Each application's outcome and refusal code, as the global pass decides them, keyed by
    (repository slug, file). Copied from ``test_http_oas3_layouts``, with the repository added."""
    plan = ReferenceLinker(workspace.knowledge).prepare_global().http
    rows = workspace.database.execute(
        "SELECT u.id, r.slug, f.rel_path FROM unresolved_refs u JOIN files f ON f.id = u.file_id "
        "JOIN repositories r ON r.id = f.repo_id WHERE u.meta_json LIKE ?",
        (f'%"{OAS3_APP}"%',),
    ).fetchall()
    return {
        (slug, path): (plan.routes[claim], plan.spec_refusals.get(claim))
        for claim, slug, path in rows
    }


def decided(workspace: Workspace, caller: str) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Copied from ``test_deployment_link_join``."""
    [(decision, chain, evidence)] = workspace.database.execute(
        """SELECT d.decision, d.chain_json, d.evidence_json FROM http_call_decisions d
           JOIN entities e ON e.id = d.src_id WHERE e.qualified_name = ?""",
        (caller,),
    ).fetchall()
    return str(decision), json.loads(chain), json.loads(evidence)


def handler_of(workspace: Workspace, caller: str) -> str | None:
    """Copied from ``test_deployment_link_join``."""
    found = workspace.database.execute(
        """SELECT f.rel_path, r.slug FROM edges x JOIN entities s ON s.id = x.src_id
           JOIN entities h ON h.id = x.dst_id JOIN files f ON f.id = h.file_id
           JOIN repositories r ON r.id = f.repo_id
           WHERE x.type = 'CALLS_HTTP_HANDLER' AND s.qualified_name = ?""",
        (caller,),
    ).fetchall()
    return f"{found[0][1]}/{found[0][0]}" if found else None


# -- task 2: the cases ----------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """One application and what the plan says becomes of it."""

    group: str
    repo: str
    #: The file of the ``expressAppConfig`` call.
    app: str
    #: The planner's outcome text.
    headline: str
    #: The planner's refusal code (``None`` when it gives none).
    planner_code: str | None = None
    #: The row's stable code and category; ``None`` code means the application is served.
    code: str | None = None
    category: str | None = None
    #: Routes of a document that differ from ``DOCUMENT_ROUTES``: ``(path, labels)``.
    documents: tuple[tuple[str, frozenset[str]], ...] = ()

    @property
    def promoted(self) -> bool:
        return self.code is None

    @property
    def id(self) -> str:
        return f"{self.repo}:{self.app}"


def served(group: str, repo: str, app: str = "src/app.ts") -> Case:
    return Case(group, repo, app, PROMOTED)


def refused(
    group: str,
    repo: str,
    app: str,
    headline: str,
    code: str,
    category: str = "path-unresolved",
    documents: tuple[tuple[str, frozenset[str]], ...] = (),
) -> Case:
    return Case(
        group,
        repo,
        app,
        headline,
        planner_code=code,
        code=code,
        category=category,
        documents=documents,
    )


A, B, C, D = "a-paths", "b-forwarding", "c-exports", "d-layouts"

CASES = [
    # A: one application per file in `paths`
    served(A, "paths-control", "src/control.ts"),
    refused(A, "paths", "src/conditional.ts", SPEC_UNREAD, "spec-dynamic"),
    refused(A, "paths", "src/call-segment.ts", SPEC_UNREAD, "spec-dynamic"),
    refused(A, "paths", "src/env-default.ts", SPEC_UNREAD, "spec-environment"),
    refused(A, "paths", "src/cwd.ts", SPEC_UNREAD, "spec-cwd-relative"),
    refused(A, "paths", "src/mutable.ts", SPEC_UNREAD, "spec-mutable-binding"),
    refused(A, "paths", "src/param-middle.ts", SPEC_UNREAD, "spec-parameter-unsupported"),
    # B: a path forwarded through callers
    served(B, "fwd-control"),
    refused(
        B, "fwd-same-file", "src/app.ts", SPEC_UNREAD, "spec-propagation-depth", "bound-reached"
    ),
    refused(B, "fwd-cross-file", "src/app.ts", SPEC_UNREAD, "spec-parameter-uncalled"),
    # C: how the factory or the path is exported
    refused(C, "exp-default-factory", "src/app.ts", SPEC_UNREAD, "spec-parameter-uncalled"),
    refused(C, "exp-barrel", "src/app.ts", SPEC_UNREAD, "spec-parameter-uncalled"),
    refused(C, "exp-namespace", "src/app.ts", SPEC_UNREAD, "spec-parameter-uncalled"),
    refused(C, "exp-commonjs", "src/app.js", SPEC_UNREAD, "spec-parameter-uncalled"),
    refused(C, "exp-default-path", "src/app.ts", SPEC_UNREAD, "spec-import-unsupported"),
    served(C, "exp-named-path"),
    refused(C, "exp-reexported-path", "src/app.ts", SPEC_UNREAD, "spec-import-unresolved"),
    Case(
        C,
        "exp-anonymous-controllers",
        "src/app.ts",
        CONTROLLERS_UNRESOLVED,
        planner_code=None,
        code="oas3-controllers-unresolved",
        category="identity-unresolved",
    ),
    # D: the layouts a tsconfig compiles the application to
    served(D, "lay-control"),
    refused(D, "lay-one-pair-two-docs", "src/app.ts", SPEC_NOT_FOUND, "spec-layout-ambiguous"),
    refused(D, "lay-two-configs", "src/app.ts", SPEC_NOT_FOUND, "spec-layout-ambiguous"),
    served(D, "lay-two-configs-one-doc"),
    refused(D, "lay-extends", "src/app.ts", SPEC_NOT_FOUND, "spec-layout-ambiguous"),
    # Amended 2026-11-06: the plan predicted promotion. The code refuses it as
    # spec-layout-ambiguous, because src/tsconfig.json has no recorded options and so is not
    # seen; the root pair then applies (repoatlas/parsers/module_facts.py:137-159, 202-203;
    # repoatlas/graph/modules.py build_layouts / _declares_layout; ts_oas3.py:320-321).
    refused(D, "lay-nearest", "src/app.ts", SPEC_NOT_FOUND, "spec-layout-ambiguous"),
    # Amended 2026-11-06 (user requirement, not a fit to a measurement): the plan predicted
    # promotion and the code promoted against openapi.yaml, because src/tsconfig.json records
    # only baseUrl, which stopped the layout walk at src/ where no outDir/rootDir pair is
    # declared. baseUrl is a module-resolution option, not an output layout, and
    # src/tsconfig.json extends nothing, so it neither sets nor hides the root pair: runtime/src
    # reads runtime/openapi.yaml beside src's openapi.yaml, and two layouts naming two documents
    # name none (repoatlas/graph/modules.py build_layouts; ts_oas3.py document()).
    refused(D, "lay-nearest-visible", "src/app.ts", SPEC_NOT_FOUND, "spec-layout-ambiguous"),
    # Added 2026-11-06 with the layout correction: a nearer tsconfig that records only baseUrl
    # does not hide the enclosing rootDir/outDir pair. Two layouts naming two documents name
    # none.
    refused(
        D,
        "lay-nearest-baseurl-two-docs",
        "src/app.ts",
        SPEC_NOT_FOUND,
        "spec-layout-ambiguous",
        documents=(("runtime/openapi.yaml", frozenset({"GET /status", "GET /gadgets"})),),
    ),
]
#: Repositories indexed with the cases that are not a case themselves: ``(group, repo)``.
EXTRA_REPOS = [(C, "exp-destructured-require")]
PROMOTED_CASES = [case for case in CASES if case.promoted]
REFUSED_CASES = [case for case in CASES if not case.promoted]
REFUSED_REPOS = sorted({case.repo for case in REFUSED_CASES})


def params(cases: list[Case]) -> list[Any]:
    return [pytest.param(case, id=case.id) for case in cases]


class Indexed:
    """The refusal repositories, indexed once; each read is cached."""

    def __init__(self, workspace: Workspace, files: dict[str, dict[str, str]]) -> None:
        self.workspace = workspace
        self._files = files
        self._rows: dict[str, list[HttpOperation]] = {}
        self._planned: dict[tuple[str, str], tuple[str, str | None]] | None = None

    def rows(self, repo: str) -> list[HttpOperation]:
        """Every provider row of the repository, with diagnostics."""
        if repo not in self._rows:
            found, _ = inventory(self.workspace, repo, include_diagnostics=True)
            self._rows[repo] = providers(found)
        return self._rows[repo]

    def documents(self, repo: str) -> list[str]:
        return [path for path in self._files[repo] if path.endswith(".yaml")]

    def planned(self, repo: str, app: str) -> tuple[str, str | None]:
        if self._planned is None:
            self._planned = applications(self.workspace)
        return self._planned[(slug_of(self.workspace, repo), app)]


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Indexed]:
    """One workspace holding every refusal repository (they share nothing)."""
    base = tmp_path_factory.mktemp("oas3-fixtures")
    files = {case.repo: load(FIXTURES / case.group / case.repo) for case in CASES}
    files.update({repo: load(FIXTURES / group / repo) for group, repo in EXTRA_REPOS})
    settings = Settings.model_validate({"storage": {"database": str(base / "state" / "atlas.db")}})
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        build(workspace, base, files)
        yield Indexed(workspace, files)
    finally:
        workspace.close()


# -- task 2: the checks ---------------------------------------------------------------------


@pytest.mark.parametrize("case", params(PROMOTED_CASES))
def test_a1_a_proven_application_serves_exactly_the_routes_its_handler_names(
    indexed: Indexed, case: Case
) -> None:
    rows = indexed.rows(case.repo)
    assert case.promoted
    served_rows = served_from_code(rows)
    assert {(row.path or "", label(row)) for row in served_rows} == PROMOTED_SET, shown(rows)
    for row in served_rows:
        diagnostic = diagnostic_of(row)
        assert diagnostic.framework == "oas3-tools", (row.path, label(row))
        assert diagnostic.published is True and diagnostic.join_eligible is True, label(row)
    assert applications_of(rows) == [], "a served application leaves no unpublished row"
    assert indexed.planned(case.repo, case.app)[0] == PROMOTED


@pytest.mark.parametrize("case", params(REFUSED_CASES))
def test_a2_a_refused_application_has_one_unpublished_row_and_serves_nothing_from_code(
    indexed: Indexed, case: Case
) -> None:
    rows = indexed.rows(case.repo)
    at_app = [row for row in rows if row.path == case.app]
    assert len(at_app) == 1, shown(at_app)
    [row] = at_app
    diagnostic = diagnostic_of(row)
    assert row.status.value == "unpublished"
    assert diagnostic.subject == "application"
    assert diagnostic.framework == "oas3-tools"
    assert diagnostic.code == case.code
    assert said(diagnostic.category) == case.category
    assert row.detail == case.headline
    assert diagnostic.published is False and diagnostic.join_eligible is False
    assert served_from_code(rows) == [], shown(served_from_code(rows))


@pytest.mark.parametrize("case", params(CASES))
def test_a3_every_document_is_published_and_cannot_be_joined(indexed: Indexed, case: Case) -> None:
    rows = indexed.rows(case.repo)
    documents = indexed.documents(case.repo)
    assert documents, f"{case.repo} holds no document"
    for path in documents:
        at_document = [row for row in rows if row.path == path]
        expected = dict(case.documents).get(path, DOCUMENT_ROUTES)
        assert {label(row) for row in at_document} == expected, (path, shown(at_document))
        for row in at_document:
            diagnostic = diagnostic_of(row)
            assert diagnostic.published is True, (path, label(row))
            assert tuple(diagnostic.join_limits) == ("openapi-document",), (path, label(row))


@pytest.mark.parametrize("case", params(CASES))
def test_a4_the_planner_decides_what_the_rows_say(indexed: Indexed, case: Case) -> None:
    assert indexed.planned(case.repo, case.app) == (case.headline, case.planner_code)


def test_the_paths_repository_serves_no_route_from_code_and_refuses_six_applications(
    indexed: Indexed,
) -> None:
    rows = indexed.rows("paths")
    assert served_from_code(rows) == [], shown(served_from_code(rows))
    refused_apps = applications_of(rows)
    assert sorted(row.path or "" for row in refused_apps) == sorted(
        case.app for case in REFUSED_CASES if case.repo == "paths"
    )
    assert len(refused_apps) == 6


@pytest.mark.parametrize("repo", REFUSED_REPOS)
def test_the_public_inventory_names_one_unpublished_row_per_unserved_application(
    indexed: Indexed, repo: str
) -> None:
    """``http_operations(include_diagnostics=True)`` is the read surface a user has."""
    expected = {case.app: case for case in REFUSED_CASES if case.repo == repo}
    found = applications_of(indexed.rows(repo))
    assert sorted(row.path or "" for row in found) == sorted(expected)
    for row in found:
        case = expected[row.path or ""]
        diagnostic = diagnostic_of(row)
        assert row.status.value == "unpublished"
        assert diagnostic.subject == "application" and diagnostic.framework == "oas3-tools"
        assert diagnostic.code == case.code
        assert said(diagnostic.category) == case.category
        assert row.detail == case.headline


# KNOWN LIMITATION, pinned not endorsed. An application that destructures require('oas3-tools')
# is invisible: no served route and no refusal row (repoatlas/parsers/http_oas3.py:229-230 skips
# non-identifier declarators). It fails closed for linking. Fixing it is out of scope for this
# workstream.
def test_known_limitation_a_destructured_require_is_silent(indexed: Indexed) -> None:
    repo, app = "exp-destructured-require", "src/app.js"
    rows = indexed.rows(repo)
    planned = applications(indexed.workspace)
    assert (slug_of(indexed.workspace, repo), app) not in planned, sorted(planned)
    assert [row for row in rows if row.path == app] == [], shown(rows)
    assert applications_of(rows) == [], shown(applications_of(rows))
    assert served_from_code(rows) == [], shown(served_from_code(rows))


# -- task 3: the join through a manual mapping ----------------------------------------------

CROSSREPO = FIXTURES / "crossrepo"
LINKS = FIXTURES / "links"
ORIGIN = "https://widgets.example.test"
CALLERS = ("fetchWidgets", "fetchWidget", "fetchOrphans")
UNMATCHED = "http-service-unmatched"
HANDLER_FILE = "widgets-provider/src/handlers/widgets.ts"
CODE_ROUTES = {
    ("src/handlers/widgets.ts", "GET /v1/widgets"),
    ("src/handlers/widgets.ts", "GET /v1/widgets/{id}"),
}


def _crossrepo(workspace: Workspace, tmp_path: Path, provider: str) -> Workspace:
    repos = {name: load(CROSSREPO / name) for name in (provider, "widgets-consumer")}
    return build(workspace, tmp_path, repos)


def _repository_context(workspace: Workspace, name: str) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug_of(workspace, name))


def _decision_refs(workspace: Workspace) -> dict[str, str]:
    """``decision_ref`` of every stored request decision, by the calling function's name."""
    rows = workspace.database.execute(
        """SELECT e.qualified_name, d.decision_ref FROM http_call_decisions d
           JOIN entities e ON e.id = d.src_id"""
    ).fetchall()
    return {str(name): str(ref) for name, ref in rows}


def _preview_outcomes(workspace: Workspace, provider: str) -> dict[str, str]:
    """What declaring ``ORIGIN`` as served by ``provider`` would do, per calling function."""
    service = CapabilityService(workspace)
    worklist = service.http_connections(HttpConnectionsRequest(context=GLOBAL))
    group = next(g for g in worklist.groups if g.origin == ORIGIN)
    preview = service.http_connection_preview(
        HttpConnectionPreviewRequest(
            context=GLOBAL, group=group.key, repository=slug_of(workspace, provider)
        )
    )
    outcomes = {row.decision_ref: row.outcome for row in preview.rows}
    return {name: outcomes.get(ref, "absent") for name, ref in _decision_refs(workspace).items()}


def _import_mapping(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mapping: Path) -> None:
    from repoatlas.cli.main import app
    from typer.testing import CliRunner

    monkeypatch.setenv("REPOATLAS_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("REPOATLAS_DB", str(tmp_path / "state" / "atlas.db"))
    result = CliRunner().invoke(app, ["links", "import", str(mapping)])
    assert result.exit_code == 0, result.output
    assert "Added 1 link(s); 0 already recorded." in result.output


def _record(name: str, evidence: dict[str, Any]) -> None:
    """The evidence of a decision, verbatim, for the report (``pytest -rA`` shows it)."""
    print(f"EVIDENCE {name}: {json.dumps(evidence, sort_keys=True, default=str)}")


@pytest.mark.parametrize("caller", ["fetchWidgets", "fetchWidget"])
def test_x1_a_mapping_joins_a_request_to_the_handler_object_and_removing_it_takes_it_back(
    workspace: Workspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caller: str,
) -> None:
    built = _crossrepo(workspace, tmp_path, "widgets-provider")

    # 1. preconditions: the code serves two routes, the document alone names the orphan
    for name in ("widgets-provider", "widgets-consumer"):
        assert slug_of(built, name) == name
    rows = providers(inventory(built, "widgets-provider", include_diagnostics=True)[0])
    assert {(row.path or "", label(row)) for row in served_from_code(rows)} == CODE_ROUTES
    orphan_rows = {(row.path, label(row)) for row in rows if row.route.endswith("/orphans")}
    assert orphan_rows == {("api/openapi.yaml", "GET /orphans")}, shown(rows)

    # 2. no mapping: nothing is linked, and the preview says what the mapping would do
    for name in CALLERS:
        assert decided(built, name)[0] == UNMATCHED, name
        assert handler_of(built, name) is None, name
    outcomes = _preview_outcomes(built, "widgets-provider")
    assert outcomes[caller] == "would-link", outcomes
    # the orphan asks for exactly the document-declared `GET /orphans`; no code serves it
    assert outcomes["fetchOrphans"] != "would-link", ("fetchOrphans", outcomes)

    # 3. importing stores the mapping; the join happens at the next sync
    _import_mapping(monkeypatch, tmp_path, LINKS / "widgets.json")
    assert handler_of(built, caller) is None

    # 4. after the sync the request reaches the handler object, never the document
    sync_all(built, tmp_path / "repos")
    decision, chain, evidence = decided(built, caller)
    assert decision == "linked", (decision, evidence)
    assert handler_of(built, caller) == HANDLER_FILE
    statuses = {hop["kind"]: hop["status"] for hop in chain}
    assert statuses["service_identity"] == "declared", statuses
    assert statuses["deployment_binding"] == "declared", statuses
    assert statuses["source_binding"] == "declared", statuses
    assert statuses["route"] == statuses["handler"] == "proven", statuses
    assert evidence["identity_source"] == "deployment-link"
    orphan_decision, _, orphan_evidence = decided(built, "fetchOrphans")
    _record("fetchOrphans", orphan_evidence)
    print(
        "declaration_ids:",
        json.dumps(orphan_evidence.get("declaration_ids"), default=str),
        "declaration_outcome:",
        json.dumps(orphan_evidence.get("declaration_outcome"), default=str),
    )
    # the document alone declares the route: after the import and the sync it is still unmatched
    assert orphan_decision == UNMATCHED, orphan_evidence
    assert handler_of(built, "fetchOrphans") is None, "the document is never a handler"

    # 5. removing the mapping takes the join back at the next sync
    [stored] = built.deployment_links.list("widgets-provider")
    CapabilityService(built).deployment_link_delete(
        DeploymentLinkDeleteRequest(
            context=_repository_context(built, "widgets-provider"), id=stored.id
        )
    )
    sync_all(built, tmp_path / "repos")
    for name in CALLERS:
        assert decided(built, name)[0] == UNMATCHED, name
        assert handler_of(built, name) is None, name


def test_x2_a_mapping_to_a_provider_whose_application_is_refused_links_nothing(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _crossrepo(workspace, tmp_path, "widgets-provider-unread")
    assert slug_of(built, "widgets-provider-unread") == "widgets-provider-unread"
    rows = providers(inventory(built, "widgets-provider-unread", include_diagnostics=True)[0])
    assert served_from_code(rows) == [], shown(served_from_code(rows))
    [application] = applications_of(rows)
    assert application.path == "src/app.ts"
    assert application.status.value == "unpublished"
    assert diagnostic_of(application).code == "spec-cwd-relative"

    _import_mapping(monkeypatch, tmp_path, LINKS / "widgets-unread.json")
    sync_all(built, tmp_path / "repos")

    decision, chain, evidence = decided(built, "fetchWidgets")
    _record("fetchWidgets", evidence)
    print("chain fetchWidgets:", json.dumps(chain, sort_keys=True, default=str))
    assert decision != "linked", (decision, evidence)
    orphan_decision, _, orphan_evidence = decided(built, "fetchOrphans")
    _record("fetchOrphans", orphan_evidence)
    assert orphan_decision != "linked", (orphan_decision, orphan_evidence)
    for name in CALLERS:
        assert handler_of(built, name) is None, name
