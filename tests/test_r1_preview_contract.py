"""R1 gate G4: the contract of ``include_diagnostics`` on a preview.

What the flag may and may not do to ``http_connection_preview``: with it off every preview of the
golden is byte for byte what it was; with it on nothing a preview decides changes (requirements,
warnings, rows, counts, ``routes_served``, ``can_create``: mutant M17); the response schema keeps
every property it had and adds the one new; the exposure cap is reported, not hidden; and a hint
names only the target's own files. The expectations on single fixtures (cases 12-15, 17b) are in
``test_r1_preview_diagnostics.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from repoatlas.capabilities.errors import InvalidDeploymentLinkError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import DECLARATIONS_SLUG
from repoatlas.graph import link_exposures
from repoatlas.graph.linker import repository_http_with_routes, repository_routes
from repoatlas.models.http_connections import (
    HttpConnectionPreviewRequest,
    HttpConnectionPreviewResponse,
    HttpConnectionsRequest,
)
from repoatlas.services.workspace import Workspace
from tests.r1_golden import GOLDEN, normalise, strip_ids
from tests.r1_provider_corpus import CORPUS_NAMES, GLOBAL, ORDERS_ORIGIN, Corpora, preview
from tests.r1_superseded import SUPERSEDED, problems

SHOP_ORIGIN = "https://shop.example.test"
#: What a preview decides: all of it must be the same with the flag on.
DECIDED = ("requirements", "warnings", "rows", "counts", "routes_served", "can_create")


@pytest.fixture(scope="module")
def corpora(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Corpora]:
    built = Corpora(tmp_path_factory.mktemp("r1-preview-contract"))
    yield built
    built.close()


def golden() -> dict[str, Any]:
    if not GOLDEN.exists():
        pytest.fail(f"{GOLDEN} is missing: it is written once, at 2da633f", pytrace=False)
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def groups(workspace: Workspace) -> dict[str, str]:
    """Origin → worklist group key, for every stated-origin group."""
    listed = CapabilityService(workspace).http_connections(
        HttpConnectionsRequest(context=GLOBAL, include_resolved=True)
    )
    return {g.origin: g.key for g in listed.groups if g.kind == "stated-origin" and g.origin}


def ask(
    workspace: Workspace, group: str, slug: str, *, diagnostics: bool = False
) -> HttpConnectionPreviewResponse:
    return CapabilityService(workspace).http_connection_preview(
        HttpConnectionPreviewRequest(
            context=GLOBAL,
            group=group,
            repository=slug,
            limit=100,
            include_diagnostics=diagnostics,
        )
    )


# -- (i) the flag off changes nothing -----------------------------------------------------------


def _unless_superseded(name: str) -> Any:
    """The corpus, marked ``xfail(strict)`` where F1 and F2 change its previews (the allowlist in
    ``tests.r1_superseded``, checked by the replacement test below)."""
    if (name, "previews") not in SUPERSEDED:
        return name
    return pytest.param(
        name,
        marks=pytest.mark.xfail(
            strict=True, reason="superseded by F1 composed NestJS routes join (previews)"
        ),
    )


@pytest.mark.parametrize("corpus", [_unless_superseded(name) for name in CORPUS_NAMES])
def test_with_the_flag_off_every_golden_preview_is_what_it_was(
    corpora: Corpora, corpus: str
) -> None:
    workspace = corpora.workspace(corpus)
    pinned = golden()["corpora"][corpus]["previews"]
    by_origin = groups(workspace)
    for entry in pinned:
        group = by_origin[entry["origin"]]
        if "refused" in entry["preview"]:
            with pytest.raises(InvalidDeploymentLinkError) as raised:
                ask(workspace, group, entry["repository"])
            assert str(raised.value) == entry["preview"]["refused"]
            continue
        response = ask(workspace, group, entry["repository"])
        written = strip_ids(json.loads(response.model_dump_json()))
        written.pop("context", None)
        assert json.dumps(normalise(written), sort_keys=True) == json.dumps(
            normalise(entry["preview"]), sort_keys=True
        ), (entry["origin"], entry["repository"])
        assert "provider_diagnostics" not in written


@pytest.mark.parametrize("corpus", [n for n in CORPUS_NAMES if (n, "previews") in SUPERSEDED])
def test_with_the_flag_off_a_superseded_preview_differs_from_the_golden_only_as_allowed(
    corpora: Corpora, corpus: str
) -> None:
    """Replaces the exact comparison above for the previews F1 and F2 change: the golden is
    untouched, and ``tests.r1_superseded`` names every entry that may differ and how."""
    workspace = corpora.workspace(corpus)
    pinned = golden()["corpora"][corpus]["previews"]
    by_origin = groups(workspace)
    now: list[Any] = []
    for entry in pinned:
        if "refused" in entry["preview"]:
            now.append(entry)
            continue
        response = ask(workspace, by_origin[entry["origin"]], entry["repository"])
        written = strip_ids(json.loads(response.model_dump_json()))
        written.pop("context", None)
        now.append(
            {
                "origin": entry["origin"],
                "repository": entry["repository"],
                "preview": normalise(written),
            }
        )
    assert problems(corpus, "previews", pinned, now) == []


# -- (ii) the flag on decides nothing more (mutant M17) ----------------------------------------


@pytest.mark.parametrize("corpus", CORPUS_NAMES)
def test_with_the_flag_on_every_golden_preview_decides_what_it_decided(
    corpora: Corpora, corpus: str
) -> None:
    workspace = corpora.workspace(corpus)
    pinned = golden()["corpora"][corpus]["previews"]
    by_origin = groups(workspace)
    seen = 0
    for entry in pinned:
        if "refused" in entry["preview"]:
            continue
        group, slug = by_origin[entry["origin"]], entry["repository"]
        plain = ask(workspace, group, slug)
        diagnosed = ask(workspace, group, slug, diagnostics=True)
        for name in DECIDED:
            assert getattr(diagnosed, name) == getattr(plain, name), (corpus, slug, name)
        assert diagnosed.provider_diagnostics is not None, (corpus, slug)
        assert diagnosed.model_dump(exclude={"provider_diagnostics"}) == plain.model_dump()
        seen += 1
    assert seen == sum(1 for entry in pinned if "refused" not in entry["preview"])


def test_a_refused_preview_is_refused_the_same_with_the_flag_on(corpora: Corpora) -> None:
    py = corpora.workspace("py")
    group = groups(py)[ORDERS_ORIGIN]
    for diagnostics in (False, True):
        with pytest.raises(InvalidDeploymentLinkError, match="not indexed"):
            CapabilityService(py).http_connection_preview(
                HttpConnectionPreviewRequest(
                    context=GLOBAL,
                    group=group,
                    repository="nowhere",
                    include_diagnostics=diagnostics,
                )
            )


# -- the routes of the one-plan helper are the routes the default path reads --------------------


@pytest.mark.parametrize("corpus", CORPUS_NAMES)
def test_the_one_plan_helper_gives_exactly_the_routes_the_default_path_reads(
    corpora: Corpora, corpus: str
) -> None:
    workspace = corpora.workspace(corpus)
    for repository in workspace.repositories.list_all():
        if repository.slug == DECLARATIONS_SLUG or repository.id is None:
            continue
        _, routes = repository_http_with_routes(workspace.knowledge, repository.id)
        assert routes == repository_routes(workspace.knowledge, repository.id), repository.slug


# -- (iii) the response schema -----------------------------------------------------------------


def test_the_preview_schema_keeps_every_property_it_had_and_adds_the_one() -> None:
    schema = HttpConnectionPreviewResponse.model_json_schema(mode="serialization")
    properties = schema["properties"]
    assert {
        "context",
        "group",
        "origin",
        "url",
        "repository",
        "scope",
        "routes_served",
        "counts",
        "rows",
        "has_more",
        "requirements",
        "warnings",
        "duplicate_of",
        "conflicts_with",
        "can_create",
        "basis",
    } <= set(properties)
    assert "provider_diagnostics" in properties
    assert "PreviewProviderDiagnostics" in schema["$defs"]
    assert "UnpublishedMatch" in schema["$defs"]
    assert "ProviderLifecycle" in schema["$defs"]


def test_the_request_schema_adds_only_the_flag() -> None:
    properties = HttpConnectionPreviewRequest.model_json_schema()["properties"]
    assert "include_diagnostics" in properties
    assert properties["include_diagnostics"]["default"] is False


# -- (iv) the exposure cap is reported ---------------------------------------------------------


def test_a_link_that_reached_the_exposure_cap_says_so(
    corpora: Corpora, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``shop`` serves two routes with lines: both are exposable, unless the cap says one."""
    workspace = corpora.workspace("connections")
    shop = next(r.slug for r in workspace.repositories.list_all() if r.name == "shop")
    group = groups(workspace)[SHOP_ORIGIN]
    whole = ask(workspace, group, shop, diagnostics=True).provider_diagnostics
    assert whole is not None
    assert (whole.routes_exposable, whole.exposures_truncated) == (2, False)
    monkeypatch.setattr(link_exposures, "MAX_LINK_EXPOSURES", 1)
    cut = ask(workspace, group, shop, diagnostics=True)
    assert cut.provider_diagnostics is not None
    assert cut.provider_diagnostics.exposures_truncated is True
    assert cut.provider_diagnostics.routes_exposable == 1


# -- (v) a hint names the target's own files ---------------------------------------------------


@pytest.mark.parametrize(("name", "directory"), [("twinprov", "tw/"), ("pyprov", "svc/")])
def test_a_hint_lists_only_files_of_the_repository_it_was_asked_of(
    corpora: Corpora, name: str, directory: str
) -> None:
    result = preview(corpora.workspace("py"), ORDERS_ORIGIN, name, include_diagnostics=True)
    hints = result.provider_diagnostics
    assert hints is not None and hints.unpublished_matches
    assert all((match.path or "").startswith(directory) for match in hints.unpublished_matches)
    assert hints.unpublished_match_count >= len(hints.unpublished_matches)
    assert hints.basis == "hints, never edges or decisions"
