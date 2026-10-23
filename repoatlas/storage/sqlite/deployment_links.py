"""``deployment_links``: what users state about where a repository is deployed.

The one store a user writes. No sync replaces it -- the global pass only reads it, for the HTTP join
to fall back on (repoatlas.graph.link_exposures) -- and it is keyed by
repository slug, so a repository removed from the index and added again finds its links
where it left them (``003_deployment_links.sql``).

Every change is also one row of ``declaration_audit`` (``005_http_declarations.sql``), written in
the same transaction: a link and the record of its creation exist together or not at all, and the
record outlives a revoked link. Creating what is already stated returns it and writes nothing;
stating a place another repository already holds, for callers it would also serve, is refused.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict

from repoatlas.models.deployment_conditions import DeploymentCondition
from repoatlas.models.deployment_targets import (
    DeclarationProvenance,
    DeclarationScope,
    DeploymentLink,
    DeploymentLinkFields,
    LinkConflictError,
    LinkLimitError,
    ScopeConflictError,
    declared_base,
)
from repoatlas.storage.sqlite.database import Database

#: Links one repository may carry: a person's list, not a crawl.
MAX_LINKS_PER_REPOSITORY = 200

DEPLOYMENT_LINKS_SELECT = (
    "SELECT id, repository, label, environment, url, target_id, variables_json, "
    "conditions_json, verification, verified_at, created_at, updated_at, callers_json, "
    "scope_json FROM deployment_links "
)

#: Audit rows one read returns at most.
MAX_HISTORY = 500


class AuditEntry(BaseModel):
    """One thing an operator did to a declaration, as the history keeps it."""

    model_config = ConfigDict(frozen=True)

    id: int
    at: datetime
    action: str
    subject_id: int
    repository: str
    via: str
    request_group: str | None = None
    note: str | None = None
    before: dict[str, object] | None = None
    after: dict[str, object] | None = None


def deployment_link(row: sqlite3.Row) -> DeploymentLink:
    return DeploymentLink(
        id=row["id"],
        repository=row["repository"],
        label=row["label"],
        environment=row["environment"],
        url=row["url"],
        target_id=row["target_id"],
        variables=json.loads(row["variables_json"]),
        conditions=[DeploymentCondition(**c) for c in json.loads(row["conditions_json"])],
        callers=json.loads(row["callers_json"]),
        scope=(
            DeclarationScope.model_validate(json.loads(row["scope_json"]))
            if row["scope_json"]
            else None
        ),
        verification=row["verification"],
        verified_at=datetime.fromisoformat(row["verified_at"]) if row["verified_at"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _verified_at(
    fields: DeploymentLinkFields, before: DeploymentLink | None, now: str
) -> str | None:
    """When a person confirmed it: kept while it stays confirmed, cleared when it does not."""
    if fields.verification != "user_confirmed":
        return None
    if before is not None and before.verification == "user_confirmed" and before.verified_at:
        return before.verified_at.isoformat()
    return now


def _stated(link: DeploymentLink) -> DeploymentLinkFields:
    """What a stored link states: its fields, without the row's own bookkeeping."""
    return DeploymentLinkFields.model_validate(
        link.model_dump(include=set(DeploymentLinkFields.model_fields))
    )


def _symbols_overlap(one: str | None, other: str | None) -> bool:
    """Whether two scoped symbols cover a common entity: equal, one inside the other at a dot
    boundary, or either absent (the whole file)."""
    if one is None or other is None:
        return True
    return one == other or one.startswith(other + ".") or other.startswith(one + ".")


class SqliteDeploymentLinkStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    def list(self, repository: str) -> list[DeploymentLink]:
        rows = self._db.execute(
            DEPLOYMENT_LINKS_SELECT + "WHERE repository = ? ORDER BY id",
            (repository,),
        ).fetchall()
        return [deployment_link(row) for row in rows]

    def list_all(self) -> list[DeploymentLink]:
        """Every repository's links, by id: what ``repoatlas links export`` writes."""
        rows = self._db.execute(DEPLOYMENT_LINKS_SELECT + "ORDER BY id").fetchall()
        return [deployment_link(row) for row in rows]

    def get(self, link_id: int) -> DeploymentLink | None:
        row = self._db.execute(DEPLOYMENT_LINKS_SELECT + "WHERE id = ?", (link_id,)).fetchone()
        return deployment_link(row) if row is not None else None

    def create(
        self,
        repository: str,
        fields: DeploymentLinkFields,
        *,
        provenance: DeclarationProvenance | None = None,
        via: str | None = None,
    ) -> DeploymentLink:
        """Store a link. Stating what the repository already states returns that link and
        writes nothing, so asking twice is one declaration; ``via`` is ``import`` for an import
        (recorded as one) and otherwise taken from the ``provenance``."""
        now = datetime.now(timezone.utc).isoformat()
        source = via or (provenance.via if provenance is not None else "api")
        with self._db.transaction() as conn:
            for existing in self.list(repository):
                if _stated(existing) == fields:
                    return existing
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM deployment_links WHERE repository = ?", (repository,)
            ).fetchone()
            if count >= MAX_LINKS_PER_REPOSITORY:
                raise LinkLimitError(
                    f"a repository carries at most {MAX_LINKS_PER_REPOSITORY} deployment links"
                )
            self._refuse_conflict(repository, fields, own_id=None)
            cursor = conn.execute(
                "INSERT INTO deployment_links (repository, label, environment, url, target_id, "
                "variables_json, conditions_json, verification, callers_json, scope_json, "
                "verified_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    repository,
                    *self._values(fields),
                    _verified_at(fields, None, now),
                    now,
                    now,
                ),
            )
            link_id = int(cursor.lastrowid or 0)
            self._audit(
                conn,
                now,
                "import" if source == "import" else "create",
                link_id,
                repository,
                source,
                provenance,
                None,
                fields,
            )
        created = self.get(link_id)
        assert created is not None
        return created

    def update(
        self,
        link_id: int,
        fields: DeploymentLinkFields,
        *,
        provenance: DeclarationProvenance | None = None,
    ) -> DeploymentLink | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._db.transaction() as conn:
            before = self.get(link_id)
            if before is None:
                return None
            if before.scope != fields.scope:
                # What a scoped declaration was made about is its identity: another scope, or
                # none, is another declaration, and the history then says so in two rows.
                raise ScopeConflictError(
                    "a scope cannot change: revoke the declaration and declare it anew"
                )
            self._refuse_conflict(before.repository, fields, own_id=link_id)
            conn.execute(
                "UPDATE deployment_links SET label = ?, environment = ?, url = ?, target_id = ?, "
                "variables_json = ?, conditions_json = ?, verification = ?, callers_json = ?, "
                "scope_json = ?, verified_at = ?, updated_at = ? WHERE id = ?",
                (*self._values(fields), _verified_at(fields, before, now), now, link_id),
            )
            if _stated(before) != fields:
                self._audit(
                    conn,
                    now,
                    "update",
                    link_id,
                    before.repository,
                    provenance.via if provenance is not None else "api",
                    provenance,
                    _stated(before),
                    fields,
                )
        return self.get(link_id)

    def delete(self, link_id: int, *, provenance: DeclarationProvenance | None = None) -> bool:
        """Revoke a link. What it stated stays in the history."""
        now = datetime.now(timezone.utc).isoformat()
        with self._db.transaction() as conn:
            before = self.get(link_id)
            if before is None:
                return False
            cursor = conn.execute("DELETE FROM deployment_links WHERE id = ?", (link_id,))
            self._audit(
                conn,
                now,
                "revoke",
                link_id,
                before.repository,
                provenance.via if provenance is not None else "api",
                provenance,
                _stated(before),
                None,
            )
            return cursor.rowcount > 0

    def history(
        self, *, link_id: int | None = None, repository: str | None = None, limit: int = 100
    ) -> list[AuditEntry]:
        """What was done to declarations, newest first: one link's, one repository's or all."""
        clauses, params = ["1 = 1"], []
        if link_id is not None:
            clauses.append("subject_id = ?")
            params.append(link_id)
        if repository is not None:
            clauses.append("repository = ?")
            params.append(repository)
        where = " AND ".join(clauses)  # fixed text; the values are bound below
        rows = self._db.execute(
            "SELECT id, at, action, subject_id, repository, via, request_group, note, "  # noqa: S608
            f"before_json, after_json FROM declaration_audit WHERE {where} "
            "ORDER BY id DESC LIMIT ?",
            (*params, max(1, min(limit, MAX_HISTORY))),
        ).fetchall()
        return [
            AuditEntry(
                id=row["id"],
                at=datetime.fromisoformat(row["at"]),
                action=row["action"],
                subject_id=row["subject_id"],
                repository=row["repository"],
                via=row["via"],
                request_group=row["request_group"],
                note=row["note"],
                before=json.loads(row["before_json"]) if row["before_json"] else None,
                after=json.loads(row["after_json"]) if row["after_json"] else None,
            )
            for row in rows
        ]

    def _refuse_conflict(
        self, repository: str, fields: DeploymentLinkFields, *, own_id: int | None
    ) -> None:
        """Refuse a URL another repository already states for callers this one would serve, and
        a scope another service already holds."""
        if fields.scope is not None:
            self._refuse_scope_conflict(repository, fields.scope, fields.callers, own_id=own_id)
            return
        base = declared_base(fields.url) if fields.url else None
        if base is None:
            return
        for other in self.list_all():
            if other.id == own_id or other.repository == repository or not other.url:
                continue
            if declared_base(other.url) != base:
                continue
            mine, theirs = set(fields.callers), set(other.callers)
            if not mine or not theirs or mine & theirs:
                raise LinkConflictError(other.id, other.repository)

    def _refuse_scope_conflict(
        self,
        repository: str,
        scope: DeclarationScope,
        callers: list[str],
        *,
        own_id: int | None,
    ) -> None:
        """Refuse a second service for one caller's base over a file (and symbol) another scoped
        declaration covers: the same repository at the same base path is the same service."""
        for other in self.list_all():
            held = other.scope
            if other.id == own_id or held is None or other.callers != callers:
                continue
            if (held.base_key, held.path) != (scope.base_key, scope.path):
                continue
            if not _symbols_overlap(held.symbol, scope.symbol):
                continue
            if (other.repository, held.base_path) == (repository, scope.base_path):
                continue
            raise ScopeConflictError(
                f"link {other.id} already states that {other.repository}"
                f"{held.base_path} is the service behind {scope.base_shown} in "
                f"{scope.path} for {callers[0]}: two services over one scope join nothing; "
                "revoke it first"
            )

    @staticmethod
    def _audit(
        conn: sqlite3.Connection,
        now: str,
        action: str,
        link_id: int,
        repository: str,
        via: str,
        provenance: DeclarationProvenance | None,
        before: DeploymentLinkFields | None,
        after: DeploymentLinkFields | None,
    ) -> None:
        conn.execute(
            "INSERT INTO declaration_audit (at, action, kind, subject_id, repository, via, "
            "request_group, note, before_json, after_json) "
            "VALUES (?, ?, 'deployment_link', ?, ?, ?, ?, ?, ?, ?)",
            (
                now,
                action,
                link_id,
                repository,
                via,
                provenance.request_group if provenance is not None else None,
                provenance.note if provenance is not None else None,
                before.model_dump_json() if before is not None else None,
                after.model_dump_json() if after is not None else None,
            ),
        )

    @staticmethod
    def _values(fields: DeploymentLinkFields) -> tuple[object, ...]:
        return (
            fields.label,
            fields.environment,
            fields.url,
            fields.target_id,
            json.dumps(fields.variables, sort_keys=True),
            json.dumps([c.model_dump(exclude_none=True) for c in fields.conditions]),
            fields.verification,
            json.dumps(sorted(fields.callers)),
            json.dumps(fields.scope.model_dump(), sort_keys=True) if fields.scope else None,
        )
