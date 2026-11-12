"""P18: a change to a Python declaration reaches its users, and theirs.

No Python-specific impact code exists: the walk follows the edges the Python resolver
wrote exactly as it follows TypeScript's (repoatlas.graph.impact).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import ImpactRequest
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.python_corpus import PKG, PYFIX

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


@pytest.fixture
def pyfix(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, {"pyfix": PYFIX})


def test_p18_the_walk_reaches_the_consumer_and_the_test_that_calls_it(pyfix: Workspace) -> None:
    seed = f"sym:pyfix:{PKG}/models.py:User:1"
    response = CapabilityService(pyfix).impact(ImpactRequest(context=GLOBAL, seed=seed))
    by_entity = {c.entity: c for c in (*response.candidates, *response.tests)}
    consumer = by_entity[f"sym:pyfix:{PKG}/abs_user.py:p2:3"]
    assert (consumer.distance, consumer.step.edge, consumer.step.use) == (1, "USES", "type")
    # A test is a leaf, reported in its own section.
    [test] = [c for c in response.tests if c.entity == "sym:pyfix:tests/test_consumer.py:test_p2:2"]
    assert (test.distance, test.step.edge, test.step.use, test.from_) == (
        2,
        "CALLS",
        "call",
        consumer.id,
    )
    # Through the package's re-export, under another name, and nowhere by name alone.
    assert f"sym:pyfix:{PKG}/reexport_user.py:p5:2" in by_entity
    assert not [key for key in by_entity if "unrelated.py" in key]
