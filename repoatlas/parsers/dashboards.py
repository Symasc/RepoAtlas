"""SQL in checked-in dashboards: Grafana panel targets and template variables.

A dashboard checked in as JSON runs its SQL against a data source it names only by an
identifier the dashboard server resolves, so nothing in the file says which database that
is. The SQL is still a statement about some table's columns, and is recorded the way code's
SQL is (``persistence-query``, :mod:`repoatlas.parsers.ts_persistence`), so the global pass
can say why it binds nothing (:mod:`repoatlas.graph.persistence`).

A file is a dashboard when its top level has ``panels``. Read: each
``panels[].targets[].rawSql`` (and the panels of ``row`` panels), and each
``templating.list[].query`` that is a string. A ``$variable`` or ``${variable}`` in the SQL
is something the dashboard server substitutes at run time: ``dynamic``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import PurePosixPath

import yaml

from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.http_facts import pending_reference
from repoatlas.parsers.sql import sql_predicates
from repoatlas.parsers.ts_persistence import PERSISTENCE_QUERY

_VARIABLE = re.compile(r"\$\{?[A-Za-z_]")
_MAX_QUERIES = 256


def dashboard_references(
    path: PurePosixPath, documents: Sequence[yaml.Node | None]
) -> list[ParsedReference]:
    if path.suffix != ".json" or not documents or not isinstance(documents[0], yaml.MappingNode):
        return []
    top = _fields(documents[0])
    if not isinstance(top.get("panels"), yaml.SequenceNode):
        return []
    found: list[tuple[str, yaml.ScalarNode]] = []
    panels = list(top["panels"].value)  # type: ignore[union-attr]
    while panels and len(found) < _MAX_QUERIES:
        panel = panels.pop(0)
        if not isinstance(panel, yaml.MappingNode):
            continue
        fields = _fields(panel)
        nested = fields.get("panels")
        if isinstance(nested, yaml.SequenceNode):
            panels.extend(nested.value)
        targets = fields.get("targets")
        for target in targets.value if isinstance(targets, yaml.SequenceNode) else []:
            if isinstance(target, yaml.MappingNode):
                raw = _fields(target).get("rawSql")
                if isinstance(raw, yaml.ScalarNode):
                    found.append(("panel", raw))
    templating = top.get("templating")
    variables = (
        _fields(templating).get("list") if isinstance(templating, yaml.MappingNode) else None
    )
    for variable in variables.value if isinstance(variables, yaml.SequenceNode) else []:
        if isinstance(variable, yaml.MappingNode):
            query = _fields(variable).get("query")
            if isinstance(query, yaml.ScalarNode):
                found.append(("variable", query))
    claims: list[ParsedReference] = []
    for source, node in found[:_MAX_QUERIES]:
        text = str(node.value)
        meta = {"kind": "sql", "library": "dashboard", "binding": "dashboard", "source": source}
        if _VARIABLE.search(text):
            meta["dynamic"] = "true"
        else:
            predicates = sql_predicates(text)
            if predicates is None:
                meta["unsupported"] = "true"
            else:
                meta["predicates"] = json.dumps(
                    [[p.column, p.op, list(p.values), p.table] for p in predicates],
                    separators=(",", ":"),
                )
        line = node.start_mark.line + 1
        claims.append(
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.SYMBOL,
                "dashboard",
                line,
                node.end_mark.line + 1,
                role=PERSISTENCE_QUERY,
                receiver="dashboard",
                **meta,
            )
        )
    return claims


def _fields(node: yaml.MappingNode) -> dict[str, yaml.Node]:
    return {str(k.value): v for k, v in node.value if isinstance(k, yaml.ScalarNode)}
