"""``deployment_links``: what users state about where a repository is deployed.

The one store a user writes. Nothing a sync does reads or replaces it, and it is keyed by
repository slug, so a repository removed from the index and added again finds its links
where it left them (``003_deployment_links.sql``).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from repoatlas.models.deployment_conditions import DeploymentCondition
from repoatlas.models.deployment_targets import DeploymentLink, DeploymentLinkFields, LinkLimitError
from repoatlas.storage.sqlite.database import Database

#: Links one repository may carry: a person's list, not a crawl.
MAX_LINKS_PER_REPOSITORY = 200

_SELECT = (
    "SELECT id, repository, label, environment, url, target_id, variables_json, "
    "conditions_json, verification, verified_at, created_at, updated_at FROM deployment_links "
)


def _link(row: sqlite3.Row) -> DeploymentLink:
    return DeploymentLink(
        id=row["id"],
        repository=row["repository"],
        label=row["label"],
        environment=row["environment"],
        url=row["url"],
        target_id=row["target_id"],
        variables=json.loads(row["variables_json"]),
        conditions=[DeploymentCondition(**c) for c in json.loads(row["conditions_json"])],
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


class SqliteDeploymentLinkStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    def list(self, repository: str) -> list[DeploymentLink]:
        rows = self._db.execute(
            _SELECT + "WHERE repository = ? ORDER BY id",
            (repository,),
        ).fetchall()
        return [_link(row) for row in rows]

    def get(self, link_id: int) -> DeploymentLink | None:
        row = self._db.execute(_SELECT + "WHERE id = ?", (link_id,)).fetchone()
        return _link(row) if row is not None else None

    def create(self, repository: str, fields: DeploymentLinkFields) -> DeploymentLink:
        now = datetime.now(timezone.utc).isoformat()
        with self._db.transaction() as conn:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM deployment_links WHERE repository = ?", (repository,)
            ).fetchone()
            if count >= MAX_LINKS_PER_REPOSITORY:
                raise LinkLimitError(
                    f"a repository carries at most {MAX_LINKS_PER_REPOSITORY} deployment links"
                )
            cursor = conn.execute(
                "INSERT INTO deployment_links (repository, label, environment, url, target_id, "
                "variables_json, conditions_json, verification, verified_at, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    repository,
                    *self._values(fields),
                    _verified_at(fields, None, now),
                    now,
                    now,
                ),
            )
            link_id = int(cursor.lastrowid or 0)
        created = self.get(link_id)
        assert created is not None
        return created

    def update(self, link_id: int, fields: DeploymentLinkFields) -> DeploymentLink | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._db.transaction() as conn:
            before = self.get(link_id)
            if before is None:
                return None
            conn.execute(
                "UPDATE deployment_links SET label = ?, environment = ?, url = ?, target_id = ?, "
                "variables_json = ?, conditions_json = ?, verification = ?, verified_at = ?, "
                "updated_at = ? WHERE id = ?",
                (*self._values(fields), _verified_at(fields, before, now), now, link_id),
            )
        return self.get(link_id)

    def delete(self, link_id: int) -> bool:
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM deployment_links WHERE id = ?", (link_id,))
            return cursor.rowcount > 0

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
        )
