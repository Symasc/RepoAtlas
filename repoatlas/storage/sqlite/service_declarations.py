"""``service_declarations``: what users state about external services, redirects and callbacks.

Written only by a person (the API, an import), never by a sync: no global pass reads or writes it
(``007_service_declarations.sql``). The behaviour is that of the deployment link store
(:mod:`repoatlas.storage.sqlite.deployment_links`): every change is also one row of
``service_declaration_audit``, written in the same transaction, so a declaration and the record of
its creation exist together or not at all and the record outlives a revoked declaration. Stating
what is already stated returns it and writes nothing; updating to what is already stated writes
nothing; what a declaration is about (its kind and identity) cannot change.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict

from repoatlas.models.deployment_targets import DeclarationProvenance
from repoatlas.models.service_declarations import (
    MAX_SERVICE_DECLARATIONS,
    SERVICE_DECLARATION_FIELDS,
    ServiceDeclaration,
    ServiceDeclarationConflictError,
    ServiceDeclarationFields,
    ServiceDeclarationIdentityError,
    ServiceDeclarationKind,
    ServiceDeclarationLimitError,
)
from repoatlas.storage.sqlite.database import Database

SERVICE_DECLARATIONS_SELECT = (
    "SELECT id, kind, identity_key, fields_json, created_at, updated_at FROM service_declarations "
)

#: Audit rows one read returns at most.
MAX_SERVICE_HISTORY = 500


class ServiceAuditEntry(BaseModel):
    """One thing an operator did to a service declaration, as the history keeps it."""

    model_config = ConfigDict(frozen=True)

    id: int
    at: datetime
    action: str
    kind: ServiceDeclarationKind
    subject_id: int
    subject: str
    via: str
    request_group: str | None = None
    note: str | None = None
    before: dict[str, object] | None = None
    after: dict[str, object] | None = None


def service_declaration(row: sqlite3.Row) -> ServiceDeclaration:
    return ServiceDeclaration(
        id=row["id"],
        fields=SERVICE_DECLARATION_FIELDS.validate_json(row["fields_json"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class SqliteServiceDeclarationStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    def list_all(self, kind: ServiceDeclarationKind | None = None) -> list[ServiceDeclaration]:
        """Every declaration, by id: all kinds, or one."""
        if kind is None:
            rows = self._db.execute(SERVICE_DECLARATIONS_SELECT + "ORDER BY id").fetchall()
        else:
            rows = self._db.execute(
                SERVICE_DECLARATIONS_SELECT + "WHERE kind = ? ORDER BY id", (kind,)
            ).fetchall()
        return [service_declaration(row) for row in rows]

    def get(self, declaration_id: int) -> ServiceDeclaration | None:
        row = self._db.execute(
            SERVICE_DECLARATIONS_SELECT + "WHERE id = ?", (declaration_id,)
        ).fetchone()
        return service_declaration(row) if row is not None else None

    def create(
        self,
        fields: ServiceDeclarationFields,
        *,
        provenance: DeclarationProvenance | None = None,
        via: str | None = None,
    ) -> ServiceDeclaration:
        """Store a declaration. Stating what is already stated returns that declaration and
        writes nothing, so asking twice is one declaration; ``via`` is ``import`` for an import
        (recorded as one) and otherwise taken from the ``provenance``. An origin already
        classified in other words is refused, never overwritten."""
        now = datetime.now(timezone.utc).isoformat()
        source = via or (provenance.via if provenance is not None else "api")
        with self._db.transaction() as conn:
            rows = conn.execute(
                SERVICE_DECLARATIONS_SELECT + "WHERE kind = ? AND identity_key = ? ORDER BY id",
                (fields.kind, fields.identity_key),
            ).fetchall()
            for existing in map(service_declaration, rows):
                if existing.fields == fields:
                    return existing
                if fields.kind == "external_service":
                    raise ServiceDeclarationConflictError(existing.id, fields.subject)
            (count,) = conn.execute("SELECT COUNT(*) FROM service_declarations").fetchone()
            if count >= MAX_SERVICE_DECLARATIONS:
                raise ServiceDeclarationLimitError(
                    f"at most {MAX_SERVICE_DECLARATIONS} service declarations are kept"
                )
            cursor = conn.execute(
                "INSERT INTO service_declarations (kind, identity_key, fields_json, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?)",
                (fields.kind, fields.identity_key, fields.model_dump_json(), now, now),
            )
            declaration_id = int(cursor.lastrowid or 0)
            self._audit(
                conn,
                now,
                "import" if source == "import" else "create",
                declaration_id,
                source,
                provenance,
                None,
                fields,
            )
        created = self.get(declaration_id)
        assert created is not None
        return created

    def update(
        self,
        declaration_id: int,
        fields: ServiceDeclarationFields,
        *,
        provenance: DeclarationProvenance | None = None,
    ) -> ServiceDeclaration | None:
        """Replace what a declaration states. The kind and the identity (origin, redirect source,
        callback provider origin) are what it is about: another one is refused, and the history
        then says so in two rows when the person revokes and declares anew. Stating what it already
        states changes nothing and records nothing."""
        now = datetime.now(timezone.utc).isoformat()
        with self._db.transaction() as conn:
            before = self.get(declaration_id)
            if before is None:
                return None
            if before.fields.kind != fields.kind or before.identity_key != fields.identity_key:
                raise ServiceDeclarationIdentityError()
            if before.fields == fields:
                return before
            conn.execute(
                "UPDATE service_declarations SET fields_json = ?, updated_at = ? WHERE id = ?",
                (fields.model_dump_json(), now, declaration_id),
            )
            self._audit(
                conn,
                now,
                "update",
                declaration_id,
                provenance.via if provenance is not None else "api",
                provenance,
                before.fields,
                fields,
            )
        return self.get(declaration_id)

    def delete(
        self, declaration_id: int, *, provenance: DeclarationProvenance | None = None
    ) -> bool:
        """Revoke a declaration. What it stated stays in the history."""
        now = datetime.now(timezone.utc).isoformat()
        with self._db.transaction() as conn:
            before = self.get(declaration_id)
            if before is None:
                return False
            cursor = conn.execute(
                "DELETE FROM service_declarations WHERE id = ?", (declaration_id,)
            )
            self._audit(
                conn,
                now,
                "revoke",
                declaration_id,
                provenance.via if provenance is not None else "api",
                provenance,
                before.fields,
                None,
            )
            return cursor.rowcount > 0

    def history(
        self,
        *,
        declaration_id: int | None = None,
        kind: ServiceDeclarationKind | None = None,
        limit: int = 100,
    ) -> list[ServiceAuditEntry]:
        """What was done to service declarations, newest first: one declaration's, one kind's or
        all."""
        clauses, params = ["1 = 1"], []
        if declaration_id is not None:
            clauses.append("subject_id = ?")
            params.append(declaration_id)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        where = " AND ".join(clauses)  # fixed text; the values are bound below
        rows = self._db.execute(
            "SELECT id, at, action, kind, subject_id, subject, via, request_group, note, "  # noqa: S608
            f"before_json, after_json FROM service_declaration_audit WHERE {where} "
            "ORDER BY id DESC LIMIT ?",
            (*params, max(1, min(limit, MAX_SERVICE_HISTORY))),
        ).fetchall()
        return [
            ServiceAuditEntry(
                id=row["id"],
                at=datetime.fromisoformat(row["at"]),
                action=row["action"],
                kind=row["kind"],
                subject_id=row["subject_id"],
                subject=row["subject"],
                via=row["via"],
                request_group=row["request_group"],
                note=row["note"],
                before=json.loads(row["before_json"]) if row["before_json"] else None,
                after=json.loads(row["after_json"]) if row["after_json"] else None,
            )
            for row in rows
        ]

    @staticmethod
    def _audit(
        conn: sqlite3.Connection,
        now: str,
        action: str,
        declaration_id: int,
        via: str,
        provenance: DeclarationProvenance | None,
        before: ServiceDeclarationFields | None,
        after: ServiceDeclarationFields | None,
    ) -> None:
        stated = after if after is not None else before
        assert stated is not None
        conn.execute(
            "INSERT INTO service_declaration_audit (at, action, kind, subject_id, subject, via, "
            "request_group, note, before_json, after_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now,
                action,
                stated.kind,
                declaration_id,
                stated.subject,
                via,
                provenance.request_group if provenance is not None else None,
                provenance.note if provenance is not None else None,
                before.model_dump_json() if before is not None else None,
                after.model_dump_json() if after is not None else None,
            ),
        )
