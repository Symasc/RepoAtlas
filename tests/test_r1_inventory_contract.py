"""R1 gate G3: the inventory contract of ``include_diagnostics``.

What the flag may and may not do to ``http_operations``: with it off the answer is the golden
byte for byte (P8, mutant M18); it is refused where it cannot apply; a cursor keeps the question
it was issued for (M19); the unpublished cap says how many it cut (M13); and the rows it adds
obey the invariants of the lifecycle (M1, M2, M4, M17). The expectations on single fixtures are
in ``test_r1_provider_diagnostics.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import ValidationError
from repoatlas.capabilities import http_inventory
from repoatlas.capabilities.errors import InvalidCursorError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import DECLARATIONS_SLUG
from repoatlas.graph.linker import GLOBAL_PARSER, plan_repository_http
from repoatlas.models.http_inventory import (
    CrossingStatus,
    HttpOperation,
    HttpOperationsRequest,
    HttpOperationsResponse,
    OperationStatus,
)
from repoatlas.services.workspace import Workspace
from tests.r1_golden import GOLDEN, normalise, operations
from tests.r1_provider_corpus import (
    CORPUS_NAMES,
    GLOBAL,
    Corpora,
    inventory,
    providers,
    repository_context,
)
from tests.r1_superseded import SUPERSEDED, problems

PUBLISHED = {OperationStatus.CALLED, OperationStatus.NO_INDEXED_CALLER}


@pytest.fixture(scope="module")
def corpora(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Corpora]:
    built = Corpora(tmp_path_factory.mktemp("r1-inventory-contract"))
    yield built
    built.close()


def names(workspace: Workspace) -> list[str]:
    return [r.name for r in workspace.repositories.list_all() if r.slug != DECLARATIONS_SLUG]


def canonical(value: Any) -> str:
    return json.dumps(normalise(value), sort_keys=True, indent=1)


# -- the response schema keeps what it described ------------------------------------------------


def test_the_coverage_schema_keeps_every_field_it_had() -> None:
    """A wrap serializer on a model of the response makes its schema untyped; the coverage must
    stay a typed object, with the lifecycle beside the fields it always had."""
    schema = HttpOperationsResponse.model_json_schema(mode="serialization")
    coverage = schema["$defs"]["OperationCoverage"]["properties"]
    assert {
        "files",
        "unresolved",
        "ambiguous",
        "declared",
        "unknown_paths",
        "limitations",
    } <= set(coverage)
    assert "provider_lifecycle" in coverage
    assert "CoverageLimitation" in schema["$defs"] and "ProviderLifecycle" in schema["$defs"]


# -- P8 (mutant M18): the flag off changes nothing ----------------------------------------------


#: The corpora F1 and F2 leave alone are compared exactly; those whose inventory they change
#: (``tests.r1_superseded``) are compared by the allowlist test below.
@pytest.mark.parametrize("corpus", [n for n in CORPUS_NAMES if (n, "operations") not in SUPERSEDED])
def test_p8_with_the_flag_off_every_repository_answers_as_the_golden_does(
    corpora: Corpora, corpus: str
) -> None:
    if not GOLDEN.exists():
        pytest.fail(f"{GOLDEN} is missing: it is written once, at 2da633f", pytrace=False)
    pinned = json.loads(GOLDEN.read_text(encoding="utf-8"))["corpora"][corpus]["operations"]
    now = operations(corpora.workspace(corpus))
    assert sorted(now) == sorted(pinned)
    for slug in now:
        assert canonical(now[slug]) == canonical(pinned[slug]), slug


@pytest.mark.parametrize("corpus", [n for n in CORPUS_NAMES if (n, "operations") in SUPERSEDED])
def test_p8_with_the_flag_off_a_superseded_inventory_differs_only_as_allowed(
    corpora: Corpora, corpus: str
) -> None:
    """Replaces the exact comparison above where F1 and F2 change an inventory: the golden is
    untouched and ``tests.r1_superseded`` names the one repository that may differ, and how."""
    pinned = json.loads(GOLDEN.read_text(encoding="utf-8"))["corpora"][corpus]["operations"]
    now = normalise(operations(corpora.workspace(corpus)))
    assert problems(corpus, "operations", pinned, now) == []


@pytest.mark.parametrize("corpus", CORPUS_NAMES)
def test_the_flag_off_writes_no_diagnostic_key_anywhere(corpora: Corpora, corpus: str) -> None:
    workspace = corpora.workspace(corpus)
    for name in names(workspace):
        rows, first = inventory(workspace, name)
        text = first.model_dump_json() + "".join(row.model_dump_json() for row in rows)
        for key in ("diagnostic", "declared_match_count", "provider_lifecycle", "unpublished"):
            assert key not in text, (name, key)


# -- the request is refused where the flag cannot apply -----------------------------------------


def test_the_flag_is_refused_with_discover(corpora: Corpora) -> None:
    with pytest.raises(ValidationError, match="discover"):
        HttpOperationsRequest(
            context=GLOBAL,
            discover=True,
            crossing=(CrossingStatus.SERVICE_UNMATCHED,),
            include_diagnostics=True,
        )


def test_the_flag_is_refused_with_a_decision_ref(corpora: Corpora) -> None:
    py = corpora.workspace("py")
    with pytest.raises(ValidationError, match="decision_ref"):
        HttpOperationsRequest(
            context=repository_context(py, "pyprov"),
            decision_ref="hcd1-" + "0" * 20,
            include_diagnostics=True,
        )


def test_the_unpublished_status_without_the_flag_is_answered_and_says_none_was_asked_for(
    corpora: Corpora,
) -> None:
    """Amendment A3: not a refusal. The answer lists no unpublished row and says why."""
    py = corpora.workspace("py")
    code = "unpublished_needs_diagnostics"

    def noted(**fields: Any) -> tuple[list[HttpOperation], list[Any]]:
        rows, first = inventory(py, "pyprov", **fields)
        return rows, [item for item in first.coverage.limitations if item.code == code]

    rows, notes = noted(status=(OperationStatus.UNPUBLISHED,))
    assert [row for row in rows if row.status is OperationStatus.UNPUBLISHED] == []
    [note] = notes
    assert note.count is None and "include_diagnostics" in note.note
    # with the flag the rows are listed and there is nothing to say
    listed, with_flag = noted(status=(OperationStatus.UNPUBLISHED,), include_diagnostics=True)
    assert {row.status for row in listed} == {OperationStatus.UNPUBLISHED}
    assert with_flag == []
    # a request that does not name the status gets no such note, with or without the flag
    assert noted()[1] == []
    assert noted(status=(OperationStatus.DECLARED,))[1] == []
    assert noted(include_diagnostics=True)[1] == []


def test_the_unpublished_filter_selects_only_those_rows(corpora: Corpora) -> None:
    py = corpora.workspace("py")
    rows, _ = inventory(
        py, "pyprov", include_diagnostics=True, status=(OperationStatus.UNPUBLISHED,)
    )
    assert {row.path for row in rows} == {"svc/reports.py", "svc/audit.py", "svc/app2.py"}
    assert {row.status for row in rows} == {OperationStatus.UNPUBLISHED}


# -- a cursor keeps its question (mutant M19) ---------------------------------------------------


def test_a_cursor_issued_with_the_flag_is_refused_without_it(corpora: Corpora) -> None:
    py = corpora.workspace("py")
    service = CapabilityService(py)
    context = repository_context(py, "pyprov")
    first = service.http_operations(
        HttpOperationsRequest(context=context, limit=2, include_diagnostics=True)
    )
    assert first.next_cursor is not None
    with pytest.raises(InvalidCursorError):
        service.http_operations(
            HttpOperationsRequest(context=context, limit=2, cursor=first.next_cursor)
        )
    # and the same cursor still serves the question it was issued for
    again = service.http_operations(
        HttpOperationsRequest(
            context=context, limit=2, cursor=first.next_cursor, include_diagnostics=True
        )
    )
    assert again.operations


def test_a_cursor_issued_without_the_flag_is_refused_with_it(corpora: Corpora) -> None:
    py = corpora.workspace("py")
    service = CapabilityService(py)
    context = repository_context(py, "pyprov")
    first = service.http_operations(HttpOperationsRequest(context=context, limit=2))
    assert first.next_cursor is not None
    with pytest.raises(InvalidCursorError):
        service.http_operations(
            HttpOperationsRequest(
                context=context, limit=2, cursor=first.next_cursor, include_diagnostics=True
            )
        )


# -- the cap (mutant M13) ---------------------------------------------------------------------


def test_m13_the_cap_on_unpublished_rows_counts_what_it_cuts(
    corpora: Corpora, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(http_inventory, "MAX_UNPUBLISHED_ROWS", 1)
    rows, first = inventory(corpora.workspace("py"), "pyprov", include_diagnostics=True)
    kept = [row for row in providers(rows) if row.status is OperationStatus.UNPUBLISHED]
    lifecycle = first.coverage.provider_lifecycle
    assert lifecycle is not None
    assert len(kept) == 1
    assert lifecycle.unpublished == 1
    assert lifecycle.unpublished_omitted == 2
    assert lifecycle.truncated is True
    # nothing is dropped from the count of what was recognised
    assert lifecycle.recognized == 8


def test_the_cap_is_a_constant_of_the_inventory_and_nothing_is_cut_below_it(
    corpora: Corpora,
) -> None:
    assert http_inventory.MAX_UNPUBLISHED_ROWS == 1000
    _, first = inventory(corpora.workspace("py"), "pyprov", include_diagnostics=True)
    lifecycle = first.coverage.provider_lifecycle
    assert lifecycle is not None
    assert (lifecycle.unpublished, lifecycle.unpublished_omitted) == (3, 0)
    assert lifecycle.truncated is False


# -- what every row obeys, in every corpus (mutants M1, M2, M4) ---------------------------------


def served_routes(workspace: Workspace, repository: Any) -> set[tuple[str, str]]:
    """(entity key, ``METHOD route``) of every route the plan or a file serves."""
    knowledge = workspace.knowledge
    planned = plan_repository_http(knowledge, repository.id)
    servers = knowledge.list_route_servers(exclude_parser=GLOBAL_PARSER, repo_id=repository.id)
    found = {(route.src_id, route.body) for route in planned.plan.code_routes}
    found |= {(row.src_id, row.body) for row in servers}
    keys = knowledge.keys_for_entities(sorted({src for src, _ in found}))
    return {(keys[src].key, body) for src, body in found if src in keys}


@pytest.mark.parametrize("corpus", CORPUS_NAMES)
def test_every_row_of_every_corpus_obeys_the_lifecycle(corpora: Corpora, corpus: str) -> None:
    workspace = corpora.workspace(corpus)
    for repository in workspace.repositories.list_all():
        if repository.slug == DECLARATIONS_SLUG:
            continue
        rows, first = inventory(workspace, repository.name, include_diagnostics=True)
        served = served_routes(workspace, repository)
        recognised = providers(rows)
        for row in recognised:
            diagnostic = row.diagnostic
            assert diagnostic is not None, (repository.name, row.route)
            if not diagnostic.published:
                assert row.status in {OperationStatus.DECLARED, OperationStatus.UNPUBLISHED}
                assert row.detail and diagnostic.code and diagnostic.category is not None
                assert diagnostic.join_eligible is False
            else:
                assert row.status in PUBLISHED
                assert diagnostic.category is None and diagnostic.code == ""
                if diagnostic.join_eligible:
                    assert (row.entity, f"{row.method} {row.route}") in served, (
                        repository.name,
                        row.route,
                    )
        for row in rows:
            if row.role == "caller":
                assert row.diagnostic is None
        lifecycle = first.coverage.provider_lifecycle
        assert lifecycle is not None
        assert lifecycle.published + lifecycle.declared + lifecycle.unpublished == len(recognised)
        assert lifecycle.join_eligible == sum(
            1 for row in recognised if row.diagnostic and row.diagnostic.join_eligible
        )


def written(row: HttpOperation) -> dict[str, Any]:
    return json.loads(row.model_dump_json())


@pytest.mark.parametrize("corpus", CORPUS_NAMES)
def test_the_rows_without_the_flag_are_the_rows_with_it_but_for_the_opt_in_fields(
    corpora: Corpora, corpus: str
) -> None:
    workspace = corpora.workspace(corpus)
    added = {"diagnostic", "declared_match_count"}
    for name in names(workspace):
        plain, _ = inventory(workspace, name)
        both, _ = inventory(workspace, name, include_diagnostics=True)
        kept = [row for row in both if row.status is not OperationStatus.UNPUBLISHED]
        assert [{k: v for k, v in written(r).items() if k not in added} for r in kept] == [
            written(r) for r in plain
        ], name


# -- repository attribution ---------------------------------------------------------------------


def test_one_file_is_published_in_the_repository_that_mounts_it_and_declared_in_the_other(
    corpora: Corpora,
) -> None:
    unmounted, _ = inventory(corpora.workspace("py"), "pyprov", include_diagnostics=True)
    mounted, _ = inventory(corpora.workspace("py2"), "pyprov2", include_diagnostics=True)
    [declared] = [r for r in providers(unmounted) if r.path == "svc/orders.py"]
    [published] = [r for r in providers(mounted) if r.path == "svc/orders.py"]
    assert declared.status is OperationStatus.DECLARED
    assert published.status in PUBLISHED
    assert declared.diagnostic is not None and published.diagnostic is not None
    assert declared.diagnostic.published is False and published.diagnostic.published is True
    # each inventory names its own repository's entities and no other's
    assert all(r.entity and r.entity.startswith("sym:pyprov:") for r in providers(unmounted))
    assert all(r.entity and r.entity.startswith("sym:pyprov2:") for r in providers(mounted))
    assert [r.route for r in providers(mounted)] == ["/orders/{oid}"]
