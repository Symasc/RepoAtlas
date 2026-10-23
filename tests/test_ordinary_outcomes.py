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
from repoatlas.rendering.text import render_sync
from repoatlas.services.workspace import Workspace, open_workspace
from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord
from tests.conftest import fact_snapshot
from tests.impact_corpus import build
from tests.ordinary_corpus import TOY_PY, repos

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    root = tmp_path_factory.mktemp("ordinary")
    workspace = open_workspace(
        settings=Settings.model_validate({"storage": {"database": str(root / "state/atlas.db")}}),
        require_root=False,
    )
    yield build(workspace, root, repos())
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


def test_a_package_a_managed_repository_provides_is_not_external(corpus: Workspace) -> None:
    rows = open_rows(corpus)
    # declared by toy-web and provided by toy-shared: the ordinary rules never cross
    # repositories, and that is a different statement from "outside the index"
    assert rows[("src/index.ts", "9", "IMPORTS:module:toy-shared")] == (
        UNSUPPORTED,
        "cross-repository-provider",
    )
    assert rows[("src/index.ts", "22", "CALLS:symbol:shared")][1] == "cross-repository-provider"
    # a member of the repository's own workspace
    assert rows[("src/index.ts", "8", "IMPORTS:module:@toy/codec")][1] == "workspace-package"
    assert rows[("src/index.ts", "21", "CALLS:symbol:parse")][1] == "workspace-package"


def test_a_configured_alias_is_named_not_called_missing(corpus: Workspace) -> None:
    """The ordinary lookup does not read tsconfig ``paths``; saying so is the whole claim."""
    assert open_rows(corpus)[("src/index.ts", "4", "IMPORTS:module:@/lib/x")] == (
        UNSUPPORTED,
        "configured-alias",
    )


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
    # a name the module re-exports under another name: not traced by the ordinary rules
    assert rows[("toy_app/main.py", "26", "CALLS:symbol:reexported")] == (
        UNSUPPORTED,
        "binding-form-untraced",
    )
    # a member of a namespace import the module does not declare
    assert rows[("src/index.ts", "17", "CALLS:symbol:ns.nothing")] == (
        UNSUPPORTED,
        "receiver-member",
    )
    # a runtime-selected handler, and a renamed import's call, emit no claim to account for
    assert not any(subject.endswith(":handler") for _, _, subject in rows)
    assert not any(subject.endswith(":renamed") for _, _, subject in rows)


def test_what_the_ordinary_rules_link_is_counted_linked(corpus: Workspace) -> None:
    by = counts(corpus)
    assert by[("EXECUTES", "script", LINKED, "resolved")] == 1
    assert by[("SOURCES", "script", LINKED, "resolved")] == 1
    assert by[("REFERENCES", "class", LINKED, "resolved")] == 1
    assert by[("CALLS", "symbol", LINKED, "resolved")] == 4


# -- the account cannot change the graph, the planners or impact --------------------------


def graph_state(workspace: Workspace) -> dict[str, object]:
    snapshot = fact_snapshot(workspace)
    return {
        key: value
        for key, value in snapshot.items()
        if key not in {"ordinary_outcomes", "ordinary_accounting"}
    }


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
    from repoatlas.graph.linker import ReferenceLinker

    result = ReferenceLinker(corpus.knowledge, corpus.dependencies).link_global()
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
    ReferenceLinker(corpus.knowledge, corpus.dependencies).link_global()


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
    from repoatlas.graph.linker import ReferenceLinker

    first = (
        corpus.knowledge.list_ordinary_outcomes(),
        corpus.knowledge.ordinary_accounting(),
        corpus.knowledge.list_link_decisions(family=ORDINARY_FAMILY),
    )
    ReferenceLinker(corpus.knowledge, corpus.dependencies).link_global()
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
    from repoatlas.services.sync_service import sync
    from tests.incremental_corpus import edit

    workspace = open_workspace(
        settings=Settings.model_validate({"storage": {"database": str(tmp_path / "s/atlas.db")}}),
        require_root=False,
    )
    try:
        build(workspace, tmp_path, {"toy-py": TOY_PY})
        assert counts(workspace)[("CALLS", "symbol", EXTERNAL, "declared-package")] == 1
        edit(tmp_path / "repos", "toy-py", "requirements.txt", "")
        sync(workspace, workspace.resolver().resolve(tmp_path / "repos"))
        assert ("CALLS", "symbol", EXTERNAL, "declared-package") not in counts(workspace)
        assert (
            counts(workspace)[("CALLS", "symbol", UNSUPPORTED, "undeclared-absolute-module")] == 2
        )
    finally:
        workspace.close()


def test_the_sync_report_states_the_account_apart_from_the_legacy_line(
    tmp_path: Path,
) -> None:
    from repoatlas.services.sync_service import sync

    workspace = open_workspace(
        settings=Settings.model_validate({"storage": {"database": str(tmp_path / "s/atlas.db")}}),
        require_root=False,
    )
    try:
        build(workspace, tmp_path, repos())
        report = sync(workspace, workspace.resolver().resolve(tmp_path / "repos"), full=True)
        accounting = workspace.knowledge.ordinary_accounting()
        assert report.ordinary is not None
        assert report.ordinary.accounting == accounting
        assert report.ordinary.considered == accounting["ordinary_considered"]
        assert report.ordinary.linked == accounting["ordinary_linked"]
        assert report.unresolved_references == accounting["legacy_unresolved"]
        text = render_sync(report)
        assert f"{report.unresolved_references} references still unresolved" in text
        assert f"{accounting['ordinary_considered']} ordinary references considered" in text
        assert "counted apart from the unresolved total above" in text
        # nothing source-specific reaches the report
        assert "toy_app" not in text.split("ordinary references considered")[1]
    finally:
        workspace.close()
