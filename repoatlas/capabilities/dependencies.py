"""Filtering, ordering, provider resolution and paging for dependencies (V2).

Pure. It receives the grouped dependency inventory the store already produced,
decides which of it the request asked for and in what order, and hands back a
page. It opens nothing, reads nothing and resolves nothing.

Why the order is settled here
-----------------------------
Pagination is a cut, and a cut is only meaningful over a total order that belongs
to the repositories rather than to the database. Two indexes built from the same
managed root in a different file order would fill their tables differently, and if
that decided which packages landed on page one, both answers would be right and
they would disagree. The order used is the substrate's own —
``(repository slug, ecosystem, normalized name)`` — which is also the grouping
identity, so "the next package" means the same thing to the cursor as it does to
the store.

Why the cursor carries two digests
----------------------------------
A continuation token has to answer two different questions before it is safe to
honour. *Is this the same question?* — otherwise page two would be a slice of a
different filtered set, silently skipping and duplicating rows. *Is this the same
inventory?* — otherwise a sync landing between two page requests would leave the
caller holding a result assembled from two generations, which no single index ever
contained. The first is a digest of the request, the second a digest of the
ordered dataset, and they fail differently on purpose: a changed question is the
caller's mistake, a changed index is nobody's.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from repoatlas.capabilities.errors import (
    InvalidCursorError,
    InvalidDependencyRequestError,
    StaleCursorError,
)
from repoatlas.dependencies.normalize import REPOSITORY_ECOSYSTEMS, normalize
from repoatlas.models.dependencies import DependencyPackage, DependencyRole
from repoatlas.models.dependency_inventory import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    AppliedPageLimits,
    DependenciesRequest,
    DependencyPackageView,
    ProviderResolution,
)

#: Version of the cursor payload. A token from another shape is refused rather
#: than parsed optimistically — an old cursor read under new rules is exactly the
#: silent skip the token exists to prevent.
CURSOR_VERSION = 1

#: Field separators for digest input. Control characters, so no package name,
#: path or constraint can produce the same byte sequence two different ways.
_FIELD = "\x1f"
_RECORD = "\x1e"

#: The ordering identity of one package group, and the cursor's continuation key.
PackageKey = tuple[str, str, str]


def store_slug(package: DependencyPackage) -> str:
    """The repository identity the store grouped this package under.

    The store keys on the slug and falls back to the display name for a
    repository that has none. Keying on anything else here would let the
    capability's idea of "the same package" drift from the store's, which is the
    one thing a cursor cannot survive.
    """
    return package.repository_slug or package.repository


def package_key(package: DependencyPackageView) -> PackageKey:
    """The substrate's own grouping identity: repository, ecosystem, exact name.

    The repository belongs in it because two repositories declaring one package
    are making two statements; the ecosystem belongs in it because npm ``foo-bar``
    and PyPI ``foo_bar`` normalize alike and are not the same dependency.
    """
    return (package.repository_slug, package.ecosystem, package.normalized_name)


def digest(records: Iterable[Sequence[str]]) -> str:
    """A stable digest of an ordered sequence of field tuples."""
    joined = _RECORD.join(_FIELD.join(fields) for fields in records)
    return hashlib.blake2b(joined.encode("utf-8"), digest_size=16).hexdigest()


def admitted_roles(request: DependenciesRequest) -> tuple[DependencyRole, ...]:
    """The observation kinds this request covers, in the substrate's own order.

    Empty means all three. The order follows the enum rather than the request, so
    two callers asking for the same set in a different order get the same answer
    and the same cursor.
    """
    if not request.roles:
        return tuple(DependencyRole)
    wanted = set(request.roles)
    return tuple(role for role in DependencyRole if role in wanted)


def wanted_name(request: DependenciesRequest, ecosystem: str) -> str | None:
    """The requested package name under one ecosystem's equality rule.

    Normalization is per ecosystem and is the ecosystem's own — PEP 503 folding
    for PyPI, lowercasing for npm and gem, nothing for anything else. Applying one
    universal rule would make ``foo.bar`` and ``foo-bar`` the same npm package,
    which the registry does not agree with.

    ``None`` when no name was asked for.
    """
    if request.name is None:
        return None
    return normalize(ecosystem, request.name)


def selects(package: DependencyPackage, request: DependenciesRequest) -> bool:
    """Whether this package group is inside the request's filters.

    Exact identity throughout. A name is compared under the ecosystem's rule and
    never as a substring or a resemblance: this capability is the structured
    inventory, and a caller exploring by likeness is asking search a different
    question.
    """
    if request.ecosystem is not None and package.ecosystem != str(request.ecosystem):
        return False
    name = wanted_name(request, package.ecosystem)
    return name is None or package.normalized_name == name


@dataclass(frozen=True, slots=True)
class ProviderCandidate:
    """One registered repository publishing a package name, as stored."""

    repository_slug: str
    repository_name: str
    version: str | None
    path: str
    line_start: int
    line_end: int
    parser: str


def provider_index(
    packages: Sequence[DependencyPackage],
) -> dict[tuple[str, str], list[ProviderCandidate]]:
    """Every repository publishing every package name, keyed by exact identity.

    Built from the **whole** index rather than from the request's scope, because
    who publishes a name is a fact about the managed root and not about what the
    caller happened to ask for. Deriving it from a filtered view is how a
    presentation filter turns into a resolution claim.
    """
    found: dict[tuple[str, str], list[ProviderCandidate]] = {}
    for package in packages:
        for observation in package.observations:
            if observation.role is not DependencyRole.PROVIDED:
                continue
            found.setdefault((package.ecosystem, package.normalized_name), []).append(
                ProviderCandidate(
                    repository_slug=store_slug(package),
                    repository_name=package.repository,
                    version=observation.value,
                    path=observation.source_path,
                    line_start=observation.line,
                    line_end=observation.line_end,
                    parser=observation.parser,
                )
            )
    for candidates in found.values():
        candidates.sort(key=lambda candidate: (candidate.repository_slug, candidate.path))
    return found


def provider_resolution(
    ecosystem: str,
    owner_slug: str,
    candidates: Sequence[ProviderCandidate],
    *,
    declares: bool,
) -> ProviderResolution:
    """What the deterministic linker concludes, restated rather than recomputed.

    The rule is the linker's own (V1_SPEC §9): when a repository *declares* a
    package, look at the repositories publishing that name, discount the declaring
    one, and resolve to a repository only when exactly one remains. Zero and
    several are both honest "unknown", and the linker draws no edge for either —
    picking the first candidate, or the highest version, or the nearest repository
    would each be a guess wearing a resolution's clothes.

    ``NOT_APPLICABLE`` is returned where the question does not arise at all: an
    ecosystem in which no repository can publish a package, or a package this
    repository never asked for. Distinct from ``NONE``, which is a real answer —
    one says there was nothing to resolve, the other that nothing resolves it.

    ``declares`` comes from the **stored** observations, never from the filtered
    view. A caller narrowing to lockfile rows is changing what they are shown, and
    a deterministic conclusion that moved when they did would be a property of the
    request rather than of the repositories.
    """
    if ecosystem not in REPOSITORY_ECOSYSTEMS or not declares:
        return ProviderResolution.NOT_APPLICABLE
    others = [candidate for candidate in candidates if candidate.repository_slug != owner_slug]
    if not others:
        return ProviderResolution.NONE
    if len(others) == 1:
        return ProviderResolution.UNIQUE
    return ProviderResolution.AMBIGUOUS


def declares_package(package: DependencyPackage) -> bool:
    """Whether this repository asks for this package anywhere in its manifests.

    Read off the stored group, before any role filter, because it decides whether
    a linker conclusion applies and that is a fact about the code.
    """
    return any(observation.role is DependencyRole.DECLARED for observation in package.observations)


def page_limits_for(request: DependenciesRequest) -> AppliedPageLimits:
    """What the server grants for one page, and where it lowered it.

    A supplied value is honoured up to the ceiling and lowered to it otherwise,
    never raised: asking for a small page is a legitimate request, and quietly
    widening it would return an inventory the caller did not ask to read.
    """
    if request.limit is None:
        return AppliedPageLimits(requested=None, applied=min(DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE))
    if request.limit > MAX_PAGE_SIZE:
        return AppliedPageLimits(requested=request.limit, applied=MAX_PAGE_SIZE, clamped=("limit",))
    return AppliedPageLimits(requested=request.limit, applied=request.limit)


def request_digest(request: DependenciesRequest, repository_slugs: Sequence[str]) -> str:
    """Everything a cursor must still agree with to be honoured.

    Not the whole request: ``limit`` is deliberately absent, because changing page
    size mid-walk is safe when continuation is by sort key rather than by offset,
    and refusing it would be a restriction with nothing behind it. Everything that
    changes *which* packages are in the dataset is here, and so is the resolved
    repository set — a scope that resolved to different repositories is a different
    question even when the request text is identical.
    """
    return digest(
        [
            ("scope", str(request.context.scope)),
            ("repositories", *sorted(repository_slugs)),
            ("ecosystem", str(request.ecosystem) if request.ecosystem else ""),
            ("name", request.name or ""),
            ("roles", *(str(role) for role in admitted_roles(request))),
            ("providers", str(request.include_providers)),
            ("provenance", str(request.include_provenance)),
        ]
    )


def inventory_digest(packages: Sequence[DependencyPackageView]) -> str:
    """A digest of the ordered dataset this cursor walks.

    Covers every fact the page could carry — each package's identity, each stored
    observation with its provenance, and each provider candidate — so a sync that
    changes any of them is observed rather than assumed away. It deliberately does
    **not** cover facts outside the filters: a change somewhere the cursor will
    never walk cannot cause a duplicate or an omission inside it, and invalidating
    the token for it would be a refusal with no defect behind it.
    """
    records: list[tuple[str, ...]] = []
    for package in packages:
        records.append(("pkg", *package_key(package), package.name))
        for observation in package.observations:
            provenance = observation.provenance
            records.append(
                (
                    "obs",
                    observation.repository_slug,
                    str(observation.role),
                    observation.value or "",
                    observation.group or "",
                    observation.scope,
                    provenance.path,
                    str(provenance.line_start),
                    str(provenance.line_end),
                    provenance.parser,
                )
            )
        records.append(("res", str(package.provider_resolution)))
        for provider in package.providers:
            records.append(
                (
                    "prv",
                    provider.repository_slug,
                    provider.version or "",
                    provider.provenance.path,
                    str(provider.provenance.line_start),
                    str(provider.provenance.line_end),
                )
            )
    return digest(records)


def encode_cursor(*, request: str, inventory: str, after: PackageKey) -> str:
    """An opaque continuation token.

    Opaque because its contents are the server's business: a caller that could
    read an offset out of it would be able to write one back in, and a cursor that
    can be hand-edited is a way to page through a dataset that no longer exists.
    """
    payload = json.dumps(
        {"v": CURSOR_VERSION, "r": request, "i": inventory, "a": list(after)},
        separators=(",", ":"),
        sort_keys=True,
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


@dataclass(frozen=True, slots=True)
class Cursor:
    """A decoded continuation token, not yet checked against anything."""

    request: str
    inventory: str
    after: PackageKey


def decode_cursor(token: str) -> Cursor:
    """Parse a token, or refuse it. Never guess at a damaged one."""
    padding = "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(token + padding)
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidCursorError() from exc
    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        raise InvalidCursorError()
    after = payload.get("a")
    request = payload.get("r")
    inventory = payload.get("i")
    if (
        not isinstance(request, str)
        or not isinstance(inventory, str)
        or not isinstance(after, list)
        or len(after) != 3
        or not all(isinstance(part, str) for part in after)
    ):
        raise InvalidCursorError()
    return Cursor(request=request, inventory=inventory, after=(after[0], after[1], after[2]))


def resume(
    packages: Sequence[DependencyPackageView],
    token: str | None,
    *,
    request: str,
    inventory: str,
) -> int:
    """Where in the ordered dataset this page starts.

    The two refusals are different failures and are reported as such. A cursor
    whose request digest disagrees was made for another question, and honouring it
    would slice a set it never described. A cursor whose inventory digest
    disagrees described this question against an index that has since moved, and
    continuing would hand back a page assembled from two generations — a result no
    single state of the repositories ever contained.
    """
    if token is None:
        return 0
    cursor = decode_cursor(token)
    if cursor.request != request:
        raise InvalidCursorError()
    if cursor.inventory != inventory:
        raise StaleCursorError()
    for index, package in enumerate(packages):
        if package_key(package) == cursor.after:
            return index + 1
    # The digest matched, so the dataset is the one this cursor was cut from and
    # the key must be in it. Reaching here means the two disagree, which is a
    # corrupted token rather than a moved index.
    raise InvalidCursorError()


def validate_dependency_request(request: DependenciesRequest) -> None:
    """Structural refusals the model cannot express on its own."""
    if request.roles and len(set(request.roles)) != len(request.roles):
        raise InvalidDependencyRequestError(
            "that dependency request names the same role more than once",
            detail="List each observation kind at most once.",
            field="roles",
        )
