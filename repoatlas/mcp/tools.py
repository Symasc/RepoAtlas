"""The eleven capabilities, as tool declarations. No repository intelligence.

Every entry here is four facts: a protocol name, a description an agent reads
before choosing, the V2 request model that validates its arguments, and the
:class:`~repoatlas.capabilities.service.CapabilityService` method it invokes.
There is no fifth field, because a fifth field is where product logic would
start: a default the model does not carry, a rewrite of one argument into
another, a second call to make when the first returns nothing.

The request model is the schema. It is not copied into a parallel declaration
here and it is not summarised — ``model_json_schema()`` is what the protocol
advertises, so an enum, a bound or a default can only ever be changed in one
place, and a caller that satisfies the advertised schema is a caller the model
accepts.

Descriptions are part of the agent contract, not decoration. An agent chooses a
tool by reading one, so a description that overstates what a capability proves
is a correctness defect in exactly the way a wrong return value is. Two of them
are pinned by tests against a specific claim they must keep making:
:data:`VERIFY_GROUNDING` must go on saying it does not verify truth, and
:data:`SYNC` must go on saying it updates the index rather than the source.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from repoatlas.models.capability_io import RepositoriesRequest, StatusRequest
from repoatlas.models.dependency_inventory import DependenciesRequest
from repoatlas.models.deployment_targets import DeploymentTargetsRequest
from repoatlas.models.http_inventory import HttpOperationsRequest
from repoatlas.models.impact import ImpactRequest
from repoatlas.models.inspection import InspectRequest
from repoatlas.models.relationships import RelationshipsRequest
from repoatlas.models.search import SearchRequest
from repoatlas.models.sync import SyncRequest
from repoatlas.models.verification import VerifyGroundingRequest

#: Prefix for every tool this server exposes. Vendor-neutral on purpose: a tool
#: named after the agent that happens to call it would make the protocol surface
#: a client's business rather than RepoAtlas's.
NAMESPACE = "repoatlas"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One protocol name bound to one capability, and nothing in between."""

    name: str
    title: str
    description: str
    #: The V2 request model. The single source of truth for this tool's schema
    #: and the only validator its arguments meet.
    request: type[BaseModel]
    #: Attribute name on :class:`CapabilityService`. Looked up rather than bound
    #: to a function, so this module never holds a callable that could acquire a
    #: wrapper with behaviour of its own.
    capability: str
    #: Whether the capability may change the index. True for ten of eleven;
    #: ``sync`` is the exception and is the whole of Slice I.
    read_only: bool


REPOSITORIES = ToolSpec(
    name=f"{NAMESPACE}_repositories",
    title="List managed repositories",
    description=(
        "List the repositories RepoAtlas manages in the requested scope, with the "
        "freshness of what it has indexed for each. Reads the local index only: it "
        "indexes nothing, changes nothing, and discovers no repository that is not "
        "already registered."
    ),
    request=RepositoriesRequest,
    capability="repositories",
    read_only=True,
)

STATUS = ToolSpec(
    name=f"{NAMESPACE}_status",
    title="Report index freshness",
    description=(
        "Report how the RepoAtlas index stands against the local working trees in "
        "the requested scope, including which repositories have drifted since they "
        "were last indexed. Reads only; it never updates the index."
    ),
    request=StatusRequest,
    capability="status",
    read_only=True,
)

SEARCH = ToolSpec(
    name=f"{NAMESPACE}_search",
    title="Retrieve indexed evidence",
    description=(
        "Retrieve deterministic indexed repository evidence for a question, and "
        "record every supplied span in an investigation so that it can be cited "
        "afterwards. Returns source text already admitted to the index; it consults "
        "no model, reaches no network, and invents nothing. Omit investigation_id to "
        "begin an investigation; echo the one that comes back to continue it. An item "
        "with declaration true is the declaration RepoAtlas recorded, not source, and "
        "has no line range; to read that definition's source with citable lines, pass "
        "its entity.key to repoatlas_inspect as target.entity_key. A file item with "
        "empty text is inspected the same way. terms says, for each identifier or path "
        "the question names, what each exact lookup established in the scope searched: "
        "checks maps text, file_path and entity_name (entity_key for an HTTP route) to "
        "match, no_match, incomplete or not_checked. state is not_found only when every "
        "relevant lookup ran to completion and found nothing, and not_established when "
        "one did not run or did not finish; not_found describes that indexed scope, not "
        "the whole repository. Set within_path to search one directory or file of the "
        "repository, and presentation to compact to receive the same evidence with "
        "repeated metadata written once. Set source_preview true to receive bounded "
        "indexed source around matching lines, with omitted spans made explicit."
    ),
    request=SearchRequest,
    capability="search",
    read_only=True,
)

INSPECT = ToolSpec(
    name=f"{NAMESPACE}_inspect",
    title="Inspect indexed source",
    description=(
        "Progressively inspect source that RepoAtlas already admitted to its index, "
        "and record what is handed over in an investigation. A path with no indexed "
        "file is refused rather than read from disk, so files the admission policy "
        "excludes stay excluded. Target an entity key from search to receive that "
        "definition's whole recorded span plus context_lines on each side, as citable "
        "lines; target a repository-relative path you were given or found, such as a "
        "document like docs/ARCHITECTURE.md, to read that file."
    ),
    request=InspectRequest,
    capability="inspect",
    read_only=True,
)

RELATIONSHIPS = ToolSpec(
    name=f"{NAMESPACE}_relationships",
    title="Walk indexed relationships",
    description=(
        "Return the deterministic indexed graph relationships of one exact entity "
        "identity. The target is a natural entity key, used verbatim: there is no "
        "name search here and no fuzzy fallback, and a key that resolves to nothing "
        "is refused rather than approximated. Entity keys come back from search."
    ),
    request=RelationshipsRequest,
    capability="relationships",
    read_only=True,
)

IMPACT = ToolSpec(
    name=f"{NAMESPACE}_impact",
    title="Trace change impact",
    description=(
        "Given what changes (the seed), return what may be affected, ranked. Each "
        "candidate carries the one indexed step joining it to its predecessor (the edge, "
        "and the file and lines to read) and its whole path as a chain. Deterministic and "
        "lean: nothing is scored, recorded or quoted, so read the lines a step names to "
        "decide. Tests, hubs "
        "(dependents counted, not listed), importers that cannot be narrowed to a "
        "declaration, and package-level dependents (coarse: a unit or repository that "
        "declares the package) are separate sections, and omitted counts what was left "
        "out. A seed that names more than one thing lists its candidates and follows "
        "nothing."
    ),
    request=ImpactRequest,
    capability="impact",
    read_only=True,
)

DEPENDENCIES = ToolSpec(
    name=f"{NAMESPACE}_dependencies",
    title="Read indexed dependencies",
    description=(
        "Return deterministic dependency observations from the local index: what "
        "each repository in scope declares, what it resolved to, and what it "
        "provides. Reads manifests and lockfiles as they were indexed; it contacts "
        "no package registry and resolves nothing itself."
    ),
    request=DependenciesRequest,
    capability="dependencies",
    read_only=True,
)

HTTP_OPERATIONS = ToolSpec(
    name=f"{NAMESPACE}_http_operations",
    title="Discover indexed HTTP operations",
    description=(
        "List the HTTP operations RepoAtlas statically indexed in one repository: the "
        "requests its source makes and the routes it serves or declares, each with its "
        "location and a status. Statuses describe indexed facts in that repository "
        "only: no_indexed_handler and no_indexed_caller are not proof that no "
        "implementation or caller exists, and other repositories, unsupported syntax, "
        "unproven mounts and unrecorded constructs are reported under coverage. A "
        "route match shows route compatibility only, never service configuration or "
        "data ownership. A caller's crossings are the workspace join's stored "
        "decisions, each with the source-backed chain from its origin to a deployment "
        "and a handler, or the hop that failed; filter them with crossing. Another "
        "repository's part of a chain is withheld unless cross_repository is true. "
        "Reads the local index; paged, with optional evidence. For workspace "
        "discovery, use global scope, discover true and a non-root route_prefix. Rows "
        "retain source repository identity and local statuses; compatible candidates "
        "do not establish runtime dependencies."
    ),
    request=HttpOperationsRequest,
    capability="http_operations",
    read_only=True,
)

DEPLOYMENT_TARGETS = ToolSpec(
    name=f"{NAMESPACE}_deployment_targets",
    title="List deployment targets",
    description=(
        "List where one repository's source says it is reached: each Kubernetes Ingress "
        "host and path (from a manifest or a Helm chart template) and each OpenAPI "
        "server URL, plus the deployment links the user recorded. Every target keeps "
        "four things apart: the pattern as configured, the conditions under which a "
        "release applies (branch, environment, pull request, variables), each "
        "resolution with the basis of every variable it filled (chart defaults, a "
        "release's values, the request's context, a link), and verification, which is "
        "unverified unless the user confirmed it. A value nothing supplies stays a named "
        "hole, never a guess. A target another repository states for this one's code "
        "is marked `runs`. Reads the local index and the stored links; it reaches no "
        "network and never checks that a URL answers. An OAuth callback or redirect URL "
        "is not a deployment target and is not listed. From the next sync, a link also "
        "joins requests the source leaves unlinked, its hops `declared`."
    ),
    request=DeploymentTargetsRequest,
    capability="deployment_targets",
    read_only=True,
)

VERIFY_GROUNDING = ToolSpec(
    name=f"{NAMESPACE}_verify_grounding",
    title="Verify citations against supplied evidence",
    description=(
        "Verify that an answer's citations refer to evidence supplied in the named "
        "investigation and still valid against the indexed snapshot. This checks "
        "provenance only: it does not verify truth or semantic entailment, and a "
        "statement that is false about the lines it cites still verifies as "
        "grounded. A verdict of ungrounded is a result, not a failure."
    ),
    request=VerifyGroundingRequest,
    capability="verify_grounding",
    read_only=True,
)

SYNC = ToolSpec(
    name=f"{NAMESPACE}_sync",
    title="Update the index from local working trees",
    description=(
        "Update the RepoAtlas index from the local working trees in scope, using the "
        "existing safe sync pipeline. This changes the RepoAtlas index and nothing "
        "else: it never modifies repository source, never runs repository code, and "
        "never fetches, pulls, checks out or resets anything. Evidence already "
        "supplied in live investigations is rechecked against the new index."
    ),
    request=SyncRequest,
    capability="sync",
    read_only=False,
)

#: Every tool this server exposes, in listing order. Adding a capability to this
#: tuple is the whole of exposing it, and a capability absent from it is
#: unreachable over the protocol however complete it is below.
TOOLS: tuple[ToolSpec, ...] = (
    REPOSITORIES,
    STATUS,
    SEARCH,
    INSPECT,
    IMPACT,
    RELATIONSHIPS,
    DEPENDENCIES,
    HTTP_OPERATIONS,
    DEPLOYMENT_TARGETS,
    VERIFY_GROUNDING,
    SYNC,
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOLS}
