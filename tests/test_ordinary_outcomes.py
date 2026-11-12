"""The ordinary-claim account: what became of every claim, and nothing it must not touch.

Phase A of ORDINARY_REFERENCE_RESOLUTION.md. The account is its own model beside the legacy
``unresolved`` scalar, so these tests pin four things: it reconciles (every standing claim is
ordinary or excluded, every ordinary claim has one outcome, the residuals are measured), it
explains refusals only on evidence (``external`` needs a declaration nobody provides), it
cannot change a link, a planner decision or an impact answer, and it is bounded, ordered and
free of source text.

The corpus (tests/ordinary_corpus.py) is invented; each expectation names the claim that
earns it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.graph import ordinary_outcomes as outcomes_module
from repoatlas.graph.linker import GlobalLinkResult, ReferenceLinker
from repoatlas.graph.ordinary_outcomes import Decision, OutcomeLedger
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import ImpactRequest, Presentation
from repoatlas.models.ordinary_outcomes import (
    AMBIGUOUS,
    EXCLUSIONS,
    EXTERNAL,
    LANGUAGES,
    LINKED,
    OPEN_OUTCOMES,
    OPEN_ROWS_PER_KEY,
    ORDINARY_FAMILY,
    OUTCOMES,
    SUBREASONS,
    UNCLASSIFIED,
    UNSUPPORTED,
)
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.models.reports import SyncReport
from repoatlas.rendering.text import render_sync
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord
from tests.conftest import fact_snapshot
from tests.impact_corpus import build
from tests.ordinary_corpus import TOY_PY, repos

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


def classified_sync(workspace: Workspace, root: Path) -> SyncReport:
    """A sync that was asked for the account (``repoatlas sync -v``): the same link pass."""
    return sync(workspace, workspace.resolver().resolve(root), classify=True)


def classified_pass(workspace: Workspace) -> GlobalLinkResult:
    linker = ReferenceLinker(workspace.knowledge, workspace.dependencies)
    return linker.commit_global(linker.prepare_global(classify=True))


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    root = tmp_path_factory.mktemp("ordinary")
    workspace = open_workspace(
        settings=Settings.model_validate({"storage": {"database": str(root / "state/atlas.db")}}),
        require_root=False,
    )
    build(workspace, root, repos())
    classified_sync(workspace, root / "repos")
    yield workspace
    workspace.close()


def open_rows(workspace: Workspace) -> dict[tuple[str, str, str], tuple[str, str]]:
    """(path, line, subject) -> (outcome, subreason) for every open ordinary claim."""
    found: dict[tuple[str, str, str], tuple[str, str]] = {}
    for row in workspace.knowledge.list_link_decisions(family=ORDINARY_FAMILY):
        found[(row.path or "", str(row.line_start), row.subject)] = (
            row.decision,
            json.loads(row.evidence_json)["subreason"],
        )
    return found


def counts(workspace: Workspace) -> dict[tuple[str, str, str, str], int]:
    """(edge type, target kind, outcome, subreason) -> claims, summed over repositories."""
    found: dict[tuple[str, str, str, str], int] = {}
    for row in workspace.knowledge.list_ordinary_outcomes():
        key = (row.edge_type, row.target_kind, row.outcome, row.subreason)
        found[key] = found.get(key, 0) + row.claims
    return found


# -- reconciliation ---------------------------------------------------------------------


def test_every_standing_claim_is_ordinary_or_excluded_and_the_residuals_are_measured(
    corpus: Workspace,
) -> None:
    accounting = corpus.knowledge.ordinary_accounting()
    rows = corpus.knowledge.list_ordinary_outcomes()
    excluded = {r.subreason: 0 for r in rows if r.outcome == "excluded"}
    for row in rows:
        if row.outcome == "excluded":
            excluded[row.subreason] += row.claims
    ordinary = sum(r.claims for r in rows if r.outcome != "excluded")

    total = corpus.database.execute("SELECT COUNT(*) FROM unresolved_refs").fetchone()[0]
    uses = corpus.database.execute(
        "SELECT COUNT(*) FROM unresolved_refs WHERE edge_type = 'USES'"
    ).fetchone()[0]
    assert accounting["standing_claims"] == total
    assert accounting["uses_rows"] == uses
    assert accounting["ordinary_considered"] == ordinary
    assert ordinary + sum(excluded.values()) + uses == total
    assert accounting["partition_residual"] == 0
    assert set(excluded) <= EXCLUSIONS
    assert accounting["ordinary_not_linked"] == ordinary - accounting["ordinary_linked"]


def test_the_legacy_scalar_is_kept_whole_and_its_parts_are_recorded_not_assumed(
    corpus: Workspace,
) -> None:
    accounting = corpus.knowledge.ordinary_accounting()
    parts = (
        accounting["legacy_ordinary"]
        + accounting["legacy_graphql_unknown_role"]
        + accounting["legacy_planners"]
        + accounting["legacy_uses_remainder"]
    )
    # the residual is measured against the scalar the pass reports; it is zero here because the
    # scalar holds nothing else, and a pass that counted something new would show it
    assert accounting["legacy_residual"] == accounting["legacy_unresolved"] - parts == 0
    # the two accounts do not describe the same set: the legacy one holds the USES remainder
    # and planner claims, the new one holds linked claims and excludes the planners
    assert accounting["legacy_unresolved"] != accounting["ordinary_not_linked"]


def test_no_claim_is_unexplained_and_every_code_is_from_the_closed_sets(
    corpus: Workspace,
) -> None:
    rows = corpus.knowledge.list_ordinary_outcomes()
    assert rows
    assert {r.subreason for r in rows} <= SUBREASONS | EXCLUSIONS
    assert {r.language for r in rows} <= LANGUAGES
    assert UNCLASSIFIED not in {r.outcome for r in rows}
    for decision in corpus.knowledge.list_link_decisions(family=ORDINARY_FAMILY):
        assert decision.decision in OPEN_OUTCOMES
        assert json.loads(decision.evidence_json)["subreason"] in SUBREASONS


def test_a_module_claim_with_only_an_interned_node_is_not_counted_as_resolved(
    corpus: Workspace,
) -> None:
    """Every import has an edge to a module node; only a source file makes it ``linked``."""
    by = counts(corpus)
    modules = {
        (outcome, sub): n for (edge, kind, outcome, sub), n in by.items() if kind == "module"
    }
    not_linked = sum(n for (outcome, _), n in modules.items() if outcome != LINKED)
    accounting = corpus.knowledge.ordinary_accounting()
    # the interned-only figure is the not-linked module and config claims, and there are edges
    # for each of them to a module node
    config = sum(
        n
        for (_, kind, outcome, _), n in by.items()
        if kind == "config" and outcome not in (LINKED, "excluded")
    )
    assert accounting["interned_node_only"] == not_linked + config
    node_edges = corpus.database.execute(
        """SELECT COUNT(*) FROM edges e JOIN entities d ON d.id = e.dst_id
           WHERE e.type = 'IMPORTS' AND d.type = 'module'"""
    ).fetchone()[0]
    assert node_edges >= not_linked


# -- what each refusal rests on ---------------------------------------------------------


@pytest.mark.parametrize(
    ("edge", "kind", "outcome", "sub", "expected"),
    [
        # a Python import of the standard library, and a declared package in each language
        ("IMPORTS", "module", EXTERNAL, "stdlib", 1),
        ("IMPORTS", "module", EXTERNAL, "declared-package", 2),
        # Node core modules, with and without the node: prefix, and a declared package
        ("IMPORTS", "module", EXTERNAL, "node-builtin", 2),
        ("CALLS", "symbol", EXTERNAL, "stdlib", 1),
        ("CALLS", "symbol", EXTERNAL, "node-builtin", 2),
        ("CALLS", "symbol", EXTERNAL, "declared-package", 2),
    ],
)
def test_external_needs_a_declaration_or_a_fixed_list(
    corpus: Workspace, edge: str, kind: str, outcome: str, sub: str, expected: int
) -> None:
    assert counts(corpus).get((edge, kind, outcome, sub), 0) == expected


def test_a_name_nobody_declares_is_never_external(corpus: Workspace) -> None:
    rows = open_rows(corpus)
    assert rows[("toy_app/main.py", "3", "IMPORTS:module:undeclared_lib")] == (
        UNSUPPORTED,
        "undeclared-absolute-module",
    )
    assert rows[("toy_app/main.py", "22", "CALLS:symbol:undeclared_lib.go")] == (
        UNSUPPORTED,
        "undeclared-absolute-module",
    )
    assert rows[("src/index.ts", "7", "IMPORTS:module:undeclared-pkg")] == (
        UNSUPPORTED,
        "undeclared-bare-specifier",
    )
    assert rows[("src/index.ts", "23", "CALLS:symbol:stray")] == (
        UNSUPPORTED,
        "undeclared-bare-specifier",
    )


def test_proven_workspace_and_cross_repository_packages_are_linked(corpus: Workspace) -> None:
    rows = open_rows(corpus)
    for key in (
        ("src/index.ts", "8", "IMPORTS:module:@toy/codec"),
        ("src/index.ts", "9", "IMPORTS:module:toy-shared"),
        ("src/index.ts", "21", "CALLS:symbol:parse"),
        ("src/index.ts", "22", "CALLS:symbol:shared"),
    ):
        assert key not in rows
    crossing = corpus.database.execute(
        """
        SELECT e.type, e.resolution FROM edges e
        JOIN files f ON f.id = e.file_id JOIN entities d ON d.id = e.dst_id
        JOIN repositories r ON r.id = d.repo_id
        WHERE f.rel_path = 'src/index.ts' AND r.slug = 'toy-shared'
          AND e.parser = 'linker:global' AND e.type IN ('IMPORTS', 'CALLS')
        ORDER BY e.type
        """
    ).fetchall()
    assert [row[0] for row in crossing] == ["CALLS", "IMPORTS"]
    assert all(json.loads(row[1])["crossing"]["package"] == "npm:toy-shared" for row in crossing)


def test_a_configured_alias_import_is_linked(corpus: Workspace) -> None:
    assert ("src/index.ts", "4", "IMPORTS:module:@/lib/x") not in open_rows(corpus)
    by = counts(corpus)
    assert ("IMPORTS", "module", UNSUPPORTED, "configured-alias") not in by
    assert ("IMPORTS", "module", UNSUPPORTED, "workspace-package") not in by
    linked_to = corpus.database.execute(
        """SELECT df.rel_path FROM edges e JOIN entities d ON d.id = e.dst_id
           JOIN files df ON df.id = d.file_id JOIN files sf ON sf.id = e.file_id
           WHERE e.type = 'IMPORTS' AND e.parser = 'linker:global'
             AND sf.rel_path = 'src/index.ts' AND e.line_start IN (4, 8)
           ORDER BY e.line_start"""
    ).fetchall()
    assert [row[0] for row in linked_to] == ["src/lib/x.ts", "packages/codec/index.ts"]


def test_paths_report_where_the_search_stopped(corpus: Workspace) -> None:
    rows = open_rows(corpus)
    assert rows[("src/index.ts", "5", "IMPORTS:module:./missing")][1] == "relative-no-file"
    assert rows[("src/index.ts", "6", "IMPORTS:module:../../../../outside")] == (
        "not-indexed",
        "outside-repository",
    )
    assert rows[("scripts/run.sh", "4", "EXECUTES:script:./scripts/absent.sh")] == (
        "target-not-found",
        "no-indexed-file",
    )
    assert rows[("scripts/run.sh", "5", "EXECUTES:script:../../shared/outside.sh")] == (
        "not-indexed",
        "outside-managed-roots",
    )
    # a command chosen by a variable is not a claim at all, so it has no outcome
    assert not any("TOOL_SCRIPT" in subject for _, _, subject in rows)


def test_a_symbol_that_is_not_found_by_the_ordinary_rules_names_the_form_met(
    corpus: Workspace,
) -> None:
    rows = open_rows(corpus)
    # two declarations of one name in the module the import names
    assert rows[("toy_app/main.py", "25", "CALLS:symbol:dup.run")] == (
        AMBIGUOUS,
        "multiple-declarations-in-module",
    )
    # a direct call through a proven Python re-export is linked by the global pass
    assert ("toy_app/main.py", "26", "CALLS:symbol:reexported") not in rows
    linked_to = corpus.database.execute(
        """SELECT df.rel_path, d.name FROM edges e JOIN entities d ON d.id = e.dst_id
           JOIN files df ON df.id = d.file_id
           WHERE e.type = 'CALLS' AND e.line_start IN (26, 28)
             AND e.file_id = (SELECT id FROM files WHERE rel_path = 'toy_app/main.py')
           ORDER BY e.line_start"""
    ).fetchall()
    assert [(row[0], row[1]) for row in linked_to] == [
        ("toy_app/codec.py", "parse_record"),
        ("toy_app/codec.py", "parse_record"),
    ]
    # a member of a namespace import the module does not declare
    assert rows[("src/index.ts", "17", "CALLS:symbol:ns.nothing")] == (
        UNSUPPORTED,
        "receiver-member",
    )
    # a runtime-selected handler, and a renamed import's call, emit no claim to account for
    assert not any(subject.endswith(":handler") for _, _, subject in rows)
    assert not any(subject.endswith(":renamed") for _, _, subject in rows)


def test_a_typed_imported_value_resolves_only_its_declared_class_method(
    corpus: Workspace,
) -> None:
    linked = corpus.database.execute(
        """
        SELECT s.qualified_name, d.qualified_name
        FROM edges e JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id
        JOIN files sf ON sf.id = s.file_id JOIN files df ON df.id = d.file_id
        WHERE e.type = 'CALLS' AND e.parser = 'linker:global'
          AND sf.rel_path = 'toy_app/main.py'
          AND s.qualified_name IN ('typed_value_use', 'shadowed_value', 'rebound_value')
        """
    ).fetchall()
    assert [tuple(row) for row in linked] == [("typed_value_use", "Worker.run")]

    rows = open_rows(corpus)
    assert any(
        path == "toy_app/main.py" and subject == "CALLS:symbol:dynamic_worker.run"
        for path, _, subject in rows
    )
    assert any(
        path == "toy_app/main.py" and subject == "CALLS:symbol:rebound_worker.run"
        for path, _, subject in rows
    )


def test_what_the_ordinary_rules_link_is_counted_linked(corpus: Workspace) -> None:
    by = counts(corpus)
    assert by[("EXECUTES", "script", LINKED, "resolved")] == 1
    assert by[("SOURCES", "script", LINKED, "resolved")] == 1
    assert by[("REFERENCES", "class", LINKED, "resolved")] == 1
    assert by[("CALLS", "symbol", LINKED, "resolved")] == 9


# -- the account cannot change the graph, the planners or impact --------------------------


def graph_state(workspace: Workspace) -> dict[str, object]:
    """Every fact table but the account itself: the ordinary family's decision rows and the
    two account tables are what an opt-in classification adds, so they are not the graph."""
    snapshot = fact_snapshot(workspace)
    state = {
        key: value
        for key, value in snapshot.items()
        if key not in {"ordinary_outcomes", "ordinary_accounting"}
    }
    decisions = state["link_decisions"]
    assert isinstance(decisions, list)
    state["link_decisions"] = [row for row in decisions if row[0] != ORDINARY_FAMILY]
    return state


def non_ordinary(snapshot: dict[str, object]) -> list[object]:
    rows = snapshot["link_decisions"]
    assert isinstance(rows, list)
    return [row for row in rows if row[0] != ORDINARY_FAMILY]


def test_classifying_differently_changes_no_edge_and_no_other_decision(
    corpus: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Classification runs after resolution has answered; its answer reaches nothing else."""
    before = graph_state(corpus)
    legacy = corpus.knowledge.ordinary_accounting()["legacy_unresolved"]

    def anything(*_args: object, **_kwargs: object) -> Decision:
        return Decision(UNCLASSIFIED, "no-rule")

    monkeypatch.setattr(outcomes_module.Classifier, "classify", anything)
    result = classified_pass(corpus)
    after = graph_state(corpus)

    assert after["edges"] == before["edges"]
    assert result.unresolved == legacy
    assert non_ordinary({"link_decisions": after["link_decisions"]}) == non_ordinary(
        {"link_decisions": before["link_decisions"]}
    )
    assert after["unresolved_refs"] == before["unresolved_refs"]
    # and the account did change, which shows the classifier is what moved
    assert corpus.knowledge.ordinary_accounting()["ordinary_not_linked"] > 0
    assert {r.outcome for r in corpus.knowledge.list_ordinary_outcomes()} >= {UNCLASSIFIED}
    # leave the module corpus as it was found
    monkeypatch.undo()
    classified_pass(corpus)


def test_impact_answers_do_not_list_or_count_the_ordinary_family(corpus: Workspace) -> None:
    """Whole answers, decisions and omissions included, are identical without the rows."""
    seeds = [
        "src/local.ts",
        "src/index.ts",
        "toy_app/codec.py",
        "scripts/common.sh",
        "index.ts",
        "packages/codec/index.ts",
    ]

    reached: list[int] = []

    def answers() -> list[str]:
        service = CapabilityService(corpus)
        found = []
        for seed in seeds:
            response = service.impact(
                ImpactRequest(
                    context=GLOBAL,
                    seed=seed,
                    max_candidates=100,
                    presentation=Presentation.FULL,
                )
            )
            assert ORDINARY_FAMILY not in {d.family for d in response.decisions}
            assert ORDINARY_FAMILY not in response.decisions_omitted
            answer = response.model_dump(mode="json")
            answer.pop("investigation_id", None)  # a fresh handle per answer, not a fact
            found.append(json.dumps(answer, sort_keys=True))
            reached.append(len(response.candidates) + len(response.tests))
        return found

    with_rows = answers()
    assert any(reached), "no seed reached anything, so the comparison would prove nothing"
    present = corpus.database.execute(
        "SELECT COUNT(*) FROM link_decisions WHERE family = ?", (ORDINARY_FAMILY,)
    ).fetchone()[0]
    assert present > 0
    kept = [
        tuple(row)
        for row in corpus.database.execute(
            "SELECT * FROM link_decisions WHERE family = ?", (ORDINARY_FAMILY,)
        ).fetchall()
    ]
    with corpus.database.transaction() as connection:
        connection.execute("DELETE FROM link_decisions WHERE family = ?", (ORDINARY_FAMILY,))
    try:
        without_rows = answers()
    finally:
        with corpus.database.transaction() as connection:
            connection.executemany(
                "INSERT INTO link_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", kept
            )
    assert with_rows == without_rows


def test_the_near_reader_never_returns_the_ordinary_family(corpus: Workspace) -> None:
    keys = [
        row[0]
        for row in corpus.database.execute(
            "SELECT e.key FROM entities e JOIN files f ON f.id = e.file_id"
        ).fetchall()
    ]
    near = corpus.knowledge.link_decisions_near(keys)
    assert ORDINARY_FAMILY not in {family for family, *_ in near}


# -- determinism, bounds, privacy ---------------------------------------------------------


def claim(
    repo_id: int, line: int, key: str, *, kind: EntityType = EntityType.SYMBOL
) -> UnresolvedRefRecord:
    return UnresolvedRefRecord(
        repo_id=repo_id,
        file_id=repo_id,
        src_id=repo_id,
        edge_type=EdgeType.CALLS,
        target_kind=kind,
        target_key=key,
        line_start=line,
        line_end=line,
    )


def file_row(repo_id: int, path: str) -> FileRow:
    return FileRow(
        file_id=repo_id,
        file_entity_id=repo_id,
        repo_id=repo_id,
        repo_name=f"r{repo_id}",
        repo_slug=f"r{repo_id}",
        repo_path=Path(f"/nowhere/r{repo_id}"),
        rel_path=path,
    )


UNSUPPORTED_DECISION = Decision(UNSUPPORTED, "binding-form-untraced", "module-file", "symbols")


def test_the_account_does_not_depend_on_the_order_claims_arrive_in() -> None:
    sites = [
        (claim(1 + i % 2, 1 + i, f"name{i % 7}"), file_row(1 + i % 2, f"f{i % 3}.py"))
        for i in range(60)
    ]
    plans = []
    for ordering in (sites, list(reversed(sites)), sites[::2] + sites[1::2]):
        ledger = OutcomeLedger()
        for site, origin in ordering:
            ledger.refused(site, origin, "python", UNSUPPORTED_DECISION)
        plans.append(ledger.plan())
    assert plans[0] == plans[1] == plans[2]


def test_per_claim_rows_are_capped_per_key_and_every_claim_is_still_counted() -> None:
    ledger = OutcomeLedger()
    total = OPEN_ROWS_PER_KEY * 3
    origin = file_row(1, "big.py")
    for line in range(1, total + 1):
        ledger.refused(claim(1, line, f"name{line}"), origin, "python", UNSUPPORTED_DECISION)
    plan = ledger.plan()
    assert len(plan.decisions) == OPEN_ROWS_PER_KEY
    assert plan.accounting["open_rows_truncated"] == total - OPEN_ROWS_PER_KEY
    assert sum(row.claims for row in plan.rows) == total
    # the rows kept are the first by place, not by arrival
    assert min(d.line_start or 0 for d in plan.decisions) == 1
    assert max(d.line_start or 0 for d in plan.decisions) == OPEN_ROWS_PER_KEY


def test_the_aggregate_is_bounded_by_the_codes_not_by_the_claims() -> None:
    small, large = OutcomeLedger(), OutcomeLedger()
    origin = file_row(1, "a.py")
    for line in range(1, 6):
        small.refused(claim(1, line, f"n{line}"), origin, "python", UNSUPPORTED_DECISION)
    for line in range(1, 5001):
        large.refused(claim(1, line, f"n{line}"), origin, "python", UNSUPPORTED_DECISION)
    assert len(small.plan().rows) == len(large.plan().rows) == 1


def test_a_stored_subject_is_a_plain_name_or_nothing() -> None:
    ledger = OutcomeLedger()
    origin = file_row(1, "a.ts")
    keys = [
        "plain.name",
        "@scope/pkg/sub",
        "call(secret_token_value).chain",
        "tag`template ${password}`",
        "x" * 500,
    ]
    for line, key in enumerate(keys, 1):
        ledger.refused(claim(1, line, key), origin, "node", UNSUPPORTED_DECISION)
    subjects = {d.subject for d in ledger.plan().decisions}
    assert subjects == {
        "CALLS:symbol:plain.name",
        "CALLS:symbol:@scope/pkg/sub",
        "CALLS:symbol:<expression>",
    }


def test_aggregate_rows_and_accounting_carry_no_path_name_or_host(corpus: Workspace) -> None:
    allowed = SUBREASONS | EXCLUSIONS | LANGUAGES | OUTCOMES
    texts: set[str] = set()
    for row in corpus.knowledge.list_ordinary_outcomes():
        texts.update((row.language, row.outcome, row.subreason))
    assert texts <= allowed
    stored = json.dumps(corpus.knowledge.ordinary_accounting())
    for needle in (
        "toy_app",
        "toy-web",
        "undeclared_lib",
        "undeclared-pkg",
        "example.invalid",
        "/",
    ):
        assert needle not in stored
    rows = json.dumps([r.model_dump() for r in corpus.knowledge.list_ordinary_outcomes()])
    for needle in (
        "toy_app",
        "toy-web",
        "undeclared_lib",
        "undeclared-pkg",
        "example.invalid",
        "main.py",
    ):
        assert needle not in rows


def test_two_passes_over_unchanged_facts_store_the_same_account(corpus: Workspace) -> None:
    first = (
        corpus.knowledge.list_ordinary_outcomes(),
        corpus.knowledge.ordinary_accounting(),
        corpus.knowledge.list_link_decisions(family=ORDINARY_FAMILY),
    )
    classified_pass(corpus)
    second = (
        corpus.knowledge.list_ordinary_outcomes(),
        corpus.knowledge.ordinary_accounting(),
        corpus.knowledge.list_link_decisions(family=ORDINARY_FAMILY),
    )
    assert first == second


def test_a_changed_declaration_changes_the_outcome_with_the_next_pass(
    tmp_path: Path,
) -> None:
    """``requests`` is external only while a manifest declares it."""
    from tests.incremental_corpus import edit

    workspace = open_workspace(
        settings=Settings.model_validate({"storage": {"database": str(tmp_path / "s/atlas.db")}}),
        require_root=False,
    )
    try:
        build(workspace, tmp_path, {"toy-py": TOY_PY})
        classified_sync(workspace, tmp_path / "repos")
        assert counts(workspace)[("CALLS", "symbol", EXTERNAL, "declared-package")] == 1
        edit(tmp_path / "repos", "toy-py", "requirements.txt", "")
        classified_sync(workspace, tmp_path / "repos")
        assert ("CALLS", "symbol", EXTERNAL, "declared-package") not in counts(workspace)
        assert (
            counts(workspace)[("CALLS", "symbol", UNSUPPORTED, "undeclared-absolute-module")] == 2
        )
    finally:
        workspace.close()


def fresh_workspace(tmp_path: Path) -> Workspace:
    return open_workspace(
        settings=Settings.model_validate({"storage": {"database": str(tmp_path / "s/atlas.db")}}),
        require_root=False,
    )


def stored_account(workspace: Workspace) -> tuple[object, ...]:
    return (
        workspace.knowledge.list_ordinary_outcomes(),
        workspace.knowledge.ordinary_accounting(),
        workspace.knowledge.list_link_decisions(family=ORDINARY_FAMILY),
    )


def test_a_verbose_sync_reports_the_account_apart_from_the_legacy_line(tmp_path: Path) -> None:
    workspace = fresh_workspace(tmp_path)
    try:
        build(workspace, tmp_path, repos())
        report = sync(
            workspace, workspace.resolver().resolve(tmp_path / "repos"), full=True, classify=True
        )
        accounting = workspace.knowledge.ordinary_accounting()
        assert report.ordinary is not None
        assert report.ordinary.accounting == accounting
        assert report.ordinary.considered == accounting["ordinary_considered"]
        assert report.ordinary.linked == accounting["ordinary_linked"]
        assert report.unresolved_references == accounting["legacy_unresolved"]
        assert sum(report.ordinary.reasons.values()) == accounting["ordinary_not_linked"]
        text = render_sync(report)
        assert f"{report.unresolved_references} references still unresolved" in text
        block = text.split("Ordinary references:")[1]
        assert f"{accounting['ordinary_considered']:,}  considered" in block
        for heading in ("linked", "external", "unsupported", "ambiguous", "other unresolved"):
            assert heading in block
        assert "Why they are not linked:" in block
        assert "external / declared-package" in block
        assert "nothing is unexplained" in block
        # nothing source-specific reaches the report
        for needle in ("toy_app", "toy-web", "undeclared_lib", "undeclared-pkg", "main.py"):
            assert needle not in block
    finally:
        workspace.close()


def test_an_ordinary_sync_does_none_of_the_classification_and_keeps_no_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an ordinary sync must not classify")

    monkeypatch.setattr(outcomes_module.Classifier, "__init__", forbidden)
    monkeypatch.setattr(outcomes_module.OutcomeLedger, "__init__", forbidden)
    workspace = fresh_workspace(tmp_path)
    try:
        build(workspace, tmp_path, repos())
        report = sync(workspace, workspace.resolver().resolve(tmp_path / "repos"), full=True)
        assert report.ordinary is None
        assert stored_account(workspace) == ([], {}, [])
        text = render_sync(report)
        assert "Ordinary references" not in text
        assert f"{report.unresolved_references} references still unresolved" in text
    finally:
        workspace.close()


def test_an_ordinary_pass_changes_no_edge_claim_or_legacy_count(tmp_path: Path) -> None:
    """Asking for the account is the only difference between the two passes."""
    workspace = fresh_workspace(tmp_path)
    try:
        build(workspace, tmp_path, repos())
        root = tmp_path / "repos"
        ordinary = sync(workspace, workspace.resolver().resolve(root), full=True)
        plain = graph_state(workspace)
        asked = sync(workspace, workspace.resolver().resolve(root), classify=True)
        assert graph_state(workspace) == plain
        assert asked.unresolved_references == ordinary.unresolved_references
        assert asked.linked_references == ordinary.linked_references
    finally:
        workspace.close()


def test_a_stored_account_cannot_outlive_the_graph_it_describes(tmp_path: Path) -> None:
    """An ordinary sync clears the account in the commit that replaces the graph."""
    workspace = fresh_workspace(tmp_path)
    try:
        build(workspace, tmp_path, repos())
        root = tmp_path / "repos"
        classified_sync(workspace, root)
        asked = stored_account(workspace)
        assert asked[0] and asked[2]

        sync(workspace, workspace.resolver().resolve(root))
        assert stored_account(workspace) == ([], {}, [])
        assert ORDINARY_FAMILY not in {
            row.family for row in workspace.knowledge.list_link_decisions()
        }

        # and asking again brings back exactly the account it first gave: no second reindex
        classified_sync(workspace, root)
        assert stored_account(workspace) == asked
    finally:
        workspace.close()


def test_a_changed_tree_with_an_ordinary_sync_leaves_no_account_of_the_old_one(
    tmp_path: Path,
) -> None:
    from tests.incremental_corpus import edit

    workspace = fresh_workspace(tmp_path)
    try:
        build(workspace, tmp_path, {"toy-py": TOY_PY})
        classified_sync(workspace, tmp_path / "repos")
        assert counts(workspace)[("CALLS", "symbol", EXTERNAL, "declared-package")] == 1
        edit(tmp_path / "repos", "toy-py", "requirements.txt", "")
        sync(workspace, workspace.resolver().resolve(tmp_path / "repos"))
        # the account of the graph before the edit is gone, not shown beside the new graph
        assert stored_account(workspace) == ([], {}, [])
    finally:
        workspace.close()


def test_a_failed_commit_leaves_the_account_that_matches_the_graph_it_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = fresh_workspace(tmp_path)
    try:
        build(workspace, tmp_path, repos())
        classified_sync(workspace, tmp_path / "repos")
        before = (graph_state(workspace), stored_account(workspace))

        def broken(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("injected")

        monkeypatch.setattr(type(workspace.knowledge), "replace_ordinary_outcomes", broken)
        with pytest.raises(RuntimeError, match="injected"):
            sync(workspace, workspace.resolver().resolve(tmp_path / "repos"))
        monkeypatch.undo()
        assert (graph_state(workspace), stored_account(workspace)) == before
    finally:
        workspace.close()


def test_a_classified_incremental_sync_equals_a_classified_fresh_index(tmp_path: Path) -> None:
    from tests.incremental_corpus import edit

    root = tmp_path / "incremental"
    workspace = fresh_workspace(root)
    fresh = fresh_workspace(tmp_path / "fresh")
    try:
        build(workspace, root, repos())
        classified_sync(workspace, root / "repos")
        edit(
            root / "repos",
            "toy-web",
            "src/index.ts",
            (root / "repos/toy-web/src/index.ts").read_text() + "\nfoo();\n",
        )
        classified_sync(workspace, root / "repos")

        build(fresh, tmp_path / "fresh", repos())
        edit(
            tmp_path / "fresh" / "repos",
            "toy-web",
            "src/index.ts",
            (tmp_path / "fresh/repos/toy-web/src/index.ts").read_text() + "\nfoo();\n",
        )
        classified_sync(fresh, tmp_path / "fresh" / "repos")
        assert stored_account(workspace) == stored_account(fresh)
    finally:
        workspace.close()
        fresh.close()
