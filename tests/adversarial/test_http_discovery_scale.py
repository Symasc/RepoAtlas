"""Measure bounded discovery separately from index setup; no agent-speed claim."""

from __future__ import annotations

import json
import statistics
import time
from contextlib import closing
from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.http_inventory import DiscoveryBasis, HttpOperationsRequest
from tests.test_http_structural_matching import CLIENT, SERVER, _index


@pytest.mark.scale
@pytest.mark.parametrize("count", [10, 50])
def test_workspace_discovery_with_unrelated_repositories(tmp_path: Path, count: int) -> None:
    sources = {
        "frontend": {"client.ts": CLIENT},
        "backend-a": {"server.ts": SERVER},
        "backend-b": {"server.ts": SERVER},
    }
    sources.update(
        {
            f"other-{i:03}": {"other.ts": SERVER.replace("/v1/courses", "/unrelated")}
            for i in range(count - 3)
        }
    )
    before = time.perf_counter()
    ws, _ = _index(tmp_path, sources)
    setup = time.perf_counter() - before
    with closing(CapabilityService(ws)) as service:
        request = HttpOperationsRequest(
            context=CapabilityContext(scope=CapabilityScope.GLOBAL),
            discover=True,
            method="GET",
            route_prefix="/v1/courses/:id/roster",
            limit=10,
        )
        samples = []
        result = None
        for _ in range(5):
            start = time.perf_counter()
            result = service.http_operations(request)
            samples.append(time.perf_counter() - start)
            assert {row.repository for row in result.operations} == {
                "frontend",
                "backend-a",
                "backend-b",
            }
            assert len(result.operations) == 4 and not result.has_more
            assert isinstance(result.basis, DiscoveryBasis)
            assert result.basis.repositories_examined == count
            assert result.basis.operations_examined >= count
        assert result is not None and isinstance(result.basis, DiscoveryBasis)
        print(
            json.dumps(
                {
                    "repositories": count,
                    "setup_seconds": setup,
                    "median_query_seconds": statistics.median(samples),
                    "p95_nearest_rank_seconds": max(samples),
                    "samples": samples,
                    "candidate_operations_examined": result.basis.operations_examined,
                    "returned_operations": len(result.operations),
                    "response_bytes": len(result.model_dump_json().encode()),
                }
            )
        )
