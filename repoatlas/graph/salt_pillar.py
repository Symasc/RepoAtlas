"""Source-backed Salt pillar resolution across explicitly associated roots."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from repoatlas.config.settings import SaltInstallationSettings, SaltRootSettings, SaltSettings
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.storage.protocols import FileRow, SymbolRow, UnresolvedRefRecord

_STATIC_TARGET = re.compile(r"^(?:\*|[A-Za-z0-9_.-]+)$")


@dataclass(frozen=True, slots=True)
class SaltPillarResolution:
    claim: UnresolvedRefRecord
    definition: SymbolRow
    resolution: str


def resolve_pillar_reads(
    settings: SaltSettings,
    index: ResolutionIndex,
    claims: Sequence[UnresolvedRefRecord],
    files: Mapping[int, FileRow],
) -> list[SaltPillarResolution]:
    """Resolve pillar reads only through configured roots and indexed top files."""
    if not settings.installations:
        return []

    by_file: dict[int, list[UnresolvedRefRecord]] = defaultdict(list)
    repo_ids = {slug: repo_id for repo_id, slug in index.repo_slugs.items()}
    for claim in claims:
        by_file[claim.file_id].append(claim)

    roots_by_installation: list[
        tuple[
            SaltInstallationSettings,
            list[tuple[SaltRootSettings, int, str, dict[str, object]]],
        ]
    ] = []
    for installation in settings.installations:
        master_file = _master_file(installation, repo_ids, index)
        if master_file is None:
            configured = installation.roots
        else:
            master_facts = [
                claim
                for claim in by_file.get(master_file.file_id, ())
                if _meta(claim).get("salt_fact") == "master_root"
            ]
            declared = {
                (meta.get("role"), meta.get("environment"), meta.get("salt_path"))
                for claim in master_facts
                if (meta := _meta(claim))
            }
            configured = tuple(
                root
                for root in installation.roots
                if (root.role, root.environment, root.salt_path) in declared
            )
            configured = (*configured, *_roots_under_managed_paths(master_facts, index))

        roots_by_key: dict[
            tuple[str, str, str, str, str],
            tuple[SaltRootSettings, int, str, dict[str, object]],
        ] = {}
        for root in configured:
            repo_id = repo_ids.get(root.repository)
            if repo_id is None:
                continue
            root_claim = next(
                (
                    claim
                    for claim in master_facts
                    if _matches_master_root(claim, root)
                ),
                None,
            ) if master_file is not None else None
            if master_file is not None and root_claim is None:
                continue
            root_evidence = (
                _master_root_evidence(root_claim, master_file)
                if root_claim is not None and master_file is not None
                else {
                    "source": "settings",
                    "repository": root.repository,
                    "path": root.path,
                    "role": root.role,
                    "environment": root.environment,
                    "salt_path": root.salt_path,
                }
            )
            roots_by_key[
                (root.repository, root.path, root.role, root.environment, root.salt_path)
            ] = (
                    root,
                    repo_id,
                    _top_path(root.path),
                    root_evidence,
                )
        roots = list(roots_by_key.values())
        roots_by_installation.append((installation, roots))

    top_entries: dict[int, list[UnresolvedRefRecord]] = defaultdict(list)
    for claim in claims:
        if _meta(claim).get("salt_fact") == "top_entry":
            top_entries[claim.file_id].append(claim)

    found: list[SaltPillarResolution] = []
    for claim in claims:
        if claim.edge_type is not EdgeType.READS or claim.target_kind is not EntityType.CONFIG:
            continue
        if not claim.target_key.startswith("pillar:"):
            continue
        qualified = claim.target_key.removeprefix("pillar:").replace(":", ".")
        if not qualified:
            continue
        origin = files.get(claim.file_id)
        if origin is None:
            continue

        candidates: dict[int, tuple[SymbolRow, dict[str, object]]] = {}
        for installation, roots in roots_by_installation:
            state_roots = [item for item in roots if item[0].role == "state"]
            pillar_roots = [item for item in roots if item[0].role == "pillar"]
            for state_root, state_repo, state_top_path, state_root_evidence in state_roots:
                if state_repo != origin.repo_id:
                    continue
                state_sls = _sls_name(state_root.path, origin.rel_path)
                if state_sls is None:
                    continue
                state_top = index.file(state_repo, state_top_path)
                if state_top is None:
                    continue
                state_mappings = [
                    mapping
                    for mapping in top_entries.get(state_top.file_id, [])
                    if _mapping_matches_root(mapping, state_root)
                    and _meta(mapping).get("sls") == state_sls
                    and _supported_target(_meta(mapping))
                ]
                for state_mapping in state_mappings:
                    state_meta = _meta(state_mapping)
                    environment = state_meta["environment"]
                    selector = state_meta["selector"]
                    matcher = state_meta["matcher"]
                    for (
                        pillar_root,
                        pillar_repo,
                        pillar_top_path,
                        pillar_root_evidence,
                    ) in pillar_roots:
                        if pillar_root.environment != environment:
                            continue
                        pillar_top = index.file(pillar_repo, pillar_top_path)
                        if pillar_top is None:
                            continue
                        selected = [
                            mapping
                            for mapping in top_entries.get(pillar_top.file_id, [])
                            if _mapping_matches_root(mapping, pillar_root)
                            and _meta(mapping).get("environment") == environment
                            and _meta(mapping).get("matcher") == matcher
                            and _meta(mapping).get("selector") == selector
                            and _supported_target(_meta(mapping))
                        ]
                        for pillar_mapping in selected:
                            pillar_sls = _meta(pillar_mapping).get("sls")
                            if not isinstance(pillar_sls, str):
                                continue
                            source = _top_evidence(pillar_mapping, pillar_top)
                            files_for_sls = _sls_files(index, pillar_root, pillar_repo, pillar_sls)
                            for pillar_file in files_for_sls:
                                for source_file in _included_files(
                                    index, pillar_root, pillar_file, by_file
                                ):
                                    for symbol in index.symbols_by_file.get(
                                        source_file.file_id, {}
                                    ).get(qualified, []):
                                        if (
                                            symbol.type is not EntityType.CONFIG
                                            or symbol.qualified_name != qualified
                                        ):
                                            continue
                                        definition_file = files.get(symbol.file_id)
                                        if definition_file is None:
                                            continue
                                        evidence = {
                                            "installation": installation.name,
                                            "environment": environment,
                                            "matcher": matcher,
                                            "selector": selector,
                                            "state_root": state_root_evidence,
                                            "pillar_root": pillar_root_evidence,
                                            "state_top": _top_evidence(state_mapping, state_top),
                                            "pillar_top": source,
                                            "definition": {
                                                "repository": definition_file.repo_slug,
                                                "path": definition_file.rel_path,
                                                "line": symbol.line_start,
                                                "name": qualified,
                                            },
                                            "consumer": {
                                                "repository": origin.repo_slug,
                                                "path": origin.rel_path,
                                                "line": claim.line_start,
                                            },
                                        }
                                        candidates.setdefault(symbol.entity_id, (symbol, evidence))

        if len(candidates) == 1:
            symbol, evidence = next(iter(candidates.values()))
            found.append(
                SaltPillarResolution(
                    claim=claim,
                    definition=symbol,
                    resolution=json.dumps({"salt_pillar": evidence}, sort_keys=True),
                )
            )
    return found


def _meta(claim: UnresolvedRefRecord) -> dict[str, str]:
    return json.loads(claim.meta_json) if claim.meta_json else {}


def _master_file(
    installation: SaltInstallationSettings,
    repo_ids: Mapping[str, int],
    index: ResolutionIndex,
) -> FileRow | None:
    master = installation.master_config
    if master is None:
        return None
    repo_id = repo_ids.get(master.repository)
    return index.file(repo_id, master.path) if repo_id is not None else None


def _roots_under_managed_paths(
    master_facts: Sequence[UnresolvedRefRecord], index: ResolutionIndex
) -> list[SaltRootSettings]:
    roots: dict[tuple[str, str, str, str], SaltRootSettings] = {}
    for claim in master_facts:
        meta = _meta(claim)
        role, environment, salt_path = (
            meta.get("role"),
            meta.get("environment"),
            meta.get("salt_path"),
        )
        if role not in {"state", "pillar"} or not environment or not salt_path:
            continue
        absolute = Path(salt_path)
        for repo_path, repo_id in index.repo_roots:
            if not absolute.is_relative_to(repo_path):
                continue
            relative = absolute.relative_to(repo_path).as_posix() or "."
            repository = index.repo_slugs[repo_id]
            root = SaltRootSettings(
                repository=repository,
                path=relative,
                role=role,
                environment=environment,
                salt_path=salt_path,
            )
            roots[(repository, relative, role, environment)] = root
            break
    return list(roots.values())


def _matches_master_root(claim: UnresolvedRefRecord, root: SaltRootSettings) -> bool:
    meta = _meta(claim)
    return (
        meta.get("salt_fact") == "master_root"
        and meta.get("role") == root.role
        and meta.get("environment") == root.environment
        and meta.get("salt_path") == root.salt_path
    )


def _master_root_evidence(claim: UnresolvedRefRecord, source: FileRow) -> dict[str, object]:
    meta = _meta(claim)
    return {
        "source": "master_config",
        "repository": source.repo_slug,
        "path": source.rel_path,
        "line": claim.line_start,
        "role": meta.get("role"),
        "environment": meta.get("environment"),
        "salt_path": meta.get("salt_path"),
    }


def _top_path(root: str) -> str:
    return "top.sls" if root == "." else f"{root}/top.sls"


def _under_root(root: str, path: str) -> str | None:
    candidate = PurePosixPath(path)
    prefix = PurePosixPath(root)
    try:
        relative = candidate.relative_to(prefix)
    except ValueError:
        return None
    return relative.as_posix()


def _sls_name(root: str, path: str) -> str | None:
    relative = _under_root(root, path)
    if relative is None or not relative.endswith(".sls") or relative == "top.sls":
        return None
    stem = relative[:-4]
    if stem.endswith("/init"):
        stem = stem[:-5]
    return stem.replace("/", ".")


def _mapping_matches_root(claim: UnresolvedRefRecord, root: SaltRootSettings) -> bool:
    meta = _meta(claim)
    return meta.get("environment") == root.environment


def _supported_target(meta: Mapping[str, str]) -> bool:
    selector = meta.get("selector", "")
    return meta.get("matcher") == "glob" and _STATIC_TARGET.fullmatch(selector) is not None


def _sls_files(
    index: ResolutionIndex, root: SaltRootSettings, repo_id: int, sls: str
) -> list[FileRow]:
    if not _SAFE_SLS.fullmatch(sls):
        return []
    base = sls.replace(".", "/")
    root_prefix = "" if root.path == "." else f"{root.path}/"
    candidates = (f"{root_prefix}{base}.sls", f"{root_prefix}{base}/init.sls")
    return [
        found
        for candidate in candidates
        if (found := index.file(repo_id, candidate)) is not None
    ]


def _included_files(
    index: ResolutionIndex,
    root: SaltRootSettings,
    start: FileRow,
    claims: Mapping[int, Sequence[UnresolvedRefRecord]],
) -> list[FileRow]:
    found: list[FileRow] = []
    pending = [start]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current.file_id in seen or _under_root(root.path, current.rel_path) is None:
            continue
        seen.add(current.file_id)
        found.append(current)
        for claim in claims.get(current.file_id, ()):
            if (
                claim.edge_type is not EdgeType.INCLUDES
                or claim.target_kind is not EntityType.SALT_STATE
            ):
                continue
            included = index.resolve_salt_include(current, claim.target_key)
            if included is not None and _under_root(root.path, included.rel_path) is not None:
                pending.append(included)
    return found


def _top_evidence(claim: UnresolvedRefRecord, source: FileRow) -> dict[str, object]:
    meta = _meta(claim)
    return {
        "repository": source.repo_slug,
        "path": source.rel_path,
        "line": claim.line_start,
        "environment": meta.get("environment"),
        "selector": meta.get("selector"),
        "matcher": meta.get("matcher"),
        "sls": meta.get("sls"),
    }


_SAFE_SLS = re.compile(r"^[A-Za-z0-9_.-]+$")
