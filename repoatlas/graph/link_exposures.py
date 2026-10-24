"""Where a user says a repository is deployed, as exposures the HTTP join may fall back on.

A deployment link (``deployment_links``) states that a repository is reached at a URL: the
link's own ``url``, or the URL a stated deployment target resolves to under the link's
values. That covers the three hops no indexed source established for it -- service
identity, deployment binding and source binding -- so each route the repository's code
serves becomes one exposure at that origin, marked :data:`LINK_SOURCE`.

The join (:func:`repoatlas.graph.http_crossings.plan_http_crossings`) never weighs these
against what the source establishes. It consults them only for a request the source left
unlinked, and links it when exactly one handler of the linked repository serves it; those
three hops are then ``declared``, each place naming the link, never a file. A source-proven
link is never overridden. Pure: reads the links and routes it is given, writes nothing.

A scoped declaration (``DeploymentLink.scope``) states no URL, so :func:`plan_link_exposures` adds
nothing for it. :func:`plan_scoped_exposures` gives it one claim per route of the repository it
names instead, marked :data:`~repoatlas.graph.http_scopes.HTTP_SCOPED_EXPOSURE`: never stored,
never offered to the ordinary fallback, and consulted by the join only for a request the
declaration covers.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from repoatlas.graph.deploy_identity import ServedRoute
from repoatlas.graph.http_scopes import HTTP_SCOPED_EXPOSURE
from repoatlas.models import http
from repoatlas.models.deployment_targets import DeploymentLink
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.parsers.http_facts import HTTP_EXPOSED_ENDPOINT, PENDING_FACT
from repoatlas.storage.protocols import UnresolvedRefRecord

#: The ``identity_source`` of an exposure a user's link states.
LINK_SOURCE = "deployment-link"
#: The path a link's place carries: not a file, so never opened, cited or evidence.
LINK_PLACE = "@deployment-links/{id}"
#: Exposures one link may add: a person's statement, not a crawl.
MAX_LINK_EXPOSURES = 2_000


def link_base(url: str) -> str | None:
    """The base a link's URL serves routes under: its origin, plus its path when that is
    more than ``/``. ``None`` when it is not an absolute http(s) URL."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    target = http.client_target(f"{parts.scheme}://{parts.netloc}/")
    if target is None or target.origin is None:
        return None
    path = parts.path.rstrip("/")
    return target.origin + path


def plan_link_exposures(
    links: Iterable[DeploymentLink],
    urls: Mapping[int, str],
    routes: Sequence[ServedRoute],
    repo_ids: Mapping[str, int],
) -> list[UnresolvedRefRecord]:
    """One exposure claim per route the linked repository's code serves, at each link's
    URL. ``urls`` gives a link that names a stated target the URL that target resolves to
    under its values; a link with neither, or whose repository is not indexed, adds none."""
    by_repo: dict[int, list[ServedRoute]] = {}
    for route in routes:
        by_repo.setdefault(route.repo_id, []).append(route)
    found: list[UnresolvedRefRecord] = []
    for link in sorted(links, key=lambda item: item.id):
        url = link.url or urls.get(link.id)
        repo_id = repo_ids.get(link.repository)
        base = link_base(url) if url else None
        if base is None or repo_id is None:
            continue
        place = LINK_PLACE.format(id=link.id)
        added = 0
        for route in by_repo.get(repo_id, ()):
            method, _, path = route.body.partition(" ")
            line = route.line_start if route.line_start is not None else route.declared_at
            if line is None:
                continue
            body = http.endpoint_body(method, f"{base}{path}")
            if http.canonical_endpoint(body) is None:
                continue
            meta = {
                PENDING_FACT: HTTP_EXPOSED_ENDPOINT,
                "method": method,
                "route": path,
                "deployment": base,
                "identity_source": LINK_SOURCE,
                "source": LINK_SOURCE,
                "scope": f"link:{link.id}",
                "link_id": str(link.id),
                "link_in": place,
                "link_label": link.label,
                "link_callers": json.dumps(sorted(link.callers)),
                "link_verification": link.verification,
                **({"link_environment": link.environment} if link.environment else {}),
            }
            found.append(
                UnresolvedRefRecord(
                    repo_id=route.repo_id,
                    file_id=route.file_id,
                    src_id=route.src_id,
                    edge_type=EdgeType.EXPOSES_HTTP_ENDPOINT,
                    target_kind=EntityType.HTTP_ENDPOINT,
                    target_key=body,
                    line_start=line,
                    line_end=route.line_end if route.line_start is not None else line,
                    meta_json=json.dumps(meta, sort_keys=True),
                )
            )
            added += 1
            if added >= MAX_LINK_EXPOSURES:
                break
    return found


def plan_scoped_exposures(
    links: Iterable[DeploymentLink],
    routes_by_repo: Mapping[int, Sequence[ServedRoute]],
    repo_ids: Mapping[str, int],
) -> list[UnresolvedRefRecord]:
    """One claim per route the serving repository's code serves, for each scoped declaration whose
    serving repository and caller are both indexed. A claim carries what an ordinary link's does,
    and the words of its scope: the file (``scope_path``) and symbol (``scope_symbol``), the hash
    of the base it serves (``scope_base_key``), how the base is shown (``base_shown``), the route
    prefix the service mounts its routes under (``base_path``) and the route as the code serves it
    (``route``, without that prefix). The prefix is the service's, never the request's: the target
    is the shown base, the prefix and the route, as ``deployment`` is the shown base and the
    prefix. The base is never a URL, and no claim holds the base text."""
    found: list[UnresolvedRefRecord] = []
    for link in sorted(links, key=lambda item: item.id):
        scope = link.scope
        if scope is None or len(link.callers) != 1:
            continue
        serving, caller = repo_ids.get(link.repository), repo_ids.get(link.callers[0])
        if serving is None or caller is None:
            continue
        place = LINK_PLACE.format(id=link.id)
        base = f"{scope.base_shown}{scope.base_path}"
        added = 0
        for route in routes_by_repo.get(serving, ()):
            method, _, path = route.body.partition(" ")
            line = route.line_start if route.line_start is not None else route.declared_at
            if line is None or http.canonical_route(route.body) is None:
                continue
            meta = {
                PENDING_FACT: HTTP_SCOPED_EXPOSURE,
                "method": method,
                "route": path,
                "deployment": base,
                "identity_source": LINK_SOURCE,
                "source": LINK_SOURCE,
                "scope": f"link:{link.id}",
                "link_id": str(link.id),
                "link_in": place,
                "link_label": link.label,
                "link_callers": json.dumps(sorted(link.callers)),
                "link_verification": link.verification,
                **({"link_environment": link.environment} if link.environment else {}),
                "scope_path": scope.path,
                **({"scope_symbol": scope.symbol} if scope.symbol else {}),
                "scope_base_key": scope.base_key,
                "base_shown": scope.base_shown,
                "base_path": scope.base_path,
            }
            found.append(
                UnresolvedRefRecord(
                    repo_id=route.repo_id,
                    file_id=route.file_id,
                    src_id=route.src_id,
                    edge_type=EdgeType.EXPOSES_HTTP_ENDPOINT,
                    target_kind=EntityType.HTTP_ENDPOINT,
                    target_key=http.endpoint_body(method, f"{base}{path}"),
                    line_start=line,
                    line_end=route.line_end if route.line_start is not None else line,
                    meta_json=json.dumps(meta, sort_keys=True),
                )
            )
            added += 1
            if added >= MAX_LINK_EXPOSURES:
                break
    return found
