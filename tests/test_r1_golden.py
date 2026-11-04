"""R1 changes no route, decision, declaration or default output: the golden snapshot says so.

``tests/data/r1_golden.json`` was written once, at the code of commit 2da633f and before any R1
product code (``python -m tests.r1_golden --write``). Each test compares one section of one
corpus with it. A difference is never fixed by regenerating the file: R1 promised to leave every
one of these byte-for-byte alone, so a difference is a finding to escalate.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from tests.r1_golden import GOLDEN, SECTIONS, normalise, snapshot
from tests.r1_provider_corpus import CORPUS_NAMES, Corpora

MISSING = (
    f"{GOLDEN} is missing. It is generated ONCE, at the code of commit 2da633f and before any R1 "
    "product code, with `python -m tests.r1_golden --write`; it is never regenerated afterwards."
)


@pytest.fixture(scope="module")
def corpora(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Corpora]:
    built = Corpora(tmp_path_factory.mktemp("r1-golden"))
    yield built
    built.close()


@pytest.fixture(scope="module")
def current(corpora: Corpora) -> Callable[[str], dict[str, Any]]:
    """The snapshot of one corpus as it is now, taken once."""
    taken: dict[str, dict[str, Any]] = {}

    def get(name: str) -> dict[str, Any]:
        if name not in taken:
            taken[name] = snapshot(corpora.workspace(name))
        return taken[name]

    return get


@functools.cache
def golden() -> dict[str, Any]:
    if not GOLDEN.exists():
        pytest.fail(MISSING, pytrace=False)
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def first_difference(then: Any, now: Any, where: str = "") -> str | None:
    """The first place two JSON values differ, named by its path."""
    if type(then) is not type(now):
        return f"{where or '<root>'}: golden {then!r:.200} but now {now!r:.200}"
    if isinstance(then, dict):
        for key in sorted(set(then) | set(now)):
            if key not in then or key not in now:
                side = "only now" if key not in then else "only in the golden"
                return f"{where}/{key}: present {side}"
            found = first_difference(then[key], now[key], f"{where}/{key}")
            if found:
                return found
        return None
    if isinstance(then, list):
        for index, (before, after) in enumerate(zip(then, now, strict=False)):
            found = first_difference(before, after, f"{where}[{index}]")
            if found:
                return found
        if len(then) != len(now):
            return f"{where}: golden has {len(then)} entries but now {len(now)}"
        return None
    return None if then == now else f"{where}: golden {then!r:.200} but now {now!r:.200}"


def test_the_golden_covers_exactly_the_corpora_and_sections_the_snapshot_takes() -> None:
    document = golden()
    assert document["base_commit"] == "2da633f"
    assert document["sections"] == list(SECTIONS)
    assert sorted(document["corpora"]) == sorted(CORPUS_NAMES)
    for name, part in document["corpora"].items():
        assert sorted(part) == sorted(SECTIONS), name


@pytest.mark.parametrize("section", SECTIONS)
@pytest.mark.parametrize("corpus", CORPUS_NAMES)
def test_a_corpus_section_is_what_it_was_before_r1(
    corpus: str, section: str, current: Callable[[str], dict[str, Any]]
) -> None:
    pinned = golden()["corpora"][corpus][section]
    now = normalise(current(corpus)[section])
    difference = first_difference(pinned, now, f"{corpus}.{section}")
    assert difference is None, (
        f"{difference}\nThe golden is never regenerated: escalate the difference, "
        "do not refresh the file."
    )


def test_the_snapshot_holds_the_http_surface_it_is_meant_to_pin(
    current: Callable[[str], dict[str, Any]],
) -> None:
    """A guard on the guard: a corpus that indexed to nothing would pin nothing."""
    for name in CORPUS_NAMES:
        now = current(name)
        assert now["repositories"], name
        # some route is claimed or served, even where none is proven (fan, flask)
        served_here = any(edge["type"] == "SERVES_HTTP_ROUTE" for edge in now["edges"])
        assert served_here or any(
            plan["routes"] or plan["code_routes"] for plan in now["plans"].values()
        ), name
    assert current("connections")["decisions"], "the connections corpus makes requests"
    assert current("py")["decisions"], "buyer2 makes requests"
    assert current("nest-declared")["declarations"], "F2 states one declaration"
    assert current("py")["previews"], "an origin group exists for buyer2"
