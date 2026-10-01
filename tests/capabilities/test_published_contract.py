"""What the published contract has to say for itself (RC audit, RCA-05 and RCA-06).

Two fields on the public surface were technically complete and practically
unreadable.

``tier`` and ``match`` are :class:`enum.IntEnum`, so they cross the wire as bare
integers: ``{"tier": 0, "match": 4}``. The generated JSON Schema said
``"enum": [0, 1, 2, 3, 4, 5, 6, 7]`` and carried the class docstring, which
explains at length what the *ordering* is for and never once says which number is
which member. A client could sort by them and could not name them.

``include_host_paths`` is the one option on the whole surface whose purpose is
disclosure, and its schema entry was ``{"default": false, "type": "boolean"}``.
The reasoning behind the default lived in a ``#:`` source comment, which Pydantic
does not read and no caller ever sees.

Both are fixed by publishing what was already true, and the tests here are mostly
about the ways that could go wrong later:

* the fix is **generated** from the enum members, so a renumbered or added member
  cannot leave a stale hand-written key behind;
* the serialized integers are **unchanged**, because they are V1's ``--json``
  values as well as V2's, and a P3 clarity finding is no reason to break both;
* ``tier`` and ``match`` stay two concepts, and neither is described as a
  confidence, a probability or a score of truth, because neither is one.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.capability_io import RepositoriesRequest, RepositoryView
from repoatlas.models.evidence import (
    MATCH_MEANINGS,
    TIER_MEANINGS,
    EvidenceCandidate,
    EvidenceTier,
    MatchClass,
    value_legend,
)
from repoatlas.models.investigation import LedgerEntry, ProposedEvidence
from repoatlas.models.search import SearchRequest, SearchResponse
from repoatlas.models.sync import SyncRequest
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import open_workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

#: Every model that declares the two fields. A description attached to three of
#: four would be worse than none, because the gap is invisible.
DECLARING = (EvidenceCandidate, LedgerEntry, ProposedEvidence)

#: Vocabulary that would misdescribe either field. A tier is a precedence class
#: and a match class is how the query named something; neither is a measurement
#: of how likely a statement is to be true, and a client told otherwise would
#: weigh evidence by a number that does not mean that.
FORBIDDEN_WORDS = (
    "confidence",
    "probability",
    "likelihood",
    "truth score",
    "certainty",
    "how true",
    "accuracy",
)


def enum_description(model: type[BaseModel], name: str) -> str:
    return str(model.model_json_schema()["$defs"][name]["description"])


# ---------------------------------------------------------------------------
# RCA-05: the mapping is complete, correct, and generated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("enum_type", "meanings"),
    [(EvidenceTier, TIER_MEANINGS), (MatchClass, MATCH_MEANINGS)],
    ids=["EvidenceTier", "MatchClass"],
)
def test_every_member_has_a_published_interpretation(
    enum_type: type[EvidenceTier] | type[MatchClass], meanings: Mapping[Any, str]
) -> None:
    """No member may ship as an integer nobody can name.

    Asserted in both directions: a member with no meaning is the defect, and a
    meaning with no member is a leftover that would publish a value the enum does
    not have.
    """
    assert set(meanings) == set(enum_type), {
        "members without a meaning": sorted(m.name for m in enum_type if m not in meanings),
        "meanings without a member": sorted(
            getattr(m, "name", repr(m)) for m in meanings if m not in set(enum_type)
        ),
    }
    for member, meaning in meanings.items():
        assert meaning.strip(), member


@pytest.mark.parametrize(
    ("enum_type", "meanings"),
    [(EvidenceTier, TIER_MEANINGS), (MatchClass, MATCH_MEANINGS)],
    ids=["EvidenceTier", "MatchClass"],
)
def test_the_published_numbers_are_the_enums_own(
    enum_type: type[EvidenceTier] | type[MatchClass], meanings: Mapping[Any, str]
) -> None:
    """The legend is derived, so this compares it back to the source.

    A hand-maintained table would pass a test that read the same table. This one
    reads ``member.value`` and looks for it in the sentence a client is given.
    """
    legend = value_legend(meanings)
    for member in enum_type:
        assert f"{member.value} = {member.name.lower()}" in legend, (member, legend)
    assert legend.count(";") == len(list(enum_type)) - 1


@pytest.mark.parametrize("name", ["EvidenceTier", "MatchClass"])
def test_the_schema_a_client_reads_carries_the_mapping(name: str) -> None:
    """The legend has to reach the published artifact, not just exist in Python.

    Pydantic emits an enum's docstring as the ``$def`` description, which is what
    an MCP client, a generated schema and a documentation build all read.
    """
    description = enum_description(SearchResponse, name)
    members = EvidenceTier if name == "EvidenceTier" else MatchClass

    assert "Serialized as an integer" in description
    for member in members:
        assert f"{member.value} = {member.name.lower()}" in description, member


def test_the_two_concepts_are_not_collapsed_into_one() -> None:
    """A tier is not a match class, and the descriptions must keep saying so.

    They are separate because two candidates can be equally deterministic and
    still be answers to different questions --- one because the query spelled a
    whole path, one because a basename collided. A client told these were the same
    axis would have no way to tell those apart.
    """
    tier = enum_description(SearchResponse, "EvidenceTier")
    match = enum_description(SearchResponse, "MatchClass")

    assert tier != match
    assert TIER_MEANINGS.keys() != MATCH_MEANINGS.keys()
    assert "Distinct from" in match or "distinct" in match.lower()

    fields = SearchResponse.model_json_schema()["$defs"]["EvidenceItem"]["properties"]
    assert fields["tier"]["description"] != fields["match"]["description"]
    for field in ("tier", "match"):
        assert "Distinct from" in fields[field]["description"], field


@pytest.mark.parametrize("word", FORBIDDEN_WORDS)
def test_neither_field_is_described_as_a_confidence(word: str) -> None:
    """Neither number says how likely anything is to be true.

    RepoAtlas verifies grounding and not entailment, and a `tier` presented as a
    confidence would quietly reintroduce the truth claim the whole capability
    layer refuses to make.
    """
    schema = SearchResponse.model_json_schema()
    published = [
        enum_description(SearchResponse, "EvidenceTier"),
        enum_description(SearchResponse, "MatchClass"),
        schema["$defs"]["EvidenceItem"]["properties"]["tier"]["description"],
        schema["$defs"]["EvidenceItem"]["properties"]["match"]["description"],
        *TIER_MEANINGS.values(),
        *MATCH_MEANINGS.values(),
    ]
    for text in published:
        assert word not in text.lower(), f"{word!r} in: {text[:90]}"


@pytest.mark.parametrize("model", DECLARING, ids=lambda m: m.__name__)
def test_every_model_carrying_the_fields_describes_them(model: type[BaseModel]) -> None:
    """One annotated type, so the wording cannot drift between the four models."""
    properties = model.model_json_schema()["properties"]
    for field in ("tier", "match"):
        assert properties[field].get("description"), (model.__name__, field)


# ---------------------------------------------------------------------------
# RCA-05: and none of it changed a single byte on the wire
# ---------------------------------------------------------------------------


def test_the_serialized_values_are_still_integers() -> None:
    """The clarity fix must not become a serialization change.

    These integers are V1's ``locate --json`` values as much as V2's, so turning
    ``0`` into ``"deterministic"`` would break two contracts to fix a P3 finding
    about documentation.
    """
    candidate = EvidenceCandidate(
        repository="app",
        repository_slug="app",
        path="src/config.py",
        tier=EvidenceTier.DETERMINISTIC,
        match=MatchClass.EXACT_PATH,
        score=1.0,
        description="defines host",
    )
    payload = json.loads(candidate.model_dump_json())

    assert payload["tier"] == 0
    assert payload["match"] == 0
    assert isinstance(payload["tier"], int)
    assert isinstance(payload["match"], int)
    assert not isinstance(payload["tier"], str)


@pytest.mark.parametrize(
    ("enum_type", "expected"),
    [
        (EvidenceTier, {"DETERMINISTIC": 0, "ASSOCIATIVE": 1}),
        (
            MatchClass,
            {
                "EXACT_PATH": 0,
                "EXACT_SUBPATH": 1,
                "EXACT_BASENAME": 2,
                "EXACT_NAME": 3,
                "EXACT_CONTENT": 4,
                "RELATED": 5,
                "LEXICAL": 6,
                "SIMILAR": 7,
            },
        ),
    ],
    ids=["EvidenceTier", "MatchClass"],
)
def test_the_numbers_themselves_are_pinned(
    enum_type: type[EvidenceTier] | type[MatchClass], expected: dict[str, int]
) -> None:
    """Renumbering is a breaking change to every stored and transmitted payload.

    Spelled out rather than derived, deliberately: this is the one place in the
    suite that is *allowed* to duplicate the values, because its job is to notice
    when they move.
    """
    assert {member.name: member.value for member in enum_type} == expected


@pytest.mark.parametrize("name", ["EvidenceTier", "MatchClass"])
def test_the_schema_still_declares_them_as_integers(name: str) -> None:
    definition = SearchResponse.model_json_schema()["$defs"][name]
    members = EvidenceTier if name == "EvidenceTier" else MatchClass

    assert definition["type"] == "integer"
    assert definition["enum"] == [m.value for m in members]


# ---------------------------------------------------------------------------
# RCA-06: a deliberate disclosure, documented as one
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


@pytest.fixture
def service(tmp_path: Path) -> Iterator[CapabilityService]:
    repo = tmp_path / "repos" / "app"
    repo.mkdir(parents=True)
    (repo / "config.py").write_text("HOST = 'localhost'\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b.invalid")
    _git(repo, "config", "user.name", "A")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x")

    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    initialize(workspace, tmp_path / "repos", config_path=tmp_path / "config.yaml")
    built = CapabilityService(workspace)
    try:
        yield built
    finally:
        built.close()


def test_host_paths_are_absent_unless_asked_for(service: CapabilityService) -> None:
    """The default, which is the whole reason the option is defensible."""
    response = service.repositories(RepositoriesRequest(context=GLOBAL))

    assert response.repositories
    for view in response.repositories:
        assert view.host_path is None
        assert json.loads(view.model_dump_json())["host_path"] is None
        assert view.slug


def test_the_default_is_off_in_the_model_itself() -> None:
    """Not merely off in practice: off in the declaration a client reads."""
    assert RepositoriesRequest.model_fields["include_host_paths"].default is False
    assert RepositoriesRequest(context=GLOBAL).include_host_paths is False


def test_an_explicit_request_really_does_disclose_the_path(service: CapabilityService) -> None:
    """Opting in returns the real absolute path, and is not quietly redacted.

    A disclosure option that disclosed nothing would be worse than none: the
    caller would believe it had the information.
    """
    response = service.repositories(RepositoriesRequest(context=GLOBAL, include_host_paths=True))

    assert response.repositories
    for view in response.repositories:
        assert view.host_path is not None
        assert view.host_path.is_absolute()
        assert view.host_path.exists()


def test_the_published_description_calls_the_disclosure_explicit() -> None:
    """The reasoning has to reach the schema, which is where a caller decides.

    It lived in a ``#:`` comment, which Pydantic does not publish. A caller
    choosing whether to set this flag never saw a word of it.
    """
    described = RepositoriesRequest.model_json_schema()["properties"]["include_host_paths"]
    text = described["description"].lower()

    assert described["default"] is False
    assert described["type"] == "boolean"
    assert "private by default" in text
    assert "explicit" in text
    for expected in ("slug", "inspect", "search"):
        assert expected in text, expected


def test_the_response_field_says_when_it_is_populated() -> None:
    described = RepositoryView.model_json_schema()["properties"]["host_path"]

    assert "include_host_paths" in described["description"]
    assert "repository_slug" in described["description"] or "slug" in described["description"]


def test_the_skill_prefers_slugs_and_says_host_paths_are_opt_in() -> None:
    """An agent reads the skill, not the JSON Schema, so both have to agree.

    Checked as content rather than presence: a section that mentioned the flag
    without saying it is off by default would be worse than silence.
    """
    skill = Path("skills/repoatlas-code-trace/SKILL.md").read_text(encoding="utf-8")
    lowered = skill.lower()

    assert "host paths are private by default" in lowered
    assert "include_host_paths" in skill
    assert "slug is the machine identity" in lowered
    assert "do not request host paths for ordinary work" in lowered


# ---------------------------------------------------------------------------
# RCA-06 against RCA-03/08/09/10: asked-for is not the same as leaked
# ---------------------------------------------------------------------------


def test_a_requested_host_path_is_allowed_and_a_leaked_one_is_not(
    service: CapabilityService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The distinction the whole privacy family rests on, pinned in one place.

    Two failure modes this blocks, in opposite directions. A maintainer
    "fixing privacy" could disable the deliberate option, which removes a
    supported capability and fixes nothing. A maintainer could equally cite the
    option as proof that host paths are fine to emit, and start passing exception
    text through. One is requested and scoped to a field whose name says so; the
    other is arbitrary text from something that was never asked.
    """
    from repoatlas.capabilities.errors import capability_error

    disclosed = service.repositories(RepositoriesRequest(context=GLOBAL, include_host_paths=True))
    assert all(view.host_path is not None for view in disclosed.repositories)

    leaked = capability_error(
        OSError("cannot open /home/private-user/RCP_LEAK_SENTINEL_918273.db")
    ).model_dump(mode="json")
    body = json.dumps(leaked)

    assert "RCP_LEAK_SENTINEL_918273" not in body
    assert "/home/private-user" not in body


def test_asking_for_host_paths_does_not_unlock_anything_else(
    service: CapabilityService,
) -> None:
    """The option is scoped to one field of one capability.

    Setting it must not turn into a general "verbose mode" that relaxes other
    projections, which is how a narrow, defensible disclosure becomes a broad one.
    """
    opted_in = service.repositories(RepositoriesRequest(context=GLOBAL, include_host_paths=True))
    default = service.repositories(RepositoriesRequest(context=GLOBAL))

    disclosed = json.loads(opted_in.model_dump_json())
    private = json.loads(default.model_dump_json())
    for view in disclosed["repositories"]:
        view.pop("host_path")
    for view in private["repositories"]:
        view.pop("host_path")

    assert disclosed == private


def test_a_search_response_is_unaffected_by_the_option(service: CapabilityService) -> None:
    """Evidence never carried a host path, and this pins that it still does not."""
    service.sync(SyncRequest(context=GLOBAL, full=True))
    found = service.search(SearchRequest(context=GLOBAL, question="HOST config.py"))

    body = found.model_dump_json()
    assert "host_path" not in body
    for item in found.evidence:
        assert not Path(item.location.split(":")[0]).is_absolute()
