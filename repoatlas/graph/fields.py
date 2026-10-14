"""A use of a model's name, followed to the fields it reads or sets (``USES``, DF3b).

A model field (``Model.field``, kind ``field``) is reached only through a use that already
names its model: the parser records, on the declaration's use of ``T`` in a parameter's
annotation, the fields read through that parameter, and on its use of ``T`` in a
construction (TypeScript: an object literal typed ``T``), the fields it sets
(:mod:`repoatlas.parsers.py_uses`, :mod:`repoatlas.parsers.ts_uses`). When the use resolves
-- in the file's own pass for a name it declares, in the global ``USES`` stage for an
imported one -- each listed field that is one of **the resolved declaration's own fields**
is one more ``USES`` edge, from the same declaration, at the line that first reads or sets
it. Nothing else is followed:

* the resolved declaration must be a model: a class (TypeScript: or an interface or object
  type alias). A function, a value or a module named like a model owns no field;
* the field must be the resolved model's own, declared inside it. A same-named field of any
  other declaration, an inherited one, or one of an alias's target is never reached;
* a Python construction ``T(f=...)`` reaches ``T.f`` only when the field records the
  initializer proven to bind it by that keyword (``init``); a matching keyword proves
  nothing by itself (DF3B_PHASE_A.md §13.1).

The use's own edge, to the model, is written exactly as before: the member list is the
claim's, never its record's. Because every field edge rides on a use landing in the model's
file, the incremental stage re-resolves it whenever that file is written
(:mod:`repoatlas.graph.uses_link`).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from repoatlas.models.parsing import FIELD, EntityType
from repoatlas.parsers.ts_uses import FIELD_MEMBERS, FIELDS_OMITTED

#: The evidence key of a field use's record: which field of which model, and how it is reached.
FIELD_USE = "field_use"
#: What a TypeScript model is besides a class.
_TS_MODELS = frozenset({"interface", "type"})


@dataclass(frozen=True, slots=True)
class Model:
    """The declaration a use resolved to, as far as owning fields goes."""

    entity_id: int
    type: EntityType
    kind: str | None
    qualified_name: str
    line_start: int | None
    line_end: int | None


@dataclass(frozen=True, slots=True)
class Field:
    """A field entity of the model's file."""

    entity_id: int
    name: str
    qualified_name: str
    line_start: int | None
    init: str | None


@dataclass(frozen=True, slots=True)
class FieldUse:
    """One field a declaration reads or sets, resolved."""

    field: Field
    model: Model
    line: int
    access: str


def members_of(meta: Mapping[str, Any]) -> list[tuple[str, int, str]]:
    """The ``[field, line, access]`` members a use claim carries, well formed, in order."""
    raw = meta.get(FIELD_MEMBERS)
    if not raw:
        return []
    try:
        listed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    found: list[tuple[str, int, str]] = []
    for item in listed if isinstance(listed, list) else []:
        if (
            isinstance(item, list)
            and len(item) == 3
            and isinstance(item[0], str)
            and isinstance(item[1], int)
            and isinstance(item[2], str)
        ):
            found.append((item[0], item[1], item[2]))
    return found


def is_model(model: Model, *, python: bool) -> bool:
    if model.type is EntityType.CLASS:
        return True
    return not python and model.type is EntityType.SYMBOL and model.kind in _TS_MODELS


def resolve_members(
    model: Model,
    members: Sequence[tuple[str, int, str]],
    fields: Iterable[Field],
    *,
    python: bool,
) -> list[FieldUse]:
    """Each member that is one of ``model``'s own fields (see the module docstring)."""
    if not members or not is_model(model, python=python):
        return []
    prefix = f"{model.qualified_name}."
    low = model.line_start if model.line_start is not None else 0
    high = model.line_end if model.line_end is not None else low
    own: dict[str, list[Field]] = {}
    for candidate in fields:
        if (
            candidate.qualified_name == prefix + candidate.name
            and candidate.line_start is not None
            and low <= candidate.line_start <= high
        ):
            own.setdefault(candidate.name, []).append(candidate)
    found: list[FieldUse] = []
    for name, line, access in members:
        matches = own.get(name, [])
        if len(matches) != 1:
            continue
        field = matches[0]
        if python and access == "construction" and field.init is None:
            continue
        found.append(FieldUse(field=field, model=model, line=line, access=access))
    return found


def field_record(use: FieldUse, meta: Mapping[str, Any]) -> dict[str, object]:
    """``field_use``: which field of which model, how it is reached, and -- when the use named
    more members than a claim records -- how many it did not record (``members_omitted``)."""
    record: dict[str, object] = {
        "field": use.field.name,
        "model": use.model.qualified_name,
        "access": use.access,
    }
    omitted = str(meta.get(FIELDS_OMITTED) or "")
    if omitted.isdigit():
        record["members_omitted"] = int(omitted)
    return record


def field_meta(meta: Mapping[str, str], use: FieldUse) -> dict[str, str]:
    """The claim's meta as a field use's record reads it: a value-level ``field`` use."""
    return {**meta, "use": "field", "form": use.access, "occurrences": "1"}


def model_of_row(row: Any) -> Model:
    """A stored symbol (:class:`~repoatlas.storage.protocols.SymbolRow`) as a model."""
    return Model(
        entity_id=row.entity_id,
        type=row.type,
        kind=row.meta.get("kind"),
        qualified_name=row.qualified_name or row.name,
        line_start=row.line_start,
        line_end=row.line_end,
    )


def fields_of_rows(rows: Iterable[Any]) -> list[Field]:
    """The field entities among stored symbols."""
    return [
        Field(
            entity_id=row.entity_id,
            name=row.name,
            qualified_name=row.qualified_name or row.name,
            line_start=row.line_start,
            init=row.meta.get("init"),
        )
        for row in rows
        if row.type is EntityType.SYMBOL and row.meta.get("kind") == FIELD
    ]
